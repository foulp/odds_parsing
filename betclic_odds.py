"""Simples SuperSub (premiere colonne) et duos decisifs Betclic.

Les duos abreges sont resolus sur la liste complete Betclic avant le matching
PMU, afin de ne pas confondre un remplacant exclu avec un titulaire retenu.
Les abreviations du prenom ou du nom exigent une correspondance unique.
Le matching PMU utilise les memes regles et le meme seuil que Winamax.
"""

import logging
import math

import pandas as pd
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, expect
from name_matching import DEFAULT_MATCH_SCORE, normalize_name, match_names, matches_abbreviation


SINGLES_MARKET = "Joueur décisif (buteur ou passeur décisif) + son remplaçant (t. rég)"
DUOS_MARKET = "Les deux joueurs sont décisifs (buteur ou passeur décisif)"
RESULT_COLUMNS = ["Taille Combi", "Joueurs", "Cote Betclic"]


def parse_odds(text, label):
    try:
        odds = float(text.strip().replace(",", "."))
    except ValueError:
        logging.warning("[BETCLIC] Cote indisponible pour %s : %r", label, text)
        return None
    if not math.isfinite(odds) or odds <= 1:
        logging.warning("[BETCLIC] Cote invalide pour %s : %r", label, text)
        return None
    return odds


def expand_selections(button, rows, label):
    if not button.count():
        return
    if "is-expanded" in (button.get_attribute("class") or "").split():
        return
    before = rows.count()
    for attempt in range(2):
        try:
            if "is-expanded" not in (button.get_attribute("class") or "").split():
                button.click(timeout=10000)
            expect(rows).not_to_have_count(before, timeout=10000)
            if rows.count() <= before:
                raise ValueError(f"Liste Betclic reduite pendant l'ouverture : {label}")
            logging.info("[BETCLIC] %s : %s -> %s selections", label, before, rows.count())
            return
        except (PlaywrightError, AssertionError) as exc:
            logging.warning("[BETCLIC] %s : ouverture tentative %s/2 echouee : %s",
                            label, attempt + 1, exc)
            # Un clic peut mettre a jour l'icone sans charger les lignes.
            if rows.count() > before:
                return
    raise RuntimeError(f"Liste Betclic non ouverte : {label}")


def market_section(page, title):
    return page.locator("div.marketBox").filter(
        has=page.locator("h2", has_text=title),
    ).last


def read_singles(section):
    bodies = section.locator(":scope > div.marketBox_body")
    expect(bodies).to_have_count(2, timeout=10000)
    players = []
    for i in range(2):
        body = bodies.nth(i)
        rows = body.locator(".marketBox_lineSelection:has(p.marketBox_label)")
        rows.first.wait_for(timeout=10000)
        more = body.locator("xpath=following-sibling::div[1]").locator("button.is-seeMore")
        expand_selections(more, rows, f"simples equipe {i + 1}")
        records = rows.evaluate_all("""rows => rows.map(row => ({
            name: row.querySelector('p.marketBox_label')?.innerText.trim() || '',
            odds: row.querySelector('.marketBox_item .btn_label')?.innerText.trim() || ''
        }))""")
        for record in records:
            if not record["name"]:
                raise ValueError("Ligne joueur Betclic sans nom")
            odds = parse_odds(record["odds"], record["name"])
            players.append({"home": 1 if i == 0 else 0, "Nom": record["name"], "Cote": odds})
        logging.info("[BETCLIC] Equipe %s : %s joueurs lus", i + 1, len(records))
    return players


def read_duos(section):
    rows = section.locator(".marketBox_lineSelection:has(p.marketBox_label)")
    rows.first.wait_for(timeout=10000)
    expand_selections(section.locator("button.is-seeMore"), rows, "duos")
    records = rows.evaluate_all("""rows => rows.map(row => ({
        label: row.querySelector('p.marketBox_label')?.innerText.trim() || '',
        odds: row.querySelector('.btn_label')?.innerText.trim() || ''
    }))""")
    duos = []
    for record in records:
        names = [name.strip() for name in record["label"].split("&")]
        if len(names) != 2 or not all(names):
            raise ValueError(f"Libelle duo Betclic inattendu : {record['label']!r}")
        odds = parse_odds(record["odds"], record["label"])
        if odds is not None:
            duos.append({"names": names, "Cote": odds})
    logging.info("[BETCLIC] %s duos lus", len(duos))
    return duos


def dismiss_cookie_banner(page):
    cookie = page.get_by_role("button", name="Continuer sans accepter", exact=True)
    try:
        cookie.click(timeout=10000)
    except PlaywrightTimeoutError:
        if cookie.is_visible():
            raise
        logging.debug("[BETCLIC] Aucune banniere cookies a fermer")
        return
    expect(cookie).not_to_be_visible(timeout=10000)


def dismiss_optional_dialogs(page):
    dismiss_cookie_banner(page)
    close = page.get_by_role("dialog").get_by_role("button", name="Fermer", exact=True)
    try:
        close.click(timeout=2500)
    except PlaywrightTimeoutError:
        cookie = page.get_by_role("button", name="Continuer sans accepter", exact=True)
        if cookie.is_visible():
            logging.info("[BETCLIC] Banniere cookies tardive : fermeture avant le dialogue")
            dismiss_cookie_banner(page)
            close.click(timeout=10000)
        elif close.is_visible():
            raise
    expect(close).not_to_be_visible(timeout=10000)


def extract_markets(page):
    dismiss_optional_dialogs(page)
    # Le SuperSub est sur la page principale, pas dans la categorie Joueurs.
    singles_section = market_section(page, SINGLES_MARKET)
    try:
        singles_section.wait_for(timeout=10000)
    except PlaywrightTimeoutError:
        logging.warning("[BETCLIC] Marche simples SuperSub indisponible : %s", page.url)
        return [], []
    players = read_singles(singles_section)
    page.get_by_text("Joueurs", exact=True).first.click(timeout=10000)
    duos_tab = page.get_by_role("button", name="Duos & Trios", exact=True)
    try:
        duos_tab.click(timeout=10000)
    except PlaywrightTimeoutError:
        if duos_tab.is_visible():
            raise
        logging.warning("[BETCLIC] Onglet Duos & Trios indisponible : %s", page.url)
        return players, []
    duos_section = market_section(page, DUOS_MARKET)
    try:
        duos_section.wait_for(timeout=10000)
    except PlaywrightTimeoutError:
        logging.warning("[BETCLIC] Marche duos decisifs indisponible : %s", page.url)
        return players, []
    return players, read_duos(duos_section)


def resolve_abbreviation(label, players, duo_label=None):
    """Resout un joueur abrege sur la liste complete, sans deviner une ambiguite."""
    query = normalize_name(label)
    exact = [i for i, player in enumerate(players) if normalize_name(player["Nom"]) == query]
    if len(exact) == 1:
        return exact[0]
    matches = exact or [
        i for i, player in enumerate(players) if matches_abbreviation(label, player["Nom"])
    ]
    if len(matches) == 1:
        return matches[0]
    logging.warning(
        '[BETCLIC] Joueur %s : BETCLIC="%s" ; duo="%s" ; candidats=%s ; '
        'correspondance unique exigee sur la liste complete Betclic',
        "ambigu" if matches else "non reconnu", label, duo_label or "non precise",
        [players[i]["Nom"] for i in matches],
    )
    return None


def match_players(players, frame, min_score=DEFAULT_MATCH_SCORE):
    mapping = {}
    for home in (1, 0):
        candidates = frame[(frame["home"] == home) & (frame["Taille Combi"] == 1)]["Joueurs"].tolist()
        indices = [i for i, p in enumerate(players) if p["home"] == home]
        matches = match_names(candidates, [players[i]["Nom"] for i in indices],
                             min_score, label="BETCLIC")
        for reference, (candidate, _) in matches.items():
            mapping[indices[candidate]] = candidates[reference]
    return mapping


def build_results(players, duos, frame, min_score=DEFAULT_MATCH_SCORE):
    mapping = match_players(players, frame, min_score)
    results = {}
    for index, name in mapping.items():
        if players[index]["Cote"] is not None:
            results[(1, name)] = players[index]["Cote"]
    for duo in duos:
        duo_label = " & ".join(duo["names"])
        indices = [resolve_abbreviation(name, players, duo_label) for name in duo["names"]]
        if None in indices or indices[0] == indices[1] or any(i not in mapping for i in indices):
            continue
        names = [mapping[i] for i in indices]
        if names[0] == names[1]:
            raise ValueError(f"Duo Betclic associe deux fois au meme joueur : {names}")
        key = (2, " - ".join(sorted(names)))
        if key in results and results[key] != duo["Cote"]:
            raise ValueError(f"Cotes Betclic contradictoires pour {key[1]}")
        results[key] = duo["Cote"]
    return pd.DataFrame(
        [{"Taille Combi": size, "Joueurs": name, "Cote Betclic": odds}
         for (size, name), odds in results.items()],
        columns=RESULT_COLUMNS,
    )
