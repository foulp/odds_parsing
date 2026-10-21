"""References des rencontres du jour, rapprochees par competition et horaire."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from thefuzz import fuzz

from flashscore_lineups import discover_matches, normalize_team
from name_matching import normalize_name


PARIS = ZoneInfo("Europe/Paris")
CACHE_DIR = Path(".match-cache")
COMPETITIONS = {
    "ldc": {
        "name": "Ligue des Champions",
        "flashscore": "https://www.flashscore.fr/football/europe/ligue-des-champions",
        "pmu": "https://www.pmu.fr/sport/football/ligue-des-champions",
        "pmu_path": "football/champions_league",
        "winamax": "https://www.winamax.fr/paris-sportifs/sports/1/800000542/151665",
        "betclic": "https://www.betclic.fr/football-sfootball/ligue-des-champions-c8",
    },
    "l1": {
        "name": "Ligue 1",
        "flashscore": "https://www.flashscore.fr/football/france/ligue-1",
        "pmu": "https://www.pmu.fr/sport/football/france/ligue-1-mcdonalds",
        "pmu_path": "football/france/ligue_1",
        "winamax": "https://www.winamax.fr/paris-sportifs/sports/1/7/4",
        "betclic": "https://www.betclic.fr/football-sfootball/ligue-1-mcdonald-s-c4",
    },
    "pl": {
        "name": "Premier League",
        "flashscore": "https://www.flashscore.fr/football/angleterre/premier-league",
        "pmu": "https://www.pmu.fr/sport/football/angleterre/premier-league",
        "pmu_path": "football/england/premier_league",
        "winamax": "https://www.winamax.fr/paris-sportifs/sports/1/1/1",
        "betclic": "https://www.betclic.fr/football-sfootball/angl-premier-league-c3",
    },
}


@contextmanager
def discovery_page(headless=True):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page(timezone_id="Europe/Paris", locale="fr-FR")
            logging.info("[DECOUVERTE] Navigateur : fuseau=Europe/Paris, langue=fr-FR")
            page.route("**/*", lambda r: r.abort() if r.request.resource_type in
                       ("image", "font", "media") else r.continue_())
            yield page
        finally:
            browser.close()


def local_start(value):
    if type(value) in (int, float):
        return datetime.fromtimestamp(value, PARIS)
    if not isinstance(value, str):
        raise ValueError(f"Horaire invalide : {value!r}")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError(f"Horaire sans fuseau : {value}")
    return result.astimezone(PARIS)


def read_pmu_matches(session, competition, today):
    path = COMPETITIONS[competition]["pmu_path"]
    response = session.get(
        f"https://eu.offering-api.kambicdn.com/offering/v2018/pmusportsfr/listView/{path}.json",
        params={"lang": "fr_MC", "market": "FR", "client_id": "200", "channel_id": "1"},
        timeout=(5, 20),
    )
    response.raise_for_status()
    result = []
    for item in response.json()["events"]:
        event = item["event"]
        start = local_start(event["start"])
        if start.date() == today and "MATCH" in event.get("tags", []):
            result.append({"home": event["homeName"], "away": event["awayName"],
                           "start": start.isoformat(), "id": int(event["id"])})
    return result


def read_winamax_matches(page, competition, today):
    url = COMPETITIONS[competition]["winamax"]
    tournament = int(url.rstrip("/").split("/")[-1])
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_function(
        "id => !!window.PRELOADED_STATE?.tournaments?.[id] && !!window.PRELOADED_STATE?.matches",
        arg=tournament, timeout=20000,
    )
    events = page.evaluate(
        "id => Object.values(window.PRELOADED_STATE.matches).filter(m => m.tournamentId === id)",
        tournament,
    )
    result = []
    for event in events:
        start = local_start(event["matchStart"])
        if start.date() == today:
            result.append({"home": event["competitor1Name"], "away": event["competitor2Name"],
                           "start": start.isoformat(), "id": int(event["matchId"])})
    return result


MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
          "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12}


def betclic_date(label, now):
    text = normalize_name(label)
    for word, delta in (("aujourd hui", 0), ("demain", 1), ("apres demain", 2), ("hier", -1)):
        if text == word:
            # Betclic calcule ces libelles sur le jour UTC : entre minuit et 2h (Paris, ete),
            # les matchs du jour y sont encore groupes sous "Demain".
            return now.astimezone(timezone.utc).date() + timedelta(days=delta)
    numeric = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", label)
    if numeric:
        result = now.date().replace(year=int(numeric[3] or now.year),
                                    month=int(numeric[2]), day=int(numeric[1]))
        if not numeric[3] and (result - now.date()).days < -180:
            result = result.replace(year=result.year + 1)
        return result
    match = re.search(r"\b(\d{1,2}) ([a-z]+)(?: (\d{4}))?\b", text)
    if not match or match[2] not in MONTHS:
        raise ValueError(f"Date de groupe Betclic non reconnue : {label!r}")
    result = now.date().replace(year=int(match[3] or now.year), month=MONTHS[match[2]], day=int(match[1]))
    if not match[3] and (result - now.date()).days < -180:
        result = result.replace(year=result.year + 1)
    return result


def read_betclic_matches(page, competition, today):
    page.goto(COMPETITIONS[competition]["betclic"], wait_until="domcontentloaded")
    try:
        page.locator("a.cardEvent .scoreboard_hour").first.wait_for(state="attached", timeout=20000)
    except PlaywrightTimeoutError:
        logging.warning("[DECOUVERTE] Betclic %s : liste non chargee, une nouvelle tentative", competition)
        page.reload(wait_until="domcontentloaded")
        page.locator("a.cardEvent .scoreboard_hour").first.wait_for(state="attached", timeout=20000)
    cards = page.locator("a.cardEvent").evaluate_all("""els => els.map(e => ({
        title: e.getAttribute('aria-label') || '',
        url: e.href,
        date: e.closest('.groupEvents')?.querySelector('.groupEvents_headTitle')?.textContent.trim() || '',
        time: e.querySelector('.scoreboard_hour')?.textContent.trim() || ''
    }))""")
    result = []
    now = datetime.now(PARIS)
    for card in cards:
        if not re.search(r"-m\d+(?:$|[/?#])", card["url"]):
            continue
        if normalize_name(card["date"]) == "maintenant":
            logging.info("[DECOUVERTE] Betclic : rencontre en direct ignoree : %s", card["url"])
            continue
        if betclic_date(card["date"], now) != today:
            continue
        teams = card["title"].split(" - ")
        if len(teams) != 2 or not card["time"]:
            raise ValueError(f"Rencontre Betclic incomplete : {card}")
        start = datetime.combine(today, datetime.strptime(card["time"], "%H:%M").time(), PARIS)
        result.append({"home": teams[0], "away": teams[1], "start": start.isoformat(),
                       "url": card["url"]})
    logging.info("[DECOUVERTE][%s][BETCLIC] %s cartes lues, %s rencontres pour le %s ; groupes=%s",
                 competition.upper(), len(cards), len(result), today,
                 sorted({card["date"] for card in cards}))
    return result


class MissingMatchError(ValueError):
    """No reliable candidate; distinct from an ambiguous association."""


def associate_match(reference, candidates, bookmaker):
    """Unicite domicile/exterieur, score >= 75 et ecart >= 10, a +/- 15 minutes."""
    ranked = []
    for candidate in candidates:
        if abs((local_start(reference["start"]) - local_start(candidate["start"])).total_seconds()) > 900:
            continue
        score = min(fuzz.WRatio(normalize_team(reference["home"]), normalize_team(candidate["home"])),
                    fuzz.WRatio(normalize_team(reference["away"]), normalize_team(candidate["away"])))
        ranked.append((score, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)
    name = f'{reference["home"]} - {reference["away"]}'
    if not ranked or ranked[0][0] < 75:
        available = [(c["home"], c["away"], c["start"]) for c in candidates]
        raise MissingMatchError(
            f"{name} ({reference['start']}) : rencontre {bookmaker} introuvable au meme horaire ; "
            f"candidats={available}"
        )
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 10:
        raise ValueError(
            f"{name} : rencontre {bookmaker} ambigue ; candidats="
            f"{[(c['home'], c['away'], c['start'], score) for score, c in ranked[:2]]}"
        )
    return ranked[0][1]


def discover_competition(page, session, competition, today, issues=None):
    fixtures = discover_matches(page, today, COMPETITIONS[competition]["flashscore"], allow_empty=True)
    if not fixtures:
        return []
    sources = {
        "PMU": read_pmu_matches(session, competition, today),
        "WINAMAX": read_winamax_matches(page, competition, today),
        "BETCLIC": read_betclic_matches(page, competition, today),
    }
    logging.info("[DECOUVERTE][%s] Flashscore=%s ; references par bookmaker=%s",
                 competition.upper(), len(fixtures), {name: len(rows) for name, rows in sources.items()})
    result = []
    used = {bookmaker: set() for bookmaker in sources}
    for fixture in fixtures:
        try:
            matched = {bookmaker: associate_match(fixture, candidates, bookmaker)
                       for bookmaker, candidates in sources.items()}
        except MissingMatchError as exc:
            if issues is None:
                raise
            message = f"[{competition.upper()}] {exc}"
            issues.append(message)
            logging.error("[DECOUVERTE] %s ; match ignore, nouvelle tentative au prochain passage",
                          message)
            continue
        record = {
            "competition": competition, "home": fixture["home"], "away": fixture["away"],
            "start": fixture["start"], "flashscore_url": fixture["url"],
            "event_pmu": matched["PMU"]["id"], "event_wina": matched["WINAMAX"]["id"],
            "url_betclic": matched["BETCLIC"]["url"],
        }
        for bookmaker, candidate in matched.items():
            key = candidate.get("id", candidate.get("url"))
            if key in used[bookmaker]:
                raise ValueError(f"Reference {bookmaker} attribuee a plusieurs rencontres : {key}")
            used[bookmaker].add(key)
        logging.info("[DECOUVERTE] %s / %s - %s : PMU=%s, WINAMAX=%s, BETCLIC=%s",
                     competition, record["home"], record["away"], record["event_pmu"],
                     record["event_wina"], record["url_betclic"])
        result.append(record)
    return result


def select_matches(records, queries):
    if not queries:
        return records
    result = []
    for query in queries:
        normalized = normalize_name(query)
        sides = query.split(" - ")
        exact = [r for r in records if normalize_name(f'{r["home"]} - {r["away"]}') == normalized
                 or (len(sides) == 2 and normalize_team(sides[0]) == normalize_team(r["home"])
                     and normalize_team(sides[1]) == normalize_team(r["away"]))]
        candidates = exact or [
            r for r in records if normalized and (
                normalized == normalize_team(r["home"]) or normalized == normalize_team(r["away"])
                or normalized in normalize_name(f'{r["home"]} - {r["away"]}')
            )
        ]
        if len(candidates) != 1:
            names = [f'{r["competition"]}: {r["home"]} - {r["away"]}' for r in candidates or records]
            raise ValueError(f"Selection match {query!r} : {len(candidates)} correspondances ; choix : {names}")
        if candidates[0] not in result:
            result.append(candidates[0])
    return result


def read_cache(path, today):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["version"] != 1 or payload["date"] != today.isoformat():
        raise ValueError(f"Cache incompatible : {path}")
    cache = payload["competitions"]
    if not isinstance(cache, dict):
        raise ValueError(f"Cache competitions invalide : {path}")
    for competition, records in cache.items():
        if competition not in COMPETITIONS or not isinstance(records, list):
            raise ValueError(f"Competition invalide dans {path}")
        for record in records:
            if (record["competition"] != competition or local_start(record["start"]).date() != today
                    or not record["flashscore_url"] or not record["url_betclic"]
                    or not record["home"] or not record["away"]
                    or type(record["event_pmu"]) is not int or record["event_pmu"] <= 0
                    or type(record["event_wina"]) is not int or record["event_wina"] <= 0):
                raise ValueError(f"Reference invalide dans {path}")
    return cache


def get_daily_matches(competitions, session, headless=True, queries=None, refresh=False,
                      today=None, cache_dir=CACHE_DIR, issues=None):
    today = today or datetime.now(PARIS).date()
    path = Path(cache_dir) / f"{today.isoformat()}.json"
    cache = {}
    if path.exists():
        try:
            cache = read_cache(path, today)
        except (ValueError, KeyError, TypeError) as exc:
            if not refresh:
                raise ValueError(f"Cache incompatible : {path} ; utiliser --refresh-matches : {exc}") from exc
            logging.warning("[DECOUVERTE] Cache invalide remplace sur demande : %s : %s", path, exc)
    if refresh:
        cache = {c: records for c, records in cache.items() if c not in competitions}
    missing = [c for c in competitions if c not in cache]
    partial = {}
    if missing:
        with discovery_page(headless) as page:
            for competition in missing:
                errors = []
                records = discover_competition(page, session, competition, today, issues=errors)
                if errors:
                    if issues is None:
                        raise MissingMatchError("\n".join(errors))
                    issues.extend(errors)
                    partial[competition] = records
                else:
                    cache[competition] = records
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "date": today.isoformat(), "competitions": cache},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    logging.info("[DECOUVERTE] References du %s : %s", today, path)
    return select_matches([r for c in competitions for r in partial.get(c, cache.get(c, []))], queries)
