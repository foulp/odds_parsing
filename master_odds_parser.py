"""Compare les cotes PMU/Winamax, puis ajoute les resultats a Google Sheets.

Les matchs sont lus dans matchs.csv par defaut (--file pour un autre CSV).
--flashscore filtre sur les 10 joueurs de champ des compositions du jour.
Sans --flashscore, aucun filtre de titulaires ; les listes CSV sont ignorees.
Toutes les combinaisons eligibles sont generees dans l'ordre domicile/exterieur puis tailles.
Les combinaisons PMU sans correspondance Winamax restent exportees.
--no-headless affiche le navigateur ; les delais MyMatch restent conservateurs.
Les durees par etape sont enregistrees dans scraping.log.
L'ouverture des listes Winamax est verifiee par l'ajout effectif de lignes joueurs.
En cas d'erreur DOM Winamax, le marche est reconstruit et le meme combo retente
une fois. Un second echec arrete le match plutot que poursuivre des lectures douteuses.
"""

import argparse
import requests
import time
import numpy as np
import pandas as pd
from contextlib import contextmanager, nullcontext
from itertools import combinations
import unidecode
from thefuzz import process
from concurrent.futures import ThreadPoolExecutor, as_completed
import random
import gspread
from gspread_dataframe import set_with_dataframe
from playwright.sync_api import sync_playwright, expect, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import logging
from flashscore_lineups import get_today_matches, get_match_lineups, filter_players


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
    logging.info(f"\n[GSHEETS] Connexion avec {json_key_path} pour l'onglet '{sheet_name}'...")
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
            elif col_name == "Ratio (PMU/Wina)":
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
        if 'Ratio (PMU/Wina)' in df_export.columns and 'Taille Combi' in df_export.columns:
            ratio_col_index = df_export.columns.get_loc('Ratio (PMU/Wina)')
            
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
                    body["requests"].append(create_gradient_request(data_start_row + group_start, data_start_row + i, ratio_col_index))
                    current_taille = taille
                    group_start = i
            
            # N'oublions pas le dernier groupe de la boucle
            if not df_export.empty:
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

def get_pmu_combi_odds(event_id, outcome_ids_list, session=None):
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
            logging.warning("[%s] Cote PMU absente pour %s", event_id, outcomes_str)
            return None
        if isinstance(cote, bool) or not isinstance(cote, (int, float)) or not np.isfinite(cote) or cote <= 0:
            raise ValueError(f"Cote PMU invalide : {cote!r}")
        return cote / 1000 if cote and cote > 100 else cote
    except (requests.RequestException, ValueError, TypeError) as exc:
        logging.warning("[%s] Echec cote PMU pour %s : %s", event_id, outcomes_str, exc)
        return None


# ==========================================
# 3. UTILITAIRES DE MATCHING
# ==========================================
def nettoyer_nom(nom):
    return unidecode.unidecode(str(nom)).lower().strip()


def match_wina_players(df_pmu_simples, wina_players, name_match_score):
    """Priorite aux noms exacts ; une selection Wina ne peut etre reutilisee."""
    mapping, ratios, results = {}, {}, []
    for home in (1, 0):
        candidates = {p["Nom"]: p for p in wina_players if p["home"] == home}
        clean_names = {name: nettoyer_nom(name) for name in candidates}
        pmu_players = df_pmu_simples[df_pmu_simples["home"] == home].to_dict("records")
        pmu_players.sort(key=lambda p: nettoyer_nom(p["Joueurs"]) not in clean_names.values())
        used = set()
        for player in pmu_players:
            name = player["Joueurs"]
            match = process.extractOne(nettoyer_nom(name), clean_names)
            if match is None:
                logging.info("[ABSENT] PMU: %s introuvable sur Winamax", name)
                continue
            _, score, wina_name = match
            if score < name_match_score:
                logging.log(logging.WARNING if score >= 65 else logging.INFO,
                            "[MATCH REFUSE] %s / %s (score %s)", name, wina_name, score)
                continue
            if wina_name in used:
                logging.warning("[MATCH AMBIGU] %s : %s deja attribue", name, wina_name)
                continue
            odds = candidates[wina_name]["Cote"]
            if not np.isfinite(odds) or odds <= 0:
                logging.warning("[COTE INVALIDE] Winamax: %s / %s", wina_name, odds)
                continue
            used.add(wina_name)
            mapping[name] = wina_name
            ratios[name] = player["Cote PMU"] / odds
            results.append({"Taille Combi": 1, "Joueurs": name, "Cote Wina": odds})
            logging.info("[MATCH OK] %s / %s (score %s)", name, wina_name, score)
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
def timed_stage(event_id, stage):
    started = time.perf_counter()
    try:
        yield
    finally:
        logging.info("[%s] Duree %s : %.2fs", event_id, stage, time.perf_counter() - started)


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
                logging.warning("[WINA] Cote indisponible pour %s", name)
                continue
            players.append({"home": 1 if i == 0 else 0, "Nom": name, "Cote": odds})
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
    # Les indices peuvent changer lors des mises a jour du marche.
    buttons, _ = extract_wina_players(tableaux)
    missing = set(names) - buttons.keys()
    if missing:
        raise ValueError(f"Joueurs Winamax absents : {sorted(missing)}")
    basket = page.locator("div[data-testid='sticky-wrap']")
    for name in names:
        buttons[name].click(force=True, timeout=10000)
        time.sleep(0.3)

    odds = None
    for _ in range(5):
        time.sleep(0.5)
        # Le span anime contient aussi les valeurs intermediaires.
        value = basket.locator(".odds-indicator-value > div").first
        if value.count() and value.is_visible():
            try:
                candidate = float(value.inner_text().strip().replace(",", "."))
            except ValueError:
                continue
            if np.isfinite(candidate) and candidate > 0:
                odds = candidate
                break
    if odds is None:
        raise ValueError(f"Cote Winamax indisponible pour {names}")

    clear_wina_basket(page)
    return odds


# ==========================================
# 4. MODULE MASTER (WINA + CRÉATION PMU COMBOS)
# ==========================================
def run_master_wina_and_pmu(url_match, event_id_pmu, df_pmu_simples, tailles_combi, cote_out, ratio_min, name_match_score=80, headless=True, pmu_session=None):
    """
    Extrait les simples Wina puis filtre par cote PMU < cote_out OU ratio >= ratio_min.
    Genere les combinaisons a la demande sans changer l'ordre ni la couverture PMU.
    La session PMU fournie appartient a l'appelant ; sinon elle est fermee ici.
    """
    logging.info(f"\n--- DÉMARRAGE WINAMAX & GÉNÉRATION CIBLÉE ---")
    session_context = nullcontext(pmu_session) if pmu_session is not None else create_pmu_session()
    with session_context as session, wina_page(headless) as page:
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())
        page.goto(url_match)

        try: page.locator("button:has-text('Accepter')").first.click(timeout=2000)
        except: pass

        try:
            page.get_by_text("Joueur décisif", exact=True).first.wait_for(timeout=10000)
        except PlaywrightTimeoutError:
            logging.warning("[%s] Marche Joueur decisif indisponible sur Winamax", event_id_pmu)
            return df_pmu_simples.drop(columns=['outcome_id']), pd.DataFrame()

        tableaux = prepare_wina_market(page)

        with timed_stage(event_id_pmu, "extraction et matching Wina"):
            _, wina_simples_bruts = extract_wina_players(tableaux)
            mapping_pmu_to_wina, ratio_dict, df_wina_results = match_wina_players(
                df_pmu_simples, wina_simples_bruts, name_match_score,
            )

        valid_combos = iter_pmu_combinations(
            df_pmu_simples, ratio_dict, tailles_combi, cote_out, ratio_min,
        )

        # D. Récupération des cotes PMU sur ces combinaisons (API)
        pmu_combos_data = []
        with timed_stage(event_id_pmu, "combinaisons PMU"):
            for combo in valid_combos:
                outcome_ids = [player["outcome_id"] for player in combo]
                cote_combi = get_pmu_combi_odds(event_id_pmu, outcome_ids, session=session)
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
        logging.info("[%s] %s combinaisons PMU traitees", event_id_pmu, len(pmu_combos_data))

        df_pmu_combos = pd.DataFrame(pmu_combos_data)
        
        # Le DataFrame PMU Master est maintenant complet (Simples + Combos filtrés)
        df_pmu_final = pd.concat([df_pmu_simples.drop(columns=['outcome_id']), df_pmu_combos], ignore_index=True)

        # E. Lancement du MyMatch Winamax sur ce pool restreint
        nom_match_log = df_pmu_simples.iloc[0]['Match']
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
                    except (PlaywrightError, ValueError) as exc:
                        logging.warning("[%s] Echec tentative %s/2 pour %s : %s",
                                        nom_match_log, attempt + 1, cle_universelle_pmu, exc)
                        if attempt == 1:
                            raise RuntimeError(
                                f"Winamax reste indisponible pour {cle_universelle_pmu}; arret du match"
                            ) from exc
                        tableaux, _ = recover_wina_market(page, joueurs_wina_list)

        logging.info("[%s] Duree combinaisons Wina : %.2fs",
                     event_id_pmu, time.perf_counter() - wina_started)
    return df_pmu_final, pd.DataFrame(df_wina_results)


# ==========================================
# 5. MODULE BETCLIC
# ==========================================
def run_betclic_extraction(url_match, df_pmu, headless=True):
    logging.info(f"\n--- DÉMARRAGE BETCLIC ---")
    df_bc_results = []
    mapping_bc_to_pmu = {} 

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())
        page.goto(url_match)

        try: page.locator("#popin_tc_privacy_button_2").first.click(timeout=3000)
        except: pass
        try: page.locator("sports-extra-win-onboarding-modal button:has-text('Fermer')").first.click(timeout=2000)
        except: pass

        # Extraction Simples
        nom_marche_simples = "Joueur décisif (buteur ou passeur décisif) + son remplaçant (t. rég)"
        section_simples = page.locator("div.marketBox").filter(has=page.locator("h2", has_text=nom_marche_simples)).last
        bc_simples_bruts = []

        if section_simples.count() > 0:
            for b in section_simples.locator("button.is-seeMore").all():
                try: b.click(); time.sleep(0.5)
                except: pass
            
            blocs_equipes = section_simples.locator("div.marketBox_body")
            for i in range(blocs_equipes.count()):
                is_home = 1 if i == 0 else 0
                lignes = blocs_equipes.nth(i).locator("div.marketBox_lineSelection:has(p.marketBox_label)")
                for j in range(lignes.count()):
                    nom = lignes.nth(j).locator("p.marketBox_label").inner_text().strip()
                    cote = float(lignes.nth(j).locator("div.marketBox_item").first.locator(".btn_label").inner_text().replace(',', '.'))
                    bc_simples_bruts.append({"home": is_home, "Nom": nom, "Cote": cote})

        # Matching Simples vers PMU
        for home_status in [1, 0]:
            pmu_names = df_pmu[(df_pmu['home'] == home_status) & (df_pmu['Taille Combi'] == 1)]['Joueurs'].tolist()
            pmu_clean = [nettoyer_nom(n) for n in pmu_names]
            
            for bc_joueur in [x for x in bc_simples_bruts if x["home"] == home_status]:
                best_match, score = process.extractOne(nettoyer_nom(bc_joueur["Nom"]), pmu_clean)
                pmu_original = pmu_names[pmu_clean.index(best_match)]
                if score >= 90:
                    logging.info(f"{bc_joueur['Nom']}({nettoyer_nom(bc_joueur["Nom"])}) matched {pmu_original}({best_match}) with score of {score}")
                    mapping_bc_to_pmu[bc_joueur["Nom"]] = pmu_original
                    df_bc_results.append({"Taille Combi": 1, "Joueurs": pmu_original, "Cote Betclic": bc_joueur["Cote"]})
                else:
                    logging.info(f"WARNING! {bc_joueur['Nom']}({nettoyer_nom(bc_joueur["Nom"])}) matched {pmu_original}({best_match}) with score of {score}")

        # Extraction Duos
        try:
            page.get_by_text("Joueurs", exact=True).first.click(timeout=3000)
            page.get_by_text("Duos & Trios", exact=True).first.click(timeout=3000)
            time.sleep(1)
        except: pass
        
        section_combos = page.locator("div.marketBox").filter(has=page.locator("h2", has_text="Les deux joueurs sont décisifs")).last
        if section_combos.count() > 0:
            if section_combos.locator("button.is-seeMore").count() > 0:
                try: section_combos.locator("button.is-seeMore").first.click(); time.sleep(1)
                except: pass

            lignes_combos = section_combos.locator("div.marketBox_lineSelection")
            for i in range(lignes_combos.count()):
                noms_duo = lignes_combos.nth(i).locator("p.marketBox_label").inner_text().strip()
                cote_str = lignes_combos.nth(i).locator(".btn_label").inner_text().strip()
                
                joueurs_abreges = [j.strip() for j in noms_duo.split(" & ")]
                if len(joueurs_abreges) == 2:
                    j1_bc, j2_bc = joueurs_abreges[0], joueurs_abreges[1]
                    
                    bc_noms_connus = list(mapping_bc_to_pmu.keys())
                    j1_complet, score1 = process.extractOne(j1_bc, bc_noms_connus)
                    j2_complet, score2 = process.extractOne(j2_bc, bc_noms_connus)
                    
                    if score1 >= 60 and score2 >= 60:
                        pmu_ref_1 = mapping_bc_to_pmu[j1_complet]
                        pmu_ref_2 = mapping_bc_to_pmu[j2_complet]
                        
                        cle_combo_pmu = " - ".join(sorted([pmu_ref_1, pmu_ref_2]))
                        df_bc_results.append({"Taille Combi": 2, "Joueurs": cle_combo_pmu, "Cote Betclic": float(cote_str.replace(',', '.'))})

        browser.close()
    return pd.DataFrame(df_bc_results)


# ==========================================
# 6. EXECUTION PRINCIPALE
# ==========================================

def process_single_match(event_pmu, event_wina, url_betclic, args, gsheet_credentials, delay, flashscore_matches=None):
    """Encapsule la logique d'un match avec un délai de démarrage anti-bot."""
    # Désynchronisation initiale pour éviter les pics de requêtes
    time.sleep(delay)
    
    try:
        logging.info(f"[{event_pmu}] Démarrage du traitement...")
        url_wina_full = f"https://www.winamax.fr/paris-sportifs/match/{event_wina}"
        
        # 1. Base PMU
        with create_pmu_session() as session:
            with timed_stage(event_pmu, "simples PMU"):
                df_pmu_simples = get_pmu_players_dict(
                    event_pmu, "Buteur ou passeur décisif", session=session,
                )
            if df_pmu_simples.empty:
                logging.info(f"[{event_pmu}] Fin : Pas de données PMU.")
                return

            if args.flashscore:
                nb_avant = len(df_pmu_simples)
                with timed_stage(event_pmu, "compositions Flashscore"):
                    lineups = get_match_lineups(flashscore_matches, df_pmu_simples, args.headless)
                    df_pmu_simples = filter_players(df_pmu_simples, lineups)
                logging.info("[%s] %s joueurs de champ conserves sur %s",
                             event_pmu, len(df_pmu_simples), nb_avant)
            else:
                logging.info("[%s] Aucun filtre de titulaires applique.", event_pmu)

            with timed_stage(event_pmu, "pipeline Wina et combinaisons"):
                df_pmu_final, df_wina = run_master_wina_and_pmu(
                    url_wina_full, event_pmu, df_pmu_simples, args.tailles,
                    args.cote_out, args.ratio_min, args.name_match_score, args.headless,
                    pmu_session=session,
                )
        
        df_final = pd.merge(df_pmu_final, df_wina, on=['Taille Combi', 'Joueurs'], how='left') if not df_wina.empty else df_pmu_final.assign(**{'Cote Wina': np.nan})
            
        # 3. Betclic
        # df_bc = run_betclic_extraction(url_betclic, df_final)
        # df_final = pd.merge(df_final, df_bc, on=['Taille Combi', 'Joueurs'], how='left') if not df_bc.empty else df_final.assign(**{'Cote Betclic': np.nan})
            
        # 4. Nettoyage et Export
        df_final['Ratio (PMU/Wina)'] = df_final['Cote PMU'] / df_final['Cote Wina']
        df_final['Ratio (PMU/Betclic)'] = np.nan  # df_final['Cote PMU'] / df_final['Cote Betclic']
        df_final = df_final.sort_values(by=['Taille Combi', 'home', 'Ratio (PMU/Wina)', 'Ratio (PMU/Betclic)'], ascending=[True, False, False, False])
        df_final = df_final.drop(columns=['home'], errors='ignore')
            
        with timed_stage(event_pmu, "export Google Sheets"):
            export_to_google_sheets(df_final, gsheet_credentials, GSHEET_NAME, df_final.iloc[0]['Match'])
        logging.info(f"[{event_pmu}] Terminé avec succès.")
        
    except Exception:
        logging.exception("[%s] ERREUR GLOBALE", event_pmu)
        raise
        
def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--tailles", type=int, nargs="+", default=[2, 3], help="Tailles des combos PMU")
    parser.add_argument("-f", "--file", type=str, default="matchs.csv",
                        help="CSV des matchs (defaut : matchs.csv)")
    parser.add_argument("-c", "--cote-out", type=float, default=4.0)
    parser.add_argument("-r", "--ratio-min", type=float, default=1.15)
    parser.add_argument("-s", "--name-match-score", type=int, default=80)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True,
                        help="Navigateur masque par defaut ; --no-headless pour l'afficher")
    parser.add_argument("--flashscore", action="store_true",
                        help="Filtrer sur les 10 joueurs de champ des compos Flashscore du jour")
    
    args = parser.parse_args(argv)
    if any(taille < 2 for taille in args.tailles):
        parser.error("--tailles doit contenir des valeurs >= 2")
    return args


if __name__ == "__main__":
    args = parse_args()
    configure_logging()
    matches = pd.read_csv(args.file, sep=";").values
    flashscore_matches = get_today_matches(args.headless) if args.flashscore else None
        
    GSHEET_CREDENTIALS = "credentials.json"
    GSHEET_NAME = "Joueur décisif PMU/WINA/Betclic"
    
    # Paramètre anti-bot clé : 2 à 3 workers max pour ne pas alerter Datadome
    MAX_WORKERS = 2 
    
    logging.info(f"Lancement du traitement par lots ({len(matches)} matchs, {MAX_WORKERS} processus simultanés)...")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for index, row in enumerate(matches.tolist() if isinstance(matches, np.ndarray) else matches):
            event_pmu = int(row[0])
            event_wina = int(row[1])
            url_betclic = str(row[2]).strip()
            
            jitter = index * random.uniform(3.0, 7.0)
            
            futures.append(executor.submit(
                process_single_match, 
                event_pmu, event_wina, url_betclic,
                args, GSHEET_CREDENTIALS, jitter, flashscore_matches,
            ))
            
        failed = False
        for future in as_completed(futures):
            if future.exception() is not None:
                failed = True
                logging.error(f"Un thread a planté : {future.exception()}")
        if failed:
            raise SystemExit(1)