"""Compare les cotes PMU/Winamax/Betclic, puis exporte vers Google Sheets.

Sans --file, les matchs du jour sont decouverts dans ldc, l1 et pl (Europe/Paris).
--date demain ou --date AAAA-MM-JJ selectionne une autre date.
-c/--competition selectionne les competitions ; -m/--match cible des rencontres.
--file conserve le mode CSV manuel. Les references automatiques sont mises en cache
par jour dans .match-cache ; --refresh-matches renouvelle leur decouverte.
--scheduled reserve l'analyse aux 90 minutes avant le match, impose les compositions
officielles et memorise les exports reussis pour les passages periodiques suivants.
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
--no-headless affiche le navigateur.
Les durees par etape sont enregistrees dans scraping.log.
Winamax est lu par son socket.io (marche Joueur decisif puis GetOdds MyMatch),
depuis la page du match, sans cliquer dans le panier (voir winamax_odds.py).
Les cotes simples indisponibles ne declenchent un avertissement que pour les
joueurs rapproches des noms PMU retenus, apres le filtre des compositions.
En cas d'erreur du socket Winamax, la connexion est rouverte et le meme combo
retente une fois. Un second echec arrete le match plutot que poursuivre des lectures
douteuses. Une combinaison refusee par Winamax reste sans cote Wina.
Un marche ferme au coup d'envoi rend le match ignore plutot qu'en erreur.
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
import gspread
from gspread_dataframe import set_with_dataframe
from playwright.sync_api import sync_playwright
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import logging
from flashscore_lineups import get_today_matches, get_match_lineups, filter_players
from betclic_odds import extract_markets as extract_betclic_markets, build_results as build_betclic_results
from name_matching import DEFAULT_MATCH_SCORE, match_names
from match_discovery import COMPETITIONS, PARIS, get_daily_matches, select_matches, local_start
from scheduled_runs import ExportedMatches, SeenLineups, run_lock
from sheets_cleanup import cleanup_match_sheets, track_match_sheet
from winamax_odds import MyMatchRefused, WinamaxMarketClosed, WinamaxMyMatch, WinamaxSocketError


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("scraping.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

def export_execution(summary):
    header = ["Debut (Paris)", "Fin (Paris)", "Competitions", "Statut",
              "Matchs exportes", "Matchs ignores", "Matchs en erreur", "Detail"]
    book = gspread.service_account(filename="credentials.json").open(GSHEET_NAME)
    try:
        sheet = book.worksheet("Exécutions")
    except gspread.exceptions.WorksheetNotFound:
        sheet = book.add_worksheet(title="Exécutions", rows="1000", cols="11")
    existing_header = sheet.row_values(1)
    if existing_header and existing_header != header:
        raise ValueError("Colonnes incompatibles dans l'onglet Executions")
    if not existing_header:
        sheet.append_row(header, value_input_option="RAW")
        sheet.freeze(rows=1)
    row = [
        summary["start"], datetime.now(PARIS).strftime("%Y-%m-%d %H:%M:%S"),
        summary["competitions"], summary["status"], summary["exported"],
        summary["ignored"], summary["failed"], summary["detail"][:4000],
    ]
    if sheet.col_count < 11:
        sheet.resize(cols=11)
    sheet.update(
        range_name="J2:K10",
        values=[["Dernier passage", ""], *[[label, value] for label, value in zip(header, row)]],
        value_input_option="RAW",
    )
    if (summary["exported"] or summary["failed"] or summary.get("deferred")
            or summary["status"] == "Termine avec erreurs"):
        sheet.append_row(row, value_input_option="RAW", table_range="A:H")
        sheet.sort((1, "des"), (2, "des"), range="A2:H" + str(sheet.row_count))
    else:
        logging.info("[GSHEETS] Passage sans export ni erreur : dernier passage actualise uniquement.")

# ==========================================
# 1. MODULE GOOGLE SHEETS
# ==========================================
# Colonnes ajoutees apres coup : inserees (vides) dans les onglets plus anciens.
ADDED_COLUMNS = ("Compositions", "Cote Betclic")


def export_to_google_sheets(df, json_key_path, spreadsheet_name, sheet_name="Feuille 1",
                           match_start=None):
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
        
        created = False
        try:
            worksheet = sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            logging.info(f"[GSHEETS] L'onglet '{sheet_name}' n'existe pas. Création en cours...")
            worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="20")
            created = True
        
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
                missing = [name for name in expected_header
                           if name in ADDED_COLUMNS and name not in existing_header]
                if missing and existing_header == [n for n in expected_header if n not in missing]:
                    # De gauche a droite : chaque index attendu reste valide apres insertion.
                    for name in missing:
                        worksheet.insert_cols([[name]], col=expected_header.index(name) + 1)
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
        if match_start is not None:
            track_match_sheet(sh.id, worksheet.id, sheet_name, match_start, created)

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


LAST_CHANCE_MINUTES = 30


class OddsNotReady(Exception):
    """Les marches ne couvrent pas encore toutes les selections requises."""


class LineupsJustReleased(OddsNotReady):
    """Compositions officielles vues pour la premiere fois : cotes encore instables."""


def require_odds(frame, quotes, column):
    keys = ["Taille Combi", "Joueurs"]
    checked = frame[keys].merge(
        quotes[keys + [column]] if not quotes.empty else pd.DataFrame(columns=keys + [column]),
        on=keys, how="left", validate="one_to_one",
    )
    odds = checked[column]
    missing = checked.loc[~(np.isfinite(odds.astype(float)) & (odds > 1)), "Joueurs"]
    if not missing.empty:
        raise OddsNotReady(f"{column} : {len(missing)}/{len(frame)} selections indisponibles : "
                           + ", ".join(missing))


@contextmanager
def wina_page(headless):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            yield browser.new_page()
        finally:
            browser.close()


# Pause entre deux GetOdds : une reponse prend ~50 ms, inutile de marteler le socket.
WINA_QUOTE_DELAY = 0.2


def quote_wina_combination(market, outcome_ids, combo_label, match_label):
    """Cote MyMatch, None si Winamax refuse la combinaison.

    Une erreur de socket ou une reponse incoherente provoque une reconnexion et
    une seconde tentative ; un second echec arrete le match.
    """
    for attempt in range(2):
        try:
            return market.quote(outcome_ids)
        except MyMatchRefused as exc:
            logging.warning("[%s] %s : %s", match_label, combo_label, exc)
            return None
        except WinamaxMarketClosed as exc:
            raise OddsNotReady(str(exc)) from exc
        except (WinamaxSocketError, ValueError) as exc:
            logging.warning("[%s] Echec tentative %s/2 pour %s : %s",
                            match_label, attempt + 1, combo_label, exc)
            if attempt == 1:
                raise RuntimeError(
                    f"Winamax reste indisponible pour {combo_label}; arret du match"
                ) from exc
            try:
                market.reconnect()
            except WinamaxMarketClosed as closed:
                raise OddsNotReady(str(closed)) from closed
            except (WinamaxSocketError, ValueError) as recovery_error:
                raise RuntimeError(
                    f"Recuperation Winamax impossible pour {combo_label} ; "
                    "arret du match sans export"
                ) from recovery_error


# ==========================================
# 4. MODULE MASTER (WINA + CRÉATION PMU COMBOS)
# ==========================================
def run_master_wina_and_pmu(url_match, event_id_pmu, df_pmu_simples, tailles_combi, cote_out, ratio_min, name_match_score=DEFAULT_MATCH_SCORE, headless=True, pmu_session=None, require_complete=False):
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
        market = WinamaxMyMatch(page, url_match)

        with timed_stage(nom_match_log, "extraction et matching Wina"):
            market.open()
            wina_simples_bruts = market.read_players()
            if wina_simples_bruts is None:
                if require_complete:
                    raise OddsNotReady("Marche Joueur decisif indisponible sur Winamax")
                logging.warning("[%s] Marche Joueur decisif indisponible sur Winamax", nom_match_log)
                return df_pmu_simples.drop(columns=['outcome_id']), pd.DataFrame()
            wina_outcomes = {p["Nom"]: p["outcome_id"] for p in wina_simples_bruts}
            mapping_pmu_to_wina, ratio_dict, df_wina_results = match_wina_players(
                df_pmu_simples, wina_simples_bruts, name_match_score,
            )
            if require_complete:
                require_odds(df_pmu_simples, pd.DataFrame(df_wina_results), "Cote Wina")

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

        quoted = 0
        for cle_universelle_pmu in df_pmu_combos.get("Joueurs", []):
            joueurs_pmu_list = cle_universelle_pmu.split(' - ')
            joueurs_wina_list = [mapping_pmu_to_wina.get(j) for j in joueurs_pmu_list]

            # Si l'un des joueurs n'a pas été matché (score thefuzz insuffisant), on saute
            if None in joueurs_wina_list:
                continue

            match_cote = quote_wina_combination(
                market, [wina_outcomes[name] for name in joueurs_wina_list],
                cle_universelle_pmu, nom_match_log,
            )
            quoted += 1
            if match_cote is not None:
                df_wina_results.append({
                    "Taille Combi": len(joueurs_pmu_list),
                    "Joueurs": cle_universelle_pmu,
                    "Cote Wina": match_cote
                })
            time.sleep(WINA_QUOTE_DELAY)

        logging.info("[%s] Duree combinaisons Wina : %.2fs (%s combinaisons demandees)",
                     nom_match_log, time.perf_counter() - wina_started, quoted)
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
                         flashscore_matches=None, flashscore_url=None, match_start=None,
                         lineups_seen=True, probable=False):
    """Encapsule la logique d'un match avec un délai de démarrage anti-bot.

    probable : export planifie sur compositions probables, avant la fenetre officielle ;
    seul un marche Winamax absent le fait differer.
    """
    # Désynchronisation initiale pour éviter les pics de requêtes
    time.sleep(delay)
    match_label = f"PMU {event_pmu}"
    lineups = None

    try:
        official_only = args.official_lineups_only
        scheduled = getattr(args, "scheduled", False)
        has_betclic = pd.notna(url_betclic) and bool(str(url_betclic).strip())
        if official_only:
            if not flashscore_url or match_start is None:
                raise ValueError("--official-lineups-only exige flashscore_url et start pour chaque match")
            if local_start(match_start) <= datetime.now(PARIS):
                logging.info("[%s] Match ignore : coup d'envoi deja atteint.", match_label)
                return "ignored"
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
                return "ignored"

            if flashscore_url or flashscore_matches is not None or args.flashscore:
                nb_avant = len(df_pmu_simples)
                with timed_stage(match_label, "compositions Flashscore"):
                    lineups = get_match_lineups(
                        flashscore_matches, df_pmu_simples, args.headless,
                        allow_probable=probable or (args.flashscore and not official_only),
                        url=flashscore_url,
                    )
                    if lineups is None and probable:
                        logging.info("[%s] Match ignore : compositions probables non publiees.",
                                     match_label)
                        return "ignored"
                    if lineups is None and official_only:
                        logging.info("[%s] Match ignore : compositions officielles non annoncees.",
                                     match_label)
                        return "ignored"
                    if probable and lineups.kind != "probables":
                        logging.info("[%s] Match ignore : compositions officielles deja publiees ; "
                                     "export a 90 minutes du coup d'envoi.", match_label)
                        return "ignored"
                    if lineups is not None:
                        df_pmu_simples = filter_players(df_pmu_simples, lineups)
                if lineups is not None:
                    logging.info("[%s] %s joueurs de champ conserves sur %s",
                                 match_label, len(df_pmu_simples), nb_avant)
            else:
                logging.warning("[%s] Aucune reference Flashscore fournie en mode manuel ; aucun filtre.",
                                match_label)

            if official_only and local_start(match_start) <= datetime.now(PARIS):
                logging.info("[%s] Match ignore : coup d'envoi atteint pendant la verification.",
                             match_label)
                return "ignored"
            require_complete = scheduled and not probable and local_start(match_start) > (
                datetime.now(PARIS) + timedelta(minutes=LAST_CHANCE_MINUTES)
            )
            if require_complete and not lineups_seen:
                raise LineupsJustReleased(
                    "compositions officielles tout juste publiees ; attente d'un passage "
                    "pour laisser les cotes se stabiliser"
                )
            if require_complete:
                require_odds(df_pmu_simples, df_pmu_simples, "Cote PMU")
            elif scheduled and not probable:
                logging.info("[%s] Dernier passage avant le coup d'envoi : export meme si des cotes "
                             "simples PMU/Winamax manquent.", match_label)
            with timed_stage(match_label, "pipeline Wina et combinaisons"):
                df_pmu_final, df_wina = run_master_wina_and_pmu(
                    url_wina_full, event_pmu, df_pmu_simples, args.tailles,
                    args.cote_out, args.ratio_min, args.name_match_score, args.headless,
                    pmu_session=session,
                    **({"require_complete": True} if require_complete else {}),
                )
            if probable and df_wina.empty:
                raise OddsNotReady("Marche Joueur decisif Winamax pas encore ouvert")

        df_final = pd.merge(df_pmu_final, df_wina, on=['Taille Combi', 'Joueurs'], how='left') if not df_wina.empty else df_pmu_final.assign(**{'Cote Wina': np.nan})
            
        if has_betclic:
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
        df_final.insert(0, 'Compositions', lineups.kind if lineups is not None else "aucune")

        with timed_stage(match_label, "export Google Sheets"):
            export_to_google_sheets(
                df_final, gsheet_credentials, GSHEET_NAME, df_final.iloc[0]['Match'],
                **({"match_start": match_start} if match_start is not None else {}),
            )
        logging.info("[%s] Termine avec succes.", match_label)
        return "exported"
        
    except OddsNotReady as exc:
        if match_start is not None and local_start(match_start) <= datetime.now(PARIS):
            # Winamax ferme le marche au coup d'envoi : rien a differer.
            logging.info("[%s] Match ignore : coup d'envoi atteint pendant le traitement (%s).",
                         match_label, exc)
            return "ignored"
        logging.info("[%s] Match differe : %s ; nouvel essai au prochain passage avant le coup d'envoi.",
                     match_label, exc)
        raise
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
    parser.add_argument("--official-lineups-only", action="store_true",
                        help="Analyser seulement les matchs a venir avec compositions officielles completes")
    parser.add_argument("--scheduled", action="store_true",
                        help="Passage periodique : 90 minutes avant le match, compositions officielles, sans reexport")
    
    args = parser.parse_args(argv)
    if any(taille < 2 for taille in args.tailles):
        parser.error("--tailles doit contenir des valeurs >= 2")
    if args.file and args.refresh_matches:
        parser.error("--refresh-matches ne s'applique pas au mode --file")
    if args.file and args.date is not None:
        parser.error("--date ne s'applique pas au mode --file")
    if args.scheduled:
        if args.file or args.date is not None:
            parser.error("--scheduled exige les matchs automatiques du jour (sans --file ni --date)")
        args.official_lineups_only = True
    if args.official_lineups_only and args.flashscore:
        parser.error("--official-lineups-only est incompatible avec --flashscore (compositions probables)")
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
        if args.official_lineups_only:
            if not row.get("flashscore_url") or not row.get("start"):
                raise ValueError("--official-lineups-only exige les colonnes CSV flashscore_url et start remplies")
            local_start(row["start"])
        row.setdefault("competition", "manuel")
        records.append(row)
    return select_matches(records, args.match)


def run_matches(args, summary):
    exported_matches = ExportedMatches() if args.scheduled else None
    probable_exports = ExportedMatches(PROBABLE_EXPORTS) if args.scheduled else None
    seen_lineups = SeenLineups() if args.scheduled else None
    probable_events = set()
    discovery_issues = []
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
                issues=discovery_issues,
            )
        flashscore_matches = None
    if discovery_issues:
        summary["failed"] += len(discovery_issues)
        summary["detail"] += "".join(f"Erreur decouverte : {issue}\n" for issue in discovery_issues)
    if not matches:
        logging.info("Aucun match a analyser dans le perimetre selectionne.")
        if discovery_issues:
            raise SystemExit(1)
        return
    if args.official_lineups_only:
        now = datetime.now(PARIS)
        upcoming = []
        for row in matches:
            if not row.get("flashscore_url") or not row.get("start"):
                raise ValueError("--official-lineups-only exige flashscore_url et start pour chaque match")
            if local_start(row["start"]) <= now:
                summary["ignored"] += 1
                logging.info("[%s - %s] Match ignore : coup d'envoi deja atteint.",
                             row.get("home", row["event_pmu"]), row.get("away", ""))
            elif exported_matches is not None and exported_matches.contains(row["event_pmu"]):
                summary["ignored"] += 1
                logging.info("[PMU %s] Match ignore : deja exporte.", row["event_pmu"])
            elif args.scheduled and local_start(row["start"]) > now + timedelta(minutes=90):
                if probable_exports.contains(row["event_pmu"]):
                    summary["ignored"] += 1
                    logging.info("[PMU %s] Match ignore : compositions probables deja exportees, "
                                 "officielles attendues dans les 90 minutes.", row["event_pmu"])
                else:
                    probable_events.add(row["event_pmu"])
                    upcoming.append(row)
            else:
                upcoming.append(row)
        # Les exports officiels, proches du coup d'envoi, passent avant les probables.
        matches = sorted(upcoming, key=lambda row: row["event_pmu"] in probable_events)
        if not matches:
            logging.info("Aucun match a venir a analyser.")
            if discovery_issues:
                raise SystemExit(1)
            return
        
    GSHEET_CREDENTIALS = "credentials.json"
    
    # Paramètre anti-bot clé : 2 à 3 workers max pour ne pas alerter Datadome
    MAX_WORKERS = 2 
    
    logging.info(f"Lancement du traitement par lots ({len(matches)} matchs, {MAX_WORKERS} processus simultanés)...")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for index, row in enumerate(matches):
            jitter = index * random.uniform(3.0, 7.0)
            
            future = executor.submit(
                process_single_match, 
                row["event_pmu"], row["event_wina"], row["url_betclic"],
                args, GSHEET_CREDENTIALS, jitter, flashscore_matches, row.get("flashscore_url") or None,
                **({"match_start": row["start"]} if row.get("start") else {}),
                **({"lineups_seen": seen_lineups.contains(row["event_pmu"])}
                   if seen_lineups is not None else {}),
                **({"probable": True} if row["event_pmu"] in probable_events else {}),
            )
            futures[future] = row

        failed = bool(discovery_issues)
        for future in as_completed(futures):
            row = futures[future]
            probable = row["event_pmu"] in probable_events
            match_label = (f"{row['home']} - {row['away']}"
                           if row.get("home") and row.get("away")
                           else f"PMU {row['event_pmu']} / Winamax {row['event_wina']}")
            error = future.exception()
            if probable and isinstance(error, OddsNotReady):
                # Retente a chaque passage jusqu'a 90 minutes : pas de ligne d'historique.
                summary["ignored"] += 1
                continue
            if isinstance(error, LineupsJustReleased):
                try:
                    seen_lineups.mark_seen(row["event_pmu"], datetime.now(PARIS))
                except OSError as exc:
                    logging.warning("[PLANIFICATION] %s : compositions vues mais non memorisees : %s",
                                    match_label, exc)
            if isinstance(error, OddsNotReady):
                summary["ignored"] += 1
                summary["deferred"] = summary.get("deferred", 0) + 1
                summary["detail"] += f"Differe : {match_label} : {error}\n"
            elif error is not None:
                failed = True
                summary["failed"] += 1
                logging.error("[%s] Echec du traitement : %s", match_label, error)
                summary["detail"] += f"Erreur : {match_label} : {type(error).__name__}: {error}\n"
            else:
                result = future.result()
                if result not in ("exported", "ignored"):
                    raise ValueError(f"Resultat de traitement inattendu : {result!r}")
                summary[result] += 1
                if result == "exported":
                    summary["detail"] += (f"Exporte (probables) : {match_label}\n" if probable
                                          else f"Exporte : {match_label}\n")
                if result == "exported" and exported_matches is not None:
                    try:
                        (probable_exports if probable else exported_matches).mark_exported(
                            futures[future]["event_pmu"],
                        )
                    except OSError as exc:
                        failed = True
                        summary["failed"] += 1
                        detail = f"{match_label} : export effectue mais non memorise : {exc}"
                        summary["detail"] += f"{detail}\n"
                        logging.exception("[PLANIFICATION] %s ; risque de doublon au prochain passage.", detail)
        if failed:
            raise SystemExit(1)

def main(argv=None):
    args = parse_args(argv)
    configure_logging()
    with run_lock() as acquired:
        if acquired:
            execute_run(args)


def execute_run(args):
    summary = {
        "start": datetime.now(PARIS).strftime("%Y-%m-%d %H:%M:%S"),
        "competitions": ", ".join(args.competition or (["CSV manuel"] if args.file else COMPETITIONS)),
        "status": "Termine", "exported": 0, "ignored": 0, "failed": 0, "deferred": 0, "detail": "",
    }
    try:
        if args.scheduled:
            cleanup_match_sheets(
                lambda: gspread.service_account(filename="credentials.json").open(GSHEET_NAME),
                datetime.now(PARIS).date(),
            )
        run_matches(args, summary)
    except (Exception, SystemExit) as exc:
        summary["status"] = "Termine avec erreurs"
        if not isinstance(exc, SystemExit) or not summary["detail"]:
            summary["detail"] += f"{type(exc).__name__}: {exc}\n"
        raise
    finally:
        if summary["status"] == "Termine" and not summary["exported"]:
            summary["status"] = "Aucun match exporte"
        try:
            export_execution(summary)
        except Exception:
            logging.exception("[GSHEETS] Echec d'enregistrement du bilan d'execution")
            if summary["status"] != "Termine avec erreurs":
                raise


GSHEET_NAME = "Joueur décisif PMU/WINA/Betclic"
PROBABLE_EXPORTS = "exported-probables.json"


if __name__ == "__main__":
    main()