"""Compositions Flashscore du jour : domicile a gauche, exterieur a droite.

Seules les compositions officielles filtrent par defaut ; allow_probable accepte
aussi les probables. Une absence normale ne filtre pas, une erreur reste bloquante.
"""

from datetime import datetime
import logging
import re
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, expect, TimeoutError as PlaywrightTimeoutError
from thefuzz import fuzz
from name_matching import normalize_name


COMPETITION_URL = "https://www.flashscore.fr/football/europe/ligue-des-champions"


def normalize(value):
    return normalize_name(value)


TEAM_ALIASES = {
    "paris sg": "psg",
    "paris saint germain": "psg",
    "paris saint germain fc": "psg",
    "roma": "as rome",
    "as roma": "as rome",
}


def normalize_team(value):
    """Alias exacts uniquement ; conserve les suffixes femmes/jeunes."""
    name = normalize(value)
    return TEAM_ALIASES.get(name, name)


def is_today(label, today):
    label = label.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", label):
        return True
    match = re.match(r"^(\d{2})\.(\d{2})\.\s", label)
    return bool(match and (int(match[1]), int(match[2])) == (today.day, today.month))


def discover_matches(page, today=None, competition_url=COMPETITION_URL, allow_empty=False):
    today = today or datetime.now(ZoneInfo("Europe/Paris")).date()
    page.goto(competition_url, wait_until="domcontentloaded")
    rows = page.locator(".event__match")
    page.locator(".event__match, .noMatchFound").first.wait_for(timeout=20000)
    if not rows.count() and allow_empty:
        logging.info("[FLASHSCORE] %s : calendrier vide pour le %s", competition_url, today)
        return []
    matches = rows.evaluate_all("""rows => rows.map(row => ({
        time: row.querySelector('.event__stageTime, .event__time')?.textContent.trim() || '',
        home: row.querySelector('.event__homeParticipant')?.textContent.trim() || '',
        away: row.querySelector('.event__awayParticipant')?.textContent.trim() || '',
        url: row.querySelector('a[href*="/match/"]')?.href || ''
    }))""")
    result = [match for match in matches if is_today(match["time"], today)
              and match["home"] and match["away"] and match["url"]]
    for match in result:
        time = re.search(r"(\d{1,2}:\d{2})$", match["time"])
        if not time:
            raise ValueError(f"Horaire Flashscore illisible : {match}")
        match["start"] = datetime.combine(
            today, datetime.strptime(time[1], "%H:%M").time(), ZoneInfo("Europe/Paris"),
        ).isoformat()
    if not result and not allow_empty:
        raise ValueError(f"Aucun match Flashscore trouve pour le {today}")
    logging.info("[FLASHSCORE] %s matchs trouves pour le %s", len(result), today)
    return result


def find_match(matches, home, away):
    ranked = sorted(
        ((min(fuzz.WRatio(normalize_team(home), normalize_team(match["home"])),
              fuzz.WRatio(normalize_team(away), normalize_team(match["away"]))), match)
         for match in matches),
        key=lambda item: item[0], reverse=True,
    )
    if not ranked or ranked[0][0] < 75:
        closest = (
            f"{ranked[0][1]['home']} - {ranked[0][1]['away']} (score {ranked[0][0]})"
            if ranked else "aucun candidat"
        )
        raise ValueError(f"Match Flashscore introuvable : {home} - {away}; meilleur candidat : {closest}")
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 10:
        raise ValueError(f"Match Flashscore ambigu : {home} - {away}")
    return ranked[0][1]


def composition_url(url):
    parts = urlsplit(url)
    path = parts.path.split("/resume/")[0].rstrip("/") + "/resume/compositions/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def read_lineups(page, url, allow_probable=False):
    page.goto(composition_url(url), wait_until="domcontentloaded")
    groups = page.locator('[data-testid="wcl-lineupsGroup"]')
    official = groups.filter(has_text=re.compile(r"Compositions de départ", re.IGNORECASE)).first
    probable = groups.filter(has_text=re.compile(r"Compositions probables", re.IGNORECASE)).first
    try:
        groups.first.wait_for(timeout=20000)
    except PlaywrightTimeoutError:
        # Une page de match chargee sans onglet COMPOS est une absence normale.
        page.locator(".duelParticipant").wait_for(timeout=10000)
        if page.locator('a[href*="/resume/compositions/"]').count():
            raise
        logging.info("[FLASHSCORE] %s : aucune composition publiee ; aucun filtre", url)
        return None
    if official.count():
        starters = official
    elif probable.count():
        if not allow_probable:
            logging.info("[FLASHSCORE] %s : composition probable uniquement ; aucun filtre", url)
            return None
        starters = probable
    else:
        raise ValueError(f"Etat des compositions Flashscore non reconnu : {url}")
    result = {}
    for home, side in ((1, "left"), (0, "right")):
        rows = starters.locator(f'[data-testid="wcl-lineupsParticipantGeneral-{side}"]')
        expect(rows).to_have_count(11, timeout=15000)
        players = rows.evaluate_all("""rows => rows.map(row => {
            const link = row.querySelector('a[href*="/joueur/"]');
            return {
                name: link?.querySelector('span')?.textContent.trim() || '',
                href: link?.getAttribute('href') || '',
                goalkeeper: !!row.querySelector('[title="Gardien"]')
                    || row.textContent.includes('(G)')
            };
        })""")
        if not players[0]["goalkeeper"] or sum(p["goalkeeper"] for p in players) != 1:
            raise ValueError(f"Gardien Flashscore non identifie dans la colonne {side}")
        outfield = [p for p in players if not p["goalkeeper"]]
        if len(outfield) != 10 or any(not p["name"] or not p["href"] for p in outfield):
            raise ValueError(f"Composition Flashscore incomplete dans la colonne {side}")
        if len({p["href"] for p in outfield}) != 10:
            raise ValueError(f"Joueurs Flashscore dupliques dans la colonne {side}")
        result[home] = outfield
    logging.info("[FLASHSCORE] %s : %s ; 10 joueurs de champ par equipe",
                 url, starters.inner_text().splitlines()[0])
    return result


def player_score(player, pmu_name):
    name = normalize(pmu_name)
    slug = player["href"].strip("/").split("/")[-2]
    full_score = fuzz.token_sort_ratio(normalize(slug), name)
    display = normalize(player["name"])
    parts = display.split()
    initials = [part for part in parts if len(part) == 1]
    surname = [part for part in parts if len(part) > 1]
    tokens = name.split()
    if surname and set(surname).issubset(tokens):
        remaining = tokens.copy()
        for part in surname:
            remaining.remove(part)
        if not initials or (remaining and remaining[0].startswith(initials[0])):
            return 100
    return full_score


def filter_players(frame, lineups):
    """Ne garde que les titulaires reconnus ; les absents du marche sont journalises."""
    selected = []
    for home in (1, 0):
        players = frame[frame["home"] == home]
        used = set()
        for player in lineups[home]:
            ranked = sorted(
                ((player_score(player, row["Joueurs"]), idx, row["Joueurs"])
                 for idx, row in players.iterrows()),
                key=lambda item: item[0], reverse=True,
            )
            if not ranked or ranked[0][0] < 85:
                logging.warning("[FLASHSCORE] Equipe %s : %s absent du marche PMU ou non reconnu",
                                home, player["name"])
                continue
            if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 5:
                raise ValueError(f"Joueur Flashscore ambigu : {player['name']}")
            score, idx, name = ranked[0]
            if idx in used:
                raise ValueError(f"Correspondance Flashscore dupliquee : {name}")
            used.add(idx)
            selected.append(idx)
            logging.info("[FLASHSCORE] Equipe %s : %s -> %s (%s)", home, player["name"], name, score)
        if not used:
            raise ValueError(f"Aucun titulaire Flashscore reconnu dans l'equipe {home}")
    return frame.loc[frame.index.isin(selected)].reset_index(drop=True)


def get_today_matches(headless=True, competition_urls=None):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page(timezone_id="Europe/Paris", locale="fr-FR")
            if competition_urls is None:
                return discover_matches(page)
            return [match for url in competition_urls
                    for match in discover_matches(page, competition_url=url, allow_empty=True)]
        finally:
            browser.close()


def get_match_lineups(matches, frame, headless=True, allow_probable=False, url=None):
    teams = {}
    for home in (1, 0):
        names = frame.loc[frame["home"] == home, "Equipe"].dropna().unique()
        if len(names) != 1:
            raise ValueError(f"Equipe PMU non identifiable pour home={home}")
        teams[home] = names[0]
    match = {"url": url} if url else find_match(matches, teams[1], teams[0])
    logging.info("[FLASHSCORE] %s - %s : %s", teams[1], teams[0], match["url"])
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            return read_lineups(browser.new_page(timezone_id="Europe/Paris", locale="fr-FR"),
                                match["url"], allow_probable=allow_probable)
        finally:
            browser.close()
