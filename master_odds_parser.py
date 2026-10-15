"""Compare les cotes PMU/Winamax/Betclic, puis exporte vers Google Sheets.

Sans --file, les matchs du jour sont decouverts dans ldc, l1 et pl (Europe/Paris).
--date demain ou --date AAAA-MM-JJ selectionne une autre date.
-c/--competition selectionne les competitions ; -m/--match cible des rencontres.
--file conserve le mode CSV manuel. Les references automatiques sont mises en cache
par jour dans .match-cache ; --refresh-matches renouvelle leur decouverte.
Les compositions officielles filtrent les 10 joueurs de champ par defaut.
--flashscore accepte aussi les compositions probables ; une absence ne filtre pas.
Toutes les combinaisons eligibles sont generees dans l'ordre domicile/exterieur puis tailles.
Les combinaisons PMU sans correspondance Winamax restent exportees.
La colonne url_betclic active les simples SuperSub (1 fois ou +) et les duos
Betclic pour les lignes PMU retenues ; vide, elle desactive cette extraction.
Les trios restent sans cote Betclic. Les ratios utilisent PMU / bookmaker.
--name-match-score fixe le seuil commun de matching Winamax et Betclic.
Le matching privilegie les noms exacts, puis les abreviations explicites
et les noms courts correspondant au premier mot du nom complet,
uniques dans les deux listes de l'equipe,
puis les noms de deux mots omettant des mots intermediaires avec premier/dernier
mots identiques et uniques dans les deux listes,
puis token_sort_ratio sur le nom complet, sans comparaison partielle.
--no-headless affiche le navigateur ; les delais MyMatch restent conservateurs.
Les durees par etape sont enregistrees dans scraping.log.
L'ouverture des listes Winamax est verifiee par l'ajout effectif de lignes joueurs.
Les cotes simples indisponibles ne declenchent un avertissement que pour les
joueurs rapproches des noms PMU retenus, apres le filtre des compositions.
En cas d'erreur DOM Winamax, le marche est reconstruit et le meme combo retente
une fois. Un second echec arrete le match plutot que poursuivre des lectures douteuses.
"""

import argparse
from datetime import date, datetime, timedelta
import requests
import time
import numpy as np
import pandas as pd
from contextlib import contextmanager, nullcontext
from itertools import combinations
from concurrent.futures import ThreadPoolExecutor, as_completed
import random
import re
import gspread
from gspread_dataframe import set_with_dataframe
from playwright.sync_api import sync_playwright, expect, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import logging
from flashscore_lineups import get_today_matches, get_match_lineups, filter_players
from betclic_odds import extract_markets as extract_betclic_markets, build_results as build_betclic_results
from name_matching import DEFAULT_MATCH_SCORE, match_names
from match_discovery import COMPETITIONS, PARIS, get_daily_matches, select_matches


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("scraping.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

# ==========================================
# 1. MODULE GOOGLE SHEETS
# ==========================================
def export_to_google_sheets(df, json_key_path, spreadsheet_name, sheet_name="Feuille 1"):
    logging.info(f"[GSHEETS] Connexion avec {json_key_path} pour l'onglet '{sheet_name}'...")
    if df.empty:
        logging.info("[GSHEETS] Le DataFrame est vide, annulation de l'export.")
        return
        
    df_export = df.copy()
    timestamp = pd.Timestamp.now().strftime("%d/%m/%Y %H:%M:%S")
    df_export.insert(0, 'Heure', timestamp)
        
    try:
        gc = gspread.service_account(filename=json_key_path)
        sh = gc.open(spreadsheet_name)
        
        try:
            worksheet = sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            logging.info(f"[GSHEETS] L'onglet '{sheet_name}' n'existe pas. Création en cours...")
            worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="20")
        
        # La colonne A contient l'horodatage de chaque ligne exportee.
        timestamps = worksheet.col_values(1)
        derniere_ligne = max(
            (i for i, value in enumerate(timestamps, start=1) if value.strip()),
            default=0,
        )
        est_vide = derniere_ligne == 0
        ligne_depart = derniere_ligne + 1
        if not est_vide:
            expected_header = df_export.columns.tolist()
            existing_header = worksheet.row_values(1)
            if existing_header != expected_header:
                legacy_header = [name for name in expected_header if name != "Cote Betclic"]
                if "Cote Betclic" in expected_header and existing_header == legacy_header:
                    worksheet.insert_cols([["Cote Betclic"]], col=expected_header.index("Cote Betclic") + 1)
                else:
                    raise ValueError(
                        f"Colonnes Google Sheets incompatibles dans {sheet_name} : {existing_header}"
                    )
        
        if est_vide:
            set_with_dataframe(worksheet, df_export, row=ligne_depart, include_column_header=True)
        else:
            set_with_dataframe(worksheet, df_export, row=ligne_depart, include_column_header=False)
            
        total_lignes = ligne_depart + len(df_export) - (1 if not est_vide else 0)
        
        logging.info("[GSHEETS] Application de la mise en forme et des filtres...")

        body = {
            "requests": [
                {"updateSheetProperties": {
                    "properties": {"sheetId": worksheet.id, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }},
                {"setBasicFilter": {"filter": {"range": {
                    "sheetId": worksheet.id, "startRowIndex": 0,
                    "endRowIndex": total_lignes, "startColumnIndex": 0,
                    "endColumnIndex": len(df_export.columns),
                }}}},
                {"autoResizeDimensions": {"dimensions": {
                    "sheetId": worksheet.id, "dimension": "COLUMNS",
                    "startIndex": 0, "endIndex": len(df_export.columns),
                }}},
            ]
        }

        for col_idx, col_name in enumerate(df_export.columns):
            if col_name in ["Cote PMU", "Cote Wina", "Cote Betclic"]:
                number_format = {"type": "NUMBER", "pattern": "0.00"}
            elif col_name in ["Ratio (PMU/Wina)", "Ratio (PMU/Betclic)"]:
                number_format = {"type": "PERCENT", "pattern": "0%"}
            else:
                continue
            body["requests"].append({"repeatCell": {
                "range": {
                    "sheetId": worksheet.id, "startRowIndex": 1,
                    "endRowIndex": total_lignes, "startColumnIndex": col_idx,
                    "endColumnIndex": col_idx + 1,
                },
                "cell": {"userEnteredFormat": {"numberFormat": number_format}},
                "fields": "userEnteredFormat.numberFormat",
            }})
        
        # NOUVEAU : Mise en forme conditionnelle par taille de combinaison
        ratio_columns = [
            df_export.columns.get_loc(name)
            for name in ("Ratio (PMU/Wina)", "Ratio (PMU/Betclic)")
            if name in df_export.columns
        ]
        if ratio_columns and 'Taille Combi' in df_export.columns:
            
            # Calcul de la vraie ligne de départ (index Google Sheets commence à 0)
            data_start_row = ligne_depart + (1 if est_vide else 0) - 1
            
            def create_gradient_request(start_r, end_r, col_idx):
                return {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{"sheetId": worksheet.id, "startRowIndex": start_r, "endRowIndex": end_r, "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1}],
                            "gradientRule": {
                                "minpoint": {"color": {"red": 0.9, "green": 0.5, "blue": 0.5}, "type": "MIN"},      # Rouge pastel
                                "midpoint": {"color": {"red": 1.0, "green": 1.0, "blue": 1.0}, "type": "PERCENTILE", "value": "50"}, # Blanc au milieu (lisibilité)
                                "maxpoint": {"color": {"red": 0.5, "green": 0.85, "blue": 0.5}, "type": "MAX"}     # Vert pastel
                            }
                        },
                        "index": 0 # Ajoute la règle en haut de la pile
                    }
                }
            
            group_start = 0
            current_taille = df_export.iloc[0]['Taille Combi'] if not df_export.empty else None
            
            for i in range(len(df_export)):
                taille = df_export.iloc[i]['Taille Combi']
                # Si on change de taille de combinaison, on clôture le groupe précédent et on crée son dégradé
                if taille != current_taille:
                    for ratio_col_index in ratio_columns:
                        body["requests"].append(create_gradient_request(data_start_row + group_start, data_start_row + i, ratio_col_index))
                    current_taille = taille
                    group_start = i
            
            # N'oublions pas le dernier groupe de la boucle
            if not df_export.empty:
                for ratio_col_index in ratio_columns:
                    body["requests"].append(create_gradient_request(data_start_row + group_start, data_start_row + len(df_export), ratio_col_index))

        # Envoi de la requête globale (mise à l'échelle + tous les dégradés)
        sh.batch_update(body)

        logging.info(f"[GSHEETS] Succès : {len(df_export)} lignes ajoutées à '{spreadsheet_name} -> {sheet_name}'.")
        
    except Exception:
        logging.exception("[GSHEETS] Echec de l'exportation")
        raise


# ==========================================
# 2. MODULE PMU (FONCTIONS DE BASE)
# ==========================================
PMU_TIMEOUT = (5, 20)


def create_pmu_session():
    """Une session par match, sans partage entre les threads."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    retry = Retry(
        total=2, backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def get_pmu_players_dict(event_id, market_label, session=None):
    url = f"https://eu.offering-api.kambicdn.com/offering/v2018/pmusportsfr/betoffer/event/{event_id}.json"
    params = {"lang": "fr_MC", "market": "FR", "client_id": "200", "channel_id": "1", "includeParticipants": "true"}
    headers = {"User-Agent": "Mozilla/5.0"}

    client = session if session is not None else requests
    response = client.get(url, params=params, headers=headers, timeout=PMU_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    
    teams_mapping = {}
    teams_order = {}
    for event in data.get("events", []):
        for participant in event.get("participants", []):
            if participant.get("participantType") == "TEAM":
                teams_mapping[participant.get("participantId")] = participant.get("name")
                teams_order[participant.get("participantId")] = int(participant.get("home"))
    events = data.get("events", [])
    if not events:
        logging.warning("[%s] Aucun evenement dans la reponse PMU", event_id)
        return pd.DataFrame()
    match_name = events[0]['name']
    
    players_dict = {}
    for offer in data.get("betOffers", []):
        if offer.get("criterion", {}).get("label", "") == market_label:
            for outcome in offer.get("outcomes", []):
                cote_solo = outcome.get("odds", 0) / 1000 
                player_name = outcome.get("participant")
                outcome_id = outcome.get("id")
                team_id = outcome.get("eventParticipantId")
                
                if player_name and outcome_id:
                    players_dict[player_name] = {
                        "Match": match_name,
                        "Equipe": teams_mapping.get(team_id),
                        "home": teams_order.get(team_id),
                        "Taille Combi": 1,
                        "Joueurs": player_name,
                        "outcome_id": str(outcome_id),
                        "Cote PMU": cote_solo
                    }
          
    players_data = pd.DataFrame.from_dict(players_dict, orient='index')
    if not players_data.empty:
        players_data = players_data.sort_values(['home', 'Cote PMU'], ascending=[False, True])
    return players_data

def get_pmu_combi_odds(event_id, outcome_ids_list, session=None, match_name=None):
    log_label = match_name or f"PMU {event_id}"
    outcomes_str = ",".join(outcome_ids_list)
    url = f"https://eu.offering-api.kambicdn.com/offering/v2018/pmusportsfr/onDemandPricing/event/{event_id}/outcome/{outcomes_str}.json"
    params = {"lang": "fr_MC", "market": "FR", "client_id": "200", "channel_id": "1", "ncid": int(time.time() * 1000)}
    
    try:
        client = session if session is not None else requests
        response = client.get(
            url, params=params, headers={"User-Agent": "Mozilla/5.0"},
            timeout=PMU_TIMEOUT,
        )
        response.raise_for_status()
        cote = response.json().get("selectedOdds", {}).get("decimal")
        if cote is None:
            logging.warning("[%s] Cote PMU absente pour %s", log_label, outcomes_str)
            return None
        if isinstance(cote, bool) or not isinstance(cote, (int, float)) or not np.isfinite(cote) or cote <= 0:
            raise ValueError(f"Cote PMU invalide : {cote!r}")
        return cote / 1000 if cote and cote > 100 else cote
    except (requests.RequestException, ValueError, TypeError) as exc:
        logging.warning("[%s] Echec cote PMU pour %s : %s", log_label, outcomes_str, exc)
        return None


# ==========================================
# 3. UTILITAIRES DE MATCHING
# ==========================================
def match_wina_players(df_pmu_simples, wina_players, name_match_score):
    """Priorite aux noms exacts ; une selection Wina ne peut etre reutilisee."""
    mapping, ratios, results = {}, {}, []
    for home in (1, 0):
        candidates = [p for p in wina_players if p["home"] == home]
        pmu_players = df_pmu_simples[df_pmu_simples["home"] == home].to_dict("records")
        matches = match_names([p["Joueurs"] for p in pmu_players],
                              [p["Nom"] for p in candidates], name_match_score, label="WINAMAX")
        for reference, (candidate, _) in matches.items():
            player = pmu_players[reference]
            name = player["Joueurs"]
            wina_name = candidates[candidate]["Nom"]
            odds = candidates[candidate]["Cote"]
            if not np.isfinite(odds) or odds <= 0:
                logging.warning('[WINA] Cote indisponible/invalide pour un joueur retenu : '
                                'PMU="%s" ; WINAMAX="%s" ; cote=%s', name, wina_name, odds)
                continue
            mapping[name] = wina_name
            ratios[name] = player["Cote PMU"] / odds
            results.append({"Taille Combi": 1, "Joueurs": name, "Cote Wina": odds})
    return mapping, ratios, results


def iter_pmu_combinations(df_pmu_simples, ratios, tailles, cote_out, ratio_min):
    """Conserve l'ordre domicile/exterieur puis tailles, y compris les joueurs PMU seuls."""
    if any(taille < 2 for taille in tailles):
        raise ValueError("Les tailles de combinaisons doivent etre >= 2")

    def generate():
        records = df_pmu_simples.to_dict("records")
        for home in (1, 0):
            eligible = [
                player for player in records
                if player["home"] == home and (
                    player["Cote PMU"] < cote_out
                    or ratios.get(player["Joueurs"], 0) >= ratio_min
                )
            ]
            for taille in tailles:
                yield from combinations(eligible, taille)

    return generate()


@contextmanager
def timed_stage(match_label, stage):
    started = time.perf_counter()
    try:
        yield
    finally:
        logging.info("[%s] Duree %s : %.2fs", match_label, stage, time.perf_counter() - started)


@contextmanager
def wina_page(headless):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            yield browser.new_page()
        finally:
            browser.close()


def extract_wina_players(tableaux):
    """Un instantane DOM par equipe ; indices conserves pour les clics."""
    buttons, players = {}, []
    for i in range(tableaux.count()):
        parent = tableaux.nth(i).locator("xpath=..")
        snapshot = parent.evaluate("""bloc => ({
            names: Array.from(bloc.querySelectorAll("div:has(> .label-line)"),
                              el => el.innerText.trim()),
            odds: Array.from(bloc.querySelectorAll(".odd-button-value"),
                             el => el.innerText.trim()),
            buttons: bloc.querySelectorAll(".odd-button-wrapper").length
        })""")
        for j, name in enumerate(snapshot["names"]):
            index = j * 3 + 2
            if index >= len(snapshot["odds"]) or index >= snapshot["buttons"]:
                raise ValueError(f"Structure Winamax inattendue pour {name}")
            try:
                odds = float(snapshot["odds"][index].replace(",", "."))
            except ValueError:
                odds = np.nan
            players.append({"home": 1 if i == 0 else 0, "Nom": name, "Cote": odds})
            if not np.isfinite(odds) or odds <= 0:
                logging.debug("[WINA] Cote indisponible/invalide pour %s", name)
                continue
            buttons[name] = parent.locator(".odd-button-wrapper").nth(index)
    return buttons, players


def expand_wina_team(tableau, team_index):
    parent = tableau.locator("xpath=..")
    rows = parent.locator("div:has(> .label-line)")
    button = parent.get_by_text("Plus de sélections", exact=True).first
    for attempt in range(3):
        if not button.count() or not button.is_visible():
            logging.info("[WINA] Equipe %s : %s joueurs affiches", team_index + 1, rows.count())
            return
        before = rows.count()
        try:
            button.evaluate("el => el.scrollIntoView({block: 'center'})", timeout=10000)
            button.click(timeout=10000)
            expect(rows).not_to_have_count(before, timeout=10000)
            logging.info("[WINA] Equipe %s : liste etendue de %s a %s joueurs",
                         team_index + 1, before, rows.count())
            if not button.count() or not button.is_visible():
                return
        except (PlaywrightError, AssertionError) as exc:
            logging.warning("[WINA] Equipe %s : ouverture tentative %s/3 echouee : %s",
                            team_index + 1, attempt + 1, exc)
    raise RuntimeError(f"Liste Winamax equipe {team_index + 1} non entierement ouverte")


def prepare_wina_market(page):
    page.get_by_text("Joueur décisif", exact=True).first.wait_for(timeout=10000)
    section = page.locator("div").filter(
        has=page.get_by_text("Joueur décisif", exact=True)
    ).filter(has=page.locator("div.bet-group-template")).last
    # Le marche est virtualise : attendre la stabilite pendant le scroll peut
    # detacher l'element. Le scroll DOM est effectue dans la meme evaluation.
    section.evaluate("el => el.scrollIntoView({block: 'center'})", timeout=10000)
    tableaux = section.locator("div.bet-group-template")
    tableaux.first.wait_for(state="visible", timeout=10000)
    time.sleep(0.5)
    page.evaluate("document.querySelectorAll('#tarteaucitronRoot, #warning-anj-risks').forEach(el => el.style.display = 'none');")
    for i in range(tableaux.count()):
        expand_wina_team(tableaux.nth(i), i)
    return tableaux


def recover_wina_market(page, required_names):
    """Reconstruit le marche et vide le panier sans effacer la session."""
    page.reload()
    tableaux = prepare_wina_market(page)
    buttons, _ = extract_wina_players(tableaux)
    missing = set(required_names) - buttons.keys()
    if missing:
        raise ValueError(f"Joueurs Winamax absents apres rechargement : {sorted(missing)}")
    for name in required_names:
        buttons[name].wait_for(state="visible", timeout=10000)
    clear_wina_basket(page)
    return tableaux, buttons


def clear_wina_basket(page):
    basket = page.locator("div[data-testid='sticky-wrap']")
    empty = basket.get_by_text("Ton panier est vide !", exact=True)
    if not empty.is_visible():
        basket.get_by_role("button", name="Effacer et continuer", exact=True).click(
            timeout=10000,
        )
    empty.wait_for(state="visible", timeout=10000)


def read_wina_combination(page, tableaux, names):
    if len(names) < 2 or len(set(names)) != len(names):
        raise ValueError(f"Combinaison Winamax invalide : {names}")
    clear_wina_basket(page)
    basket = page.locator("div[data-testid='sticky-wrap']")
    items = basket.locator('[data-testid^="basket-item-"]')
    # Un MyMatch est un seul item contenant une ligne par joueur.
    rows = items.locator("span").filter(
        has_text=re.compile(r"^Joueur décisif$"),
    ).locator("xpath=..")
    expected_rows = []
    for name in names:
        buttons, _ = extract_wina_players(tableaux)
        if name not in buttons:
            raise ValueError(f"Joueur Winamax absent : {name}")
        buttons[name].click(force=True, timeout=10000)
        expected_rows.append(re.compile(r"^Joueur décisif\s*(?:-\s*)?" + re.escape(name) + r"$"))
        expect(items).to_have_count(1, timeout=10000)
        expect(rows).to_have_text(expected_rows, timeout=10000)

    odds = None
    stable_reads = 0
    for _ in range(20):
        time.sleep(0.5)
        expect(items).to_have_count(1, timeout=10000)
        expect(rows).to_have_text(expected_rows, timeout=10000)
        # Le span anime contient aussi les valeurs intermediaires.
        value = items.locator(".odds-indicator-value > div").first
        if value.count() and value.is_visible():
            try:
                candidate = float(value.inner_text().strip().replace(",", "."))
            except ValueError:
                odds, stable_reads = None, 0
                continue
            if np.isfinite(candidate) and candidate > 0:
                stable_reads = stable_reads + 1 if candidate == odds else 1
                odds = candidate
                if stable_reads >= 3:
                    break
                continue
        odds, stable_reads = None, 0
    if odds is None or stable_reads < 3:
        raise ValueError(f"Cote Winamax indisponible pour {names}")

    clear_wina_basket(page)
    return odds


# ==========================================
# 4. MODULE MASTER (WINA + CRÉATION PMU COMBOS)
# ==========================================
def run_master_wina_and_pmu(url_match, event_id_pmu, df_pmu_simples, tailles_combi, cote_out, ratio_min, name_match_score=DEFAULT_MATCH_SCORE, headless=True, pmu_session=None):
    """
    Extrait les simples Wina puis filtre par cote PMU < cote_out OU ratio >= ratio_min.
    Genere les combinaisons a la demande sans changer l'ordre ni la couverture PMU.
    La session PMU fournie appartient a l'appelant ; sinon elle est fermee ici.
    """
    nom_match_log = df_pmu_simples.iloc[0]["Match"]
    logging.info("[%s] DÉMARRAGE WINAMAX & GÉNÉRATION CIBLÉE", nom_match_log)
    session_context = nullcontext(pmu_session) if pmu_session is not None else create_pmu_session()
    with session_context as session, wina_page(headless) as page:
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())
        page.goto(url_match)

        try: page.locator("button:has-text('Accepter')").first.click(timeout=2000)
        except: pass

        try:
            page.get_by_text("Joueur décisif", exact=True).first.wait_for(timeout=10000)
        except PlaywrightTimeoutError:
            logging.warning("[%s] Marche Joueur decisif indisponible sur Winamax", nom_match_log)
            return df_pmu_simples.drop(columns=['outcome_id']), pd.DataFrame()

        tableaux = prepare_wina_market(page)

        with timed_stage(nom_match_log, "extraction et matching Wina"):
            _, wina_simples_bruts = extract_wina_players(tableaux)
            mapping_pmu_to_wina, ratio_dict, df_wina_results = match_wina_players(
                df_pmu_simples, wina_simples_bruts, name_match_score,
            )

        valid_combos = iter_pmu_combinations(
            df_pmu_simples, ratio_dict, tailles_combi, cote_out, ratio_min,
        )

        # D. Récupération des cotes PMU sur ces combinaisons (API)
        pmu_combos_data = []
        with timed_stage(nom_match_log, "combinaisons PMU"):
            for combo in valid_combos:
                outcome_ids = [player["outcome_id"] for player in combo]
                cote_combi = get_pmu_combi_odds(
                    event_id_pmu, outcome_ids, session=session, match_name=nom_match_log,
                )
                cle_jointure = " - ".join(sorted(player["Joueurs"] for player in combo))

                pmu_combos_data.append({
                    "Match": combo[0]["Match"],
                    "Equipe": combo[0]["Equipe"],
                    "home": combo[0]["home"],
                    "Taille Combi": len(combo),
                    "Joueurs": cle_jointure,
                    "Cote PMU": cote_combi
                })
                time.sleep(0.2)
        logging.info("[%s] %s combinaisons PMU traitees", nom_match_log, len(pmu_combos_data))

        df_pmu_combos = pd.DataFrame(pmu_combos_data)
        
        # Le DataFrame PMU Master est maintenant complet (Simples + Combos filtrés)
        df_pmu_final = pd.concat([df_pmu_simples.drop(columns=['outcome_id']), df_pmu_combos], ignore_index=True)

        # E. Lancement du MyMatch Winamax sur ce pool restreint
        logging.info(f"[{nom_match_log}] Calcul des combinaisons ciblées sur Winamax...")
        wina_started = time.perf_counter()

        if not df_pmu_combos.empty:
            for i, ( _, row) in enumerate(df_pmu_combos.iterrows()):
                cle_universelle_pmu = row['Joueurs']
                logging.info(f"[{nom_match_log}] Computing {i+1} / {len(df_pmu_combos)} : {cle_universelle_pmu}")
                
                joueurs_pmu_list = cle_universelle_pmu.split(' - ')
                joueurs_wina_list = [mapping_pmu_to_wina.get(j) for j in joueurs_pmu_list]
                
                # Si l'un des joueurs n'a pas été matché (score thefuzz insuffisant), on saute
                if None in joueurs_wina_list: 
                    continue
                    
                for attempt in range(2):
                    try:
                        match_cote = read_wina_combination(page, tableaux, joueurs_wina_list)
                        df_wina_results.append({
                            "Taille Combi": len(joueurs_pmu_list), 
                            "Joueurs": cle_universelle_pmu, 
                            "Cote Wina": match_cote
                        })
                        break
                    except (PlaywrightError, ValueError, AssertionError) as exc:
                        logging.warning("[%s] Echec tentative %s/2 pour %s : %s",
                                        nom_match_log, attempt + 1, cle_universelle_pmu, exc)
                        if attempt == 1:
                            raise RuntimeError(
                                f"Winamax reste indisponible pour {cle_universelle_pmu}; arret du match"
                            ) from exc
                        tableaux, _ = recover_wina_market(page, joueurs_wina_list)

        logging.info("[%s] Duree combinaisons Wina : %.2fs",
                     nom_match_log, time.perf_counter() - wina_started)
    return df_pmu_final, pd.DataFrame(df_wina_results)


# ==========================================
# 5. MODULE BETCLIC
# ==========================================
def run_betclic_extraction(url_match, df_pmu, headless=True, name_match_score=DEFAULT_MATCH_SCORE):
    """Ajoute les simples et duos Betclic disponibles pour les joueurs PMU retenus."""
    logging.info("--- DEMARRAGE BETCLIC : %s ---", url_match)
    with wina_page(headless) as page:
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())
        page.goto(url_match, wait_until="domcontentloaded")
        players, duos = extract_betclic_markets(page)
    return build_betclic_results(players, duos, df_pmu, name_match_score)


# ==========================================
# 6. EXECUTION PRINCIPALE
# ==========================================

def process_single_match(event_pmu, event_wina, url_betclic, args, gsheet_credentials, delay,
                         flashscore_matches=None, flashscore_url=None):
    """Encapsule la logique d'un match avec un délai de démarrage anti-bot."""
    # Désynchronisation initiale pour éviter les pics de requêtes
    time.sleep(delay)
    match_label = f"PMU {event_pmu}"
    
    try:
        logging.info("[%s] Recuperation du match PMU...", match_label)
        url_wina_full = f"https://www.winamax.fr/paris-sportifs/match/{event_wina}"
        
        # 1. Base PMU
        with create_pmu_session() as session:
            started = time.perf_counter()
            try:
                df_pmu_simples = get_pmu_players_dict(
                    event_pmu, "Buteur ou passeur décisif", session=session,
                )
                if not df_pmu_simples.empty:
                    match_label = df_pmu_simples.iloc[0]["Match"]
            finally:
                logging.info("[%s] Duree simples PMU : %.2fs", match_label, time.perf_counter() - started)
            if df_pmu_simples.empty:
                logging.info("[%s] Fin : Pas de donnees PMU.", match_label)
                return

            if flashscore_url or flashscore_matches is not None or args.flashscore:
                nb_avant = len(df_pmu_simples)
                with timed_stage(match_label, "compositions Flashscore"):
                    lineups = get_match_lineups(
                        flashscore_matches, df_pmu_simples, args.headless,
                        allow_probable=args.flashscore, url=flashscore_url,
                    )
                    if lineups is not None:
                        df_pmu_simples = filter_players(df_pmu_simples, lineups)
                if lineups is not None:
                    logging.info("[%s] %s joueurs de champ conserves sur %s",
                                 match_label, len(df_pmu_simples), nb_avant)
            else:
                logging.warning("[%s] Aucune reference Flashscore fournie en mode manuel ; aucun filtre.",
                                match_label)

            with timed_stage(match_label, "pipeline Wina et combinaisons"):
                df_pmu_final, df_wina = run_master_wina_and_pmu(
                    url_wina_full, event_pmu, df_pmu_simples, args.tailles,
                    args.cote_out, args.ratio_min, args.name_match_score, args.headless,
                    pmu_session=session,
                )
        
        df_final = pd.merge(df_pmu_final, df_wina, on=['Taille Combi', 'Joueurs'], how='left') if not df_wina.empty else df_pmu_final.assign(**{'Cote Wina': np.nan})
            
        if pd.notna(url_betclic) and str(url_betclic).strip():
            with timed_stage(match_label, "extraction Betclic"):
                df_bc = run_betclic_extraction(str(url_betclic).strip(), df_final, args.headless,
                                              args.name_match_score)
            df_final = pd.merge(
                df_final, df_bc, on=['Taille Combi', 'Joueurs'], how='left', validate="one_to_one",
            ) if not df_bc.empty else df_final.assign(**{'Cote Betclic': np.nan})
        else:
            logging.info("[%s] URL Betclic absente : extraction ignoree.", match_label)
            df_final['Cote Betclic'] = np.nan
            
        # 4. Nettoyage et Export
        df_final['Ratio (PMU/Wina)'] = df_final['Cote PMU'] / df_final['Cote Wina']
        df_final['Ratio (PMU/Betclic)'] = df_final['Cote PMU'] / df_final['Cote Betclic']
        df_final = df_final.sort_values(by=['Taille Combi', 'home', 'Ratio (PMU/Wina)', 'Ratio (PMU/Betclic)'], ascending=[True, False, False, False])
        df_final = df_final.drop(columns=['home'], errors='ignore')
            
        with timed_stage(match_label, "export Google Sheets"):
            export_to_google_sheets(df_final, gsheet_credentials, GSHEET_NAME, df_final.iloc[0]['Match'])
        logging.info("[%s] Termine avec succes.", match_label)
        
    except Exception:
        logging.exception("[%s] ERREUR GLOBALE", match_label)
        raise
        
def parse_match_date(value):
    if value.lower() == "demain":
        return datetime.now(PARIS).date() + timedelta(days=1)
    try:
        if len(value) != 10 or value[4] != "-" or value[7] != "-":
            raise ValueError("format")
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Date attendue : demain ou AAAA-MM-JJ") from exc


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--tailles", type=int, nargs="+", default=[2, 3], help="Tailles des combos PMU")
    parser.add_argument("-f", "--file", type=str,
                        help="Mode manuel : CSV event_pmu;event_wina;url_betclic")
    parser.add_argument("-c", "--competition", choices=list(COMPETITIONS), nargs="+",
                        help="Competitions du jour : ldc, l1, pl (defaut : toutes)")
    parser.add_argument("-m", "--match", nargs="+",
                        help='Un ou plusieurs matchs entre guillemets, ex. "PSG - Marseille"')
    parser.add_argument("--refresh-matches", action="store_true",
                        help="Renouveler le cache des references des competitions selectionnees")
    parser.add_argument("--date", type=parse_match_date,
                        help="Date des matchs : demain ou AAAA-MM-JJ (defaut : aujourd'hui, Europe/Paris)")
    parser.add_argument("--cote-out", type=float, default=4.0)
    parser.add_argument("-r", "--ratio-min", type=float, default=1.15)
    parser.add_argument("-s", "--name-match-score", type=int, default=DEFAULT_MATCH_SCORE,
                        help="Seuil fuzzy commun Winamax/Betclic (0 a 100, defaut : 80)")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True,
                        help="Navigateur masque par defaut ; --no-headless pour l'afficher")
    parser.add_argument("--flashscore", action="store_true",
                        help="Accepter aussi les compositions probables (officielles seules par defaut)")
    
    args = parser.parse_args(argv)
    if any(taille < 2 for taille in args.tailles):
        parser.error("--tailles doit contenir des valeurs >= 2")
    if args.file and args.refresh_matches:
        parser.error("--refresh-matches ne s'applique pas au mode --file")
    if args.file and args.date is not None:
        parser.error("--date ne s'applique pas au mode --file")
    return args


def load_manual_matches(args):
    frame = pd.read_csv(args.file, sep=";")
    required = {"event_pmu", "event_wina", "url_betclic"}
    if not required.issubset(frame.columns):
        raise ValueError(f"CSV : colonnes obligatoires manquantes : {sorted(required - set(frame.columns))}")
    if args.competition and "competition" not in frame.columns:
        raise ValueError("Le filtre -c en mode CSV exige une colonne competition (ldc/l1/pl)")
    if args.match and not {"home", "away"}.issubset(frame.columns):
        raise ValueError("Le filtre -m en mode CSV exige les colonnes home et away")
    records = []
    for row in frame.fillna("").to_dict("records"):
        if args.competition and row["competition"] not in args.competition:
            continue
        for key in ("event_pmu", "event_wina"):
            value = str(row[key])
            if not value.isdecimal():
                # pandas peut convertir une colonne d'entiers en flottants si elle contient un vide.
                raise ValueError(f"CSV : {key} doit etre un identifiant entier positif : {value!r}")
            row[key] = int(value)
            if row[key] <= 0:
                raise ValueError(f"CSV : {key} doit etre positif")
        row["url_betclic"] = str(row["url_betclic"]).strip()
        row.setdefault("competition", "manuel")
        records.append(row)
    return select_matches(records, args.match)


def main(argv=None):
    args = parse_args(argv)
    configure_logging()
    if args.file:
        matches = load_manual_matches(args)
        need_calendar = args.flashscore and any(not r.get("flashscore_url") for r in matches)
        flashscore_matches = get_today_matches(
            args.headless, [COMPETITIONS[c]["flashscore"] for c in args.competition or COMPETITIONS],
        ) if need_calendar else None
    else:
        with create_pmu_session() as session:
            matches = get_daily_matches(
                list(dict.fromkeys(args.competition or COMPETITIONS)), session, args.headless,
                queries=args.match, refresh=args.refresh_matches, today=args.date,
            )
        flashscore_matches = None
    if not matches:
        logging.info("Aucun match a analyser dans le perimetre selectionne.")
        return
        
    GSHEET_CREDENTIALS = "credentials.json"
    
    # Paramètre anti-bot clé : 2 à 3 workers max pour ne pas alerter Datadome
    MAX_WORKERS = 2 
    
    logging.info(f"Lancement du traitement par lots ({len(matches)} matchs, {MAX_WORKERS} processus simultanés)...")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for index, row in enumerate(matches):
            jitter = index * random.uniform(3.0, 7.0)
            
            futures.append(executor.submit(
                process_single_match, 
                row["event_pmu"], row["event_wina"], row["url_betclic"],
                args, GSHEET_CREDENTIALS, jitter, flashscore_matches, row.get("flashscore_url") or None,
            ))
            
        failed = False
        for future in as_completed(futures):
            if future.exception() is not None:
                failed = True
                logging.error(f"Un thread a planté : {future.exception()}")
        if failed:
            raise SystemExit(1)


GSHEET_NAME = "Joueur décisif PMU/WINA/Betclic"


if __name__ == "__main__":
    main()