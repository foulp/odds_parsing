import argparse
import requests
import time
import re
import numpy as np
import pandas as pd
from itertools import combinations
import unidecode
from thefuzz import process

import gspread
from gspread_dataframe import set_with_dataframe
from gspread.utils import rowcol_to_a1
from playwright.sync_api import sync_playwright

# ==========================================
# 1. MODULE GOOGLE SHEETS
# ==========================================
def export_to_google_sheets(df, json_key_path, spreadsheet_name, sheet_name="Feuille 1"):
    print(f"\n[GSHEETS] Connexion avec {json_key_path}...")
    if df.empty:
        print("[GSHEETS] Le DataFrame est vide, annulation de l'export.")
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
            print(f"[GSHEETS] L'onglet '{sheet_name}' n'existe pas. Création en cours...")
            worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="20")
        
        donnees_existantes = worksheet.get_all_values()
        est_vide = len(donnees_existantes) == 0
        ligne_depart = len(donnees_existantes) + 1 if not est_vide else 1
        
        if est_vide:
            set_with_dataframe(worksheet, df_export, row=ligne_depart, include_column_header=True)
        else:
            set_with_dataframe(worksheet, df_export, row=ligne_depart, include_column_header=False)
            
        total_lignes = ligne_depart + len(df_export) - (1 if not est_vide else 0)
        print("[GSHEETS] Application de la mise en forme et des filtres...")

        worksheet.freeze(rows=1)

        try:
            worksheet.clear_basic_filter() 
        except Exception:
            pass
            
        lettre_max = rowcol_to_a1(1, len(df_export.columns))[:-1]
        plage_totale = f"A1:{lettre_max}{total_lignes}"
        worksheet.set_basic_filter(plage_totale)

        # Formatage des nombres
        for col_idx, col_name in enumerate(df_export.columns):
            col_lettre = rowcol_to_a1(1, col_idx + 1)[:-1]
            plage_colonne = f"{col_lettre}2:{col_lettre}{total_lignes}"

            if col_name in ["Cote PMU", "Cote Wina", "Cote Betclic"]:
                worksheet.format(plage_colonne, {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}})

        # Ajustement des colonnes
        body = {
            "requests": [{"autoResizeDimensions": {"dimensions": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": len(df_export.columns)}}}]
        }
        sh.batch_update(body)

        print(f"[GSHEETS] Succès : {len(df_export)} lignes ajoutées à '{spreadsheet_name} -> {sheet_name}'.")
        
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"[ERREUR] Le fichier '{spreadsheet_name}' est introuvable.")
    except Exception as e:
        print(f"[ERREUR] Échec de l'exportation : {type(e).__name__} - {e}")


# ==========================================
# 2. MODULE PMU (LA BASE DE RÉFÉRENCE)
# ==========================================
def get_pmu_players_dict(event_id, market_label):
    url = f"https://eu.offering-api.kambicdn.com/offering/v2018/pmusportsfr/betoffer/event/{event_id}.json"
    params = {"lang": "fr_MC", "market": "FR", "client_id": "200", "channel_id": "1", "includeParticipants": "true"}
    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(url, params=params, headers=headers)
    response.raise_for_status()
    data = response.json()
    
    teams_mapping = {}
    teams_order = {}
    for event in data.get("events", []):
        for participant in event.get("participants", []):
            if participant.get("participantType") == "TEAM":
                teams_mapping[participant.get("participantId")] = participant.get("name")
                teams_order[participant.get("participantId")] = int(participant.get("home"))
    match_name = data.get('events')[0]['name']
    
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

def get_pmu_combi_odds(event_id, outcome_ids_list):
    outcomes_str = ",".join(outcome_ids_list)
    url = f"https://eu.offering-api.kambicdn.com/offering/v2018/pmusportsfr/onDemandPricing/event/{event_id}/outcome/{outcomes_str}.json"
    params = {"lang": "fr_MC", "market": "FR", "client_id": "200", "channel_id": "1", "ncid": int(time.time() * 1000)}
    
    try:
        response = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code != 200: return None
        cote = response.json().get("selectedOdds", {}).get("decimal")
        return cote / 1000 if cote and cote > 100 else cote
    except Exception: return None

def run_pmu_analysis(event_id, market="Buteur ou passeur décisif", tailles_combi=[2, 3], limit=None, cote_out=4):
    print(f"\n--- DÉMARRAGE PMU ---")
    players_data = get_pmu_players_dict(event_id, market)
    
    if players_data.empty: return pd.DataFrame()
        
    valid_combos = []
    for home_status in [1, 0]:
        players_in_team = players_data[players_data['home'] == home_status].index.tolist()
        for taille in tailles_combi:
            for combo in combinations(players_in_team, taille):
                if any(players_data.loc[p, "Cote PMU"] >= cote_out for p in combo): continue
                valid_combos.append(combo)
            
    print(f"Génération de {len(valid_combos)} combinaisons valides...")
    results = []
    
    for combo in valid_combos[:limit]:
        outcome_ids = [players_data.loc[p, "outcome_id"] for p in combo]
        cote_combi = get_pmu_combi_odds(event_id, outcome_ids)
        cle_jointure = " - ".join(sorted([p for p in combo]))
        cotes_solos = " / ".join([f"{players_data.loc[p, 'Cote PMU']:.2f}" for p in combo])
        
        results.append({
            "Match": players_data.iloc[0]['Match'], 
            "Equipe": players_data.loc[combo[0], "Equipe"],
            "home": players_data.loc[combo[0], "home"],
            "Taille Combi": len(combo),
            "Joueurs": cle_jointure,
            "Cote PMU": cote_combi
        })
        time.sleep(0.5)
        
    df_pmu_final = pd.concat([players_data.drop(columns=['outcome_id']), pd.DataFrame(results)]) if results else players_data.drop(columns=['outcome_id'])
    return df_pmu_final.reset_index(drop=True)


# ==========================================
# 3. UTILITAIRES DE MATCHING
# ==========================================
def nettoyer_nom(nom):
    return unidecode.unidecode(str(nom)).lower().strip()


# ==========================================
# 4. MODULE WINAMAX
# ==========================================
def run_winamax_extraction(url_match, df_pmu):
    print(f"\n--- DÉMARRAGE WINAMAX ---")
    df_wina_results = []
    mapping_pmu_to_wina = {} 

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())
        page.goto(url_match)

        try: page.locator("button:has-text('Accepter')").first.click(timeout=2000)
        except: pass

        try: page.get_by_text("Joueur décisif", exact=True).first.wait_for(timeout=10000)
        except: return pd.DataFrame()

        section = page.locator("div").filter(has=page.get_by_text("Joueur décisif", exact=True)).filter(has=page.locator("div.bet-group-template")).last
        tableaux = section.locator("div.bet-group-template")

        for i in range(tableaux.count()):
            btn_plus = tableaux.nth(i).locator("xpath=..").get_by_text("Plus de sélections")
            if btn_plus.count() > 0 and btn_plus.first.is_visible():
                btn_plus.first.click()
                time.sleep(1)

        dict_boutons = {} 
        wina_simples_bruts = []
        
        for i in range(tableaux.count()):
            parent_bloc = tableaux.nth(i).locator("xpath=..")
            is_home = 1 if i == 0 else 0
            noms_elems = parent_bloc.locator("div:has(> .label-line)")
            btn_elems = parent_bloc.locator(".odd-button-wrapper")
            val_elems = parent_bloc.locator(".odd-button-value")
            
            if noms_elems.count() > 0:
                for j in range(noms_elems.count()):
                    nom = noms_elems.nth(j).inner_text().strip()
                    dict_boutons[nom] = btn_elems.nth(j * 3 + 2)
                    wina_simples_bruts.append({"home": is_home, "Nom": nom, "Cote": float(val_elems.nth(j * 3 + 2).inner_text().replace(',', '.'))})

        # Matching Simples
        for home_status in [1, 0]:
            pmu_names = df_pmu[(df_pmu['home'] == home_status) & (df_pmu['Taille Combi'] == 1)]['Joueurs'].tolist()
            wina_names = [x["Nom"] for x in wina_simples_bruts if x["home"] == home_status]
            wina_clean = [nettoyer_nom(n) for n in wina_names]
            
            for pmu_name in pmu_names:
                best_match, score = process.extractOne(nettoyer_nom(pmu_name), wina_clean)
                wina_original = wina_names[wina_clean.index(best_match)]
                if score >= 90:
                    print(f"{wina_original}({best_match}) matched {pmu_name}({nettoyer_nom(pmu_name)}) with score of {score}")
                    wina_original = wina_names[wina_clean.index(best_match)]
                    mapping_pmu_to_wina[pmu_name] = wina_original
                    # Ajout de la cote simple trouvée directement avec la clé universelle PMU
                    cote_simple = next(x["Cote"] for x in wina_simples_bruts if x["Nom"] == wina_original)
                    df_wina_results.append({"Taille Combi": 1, "Joueurs": pmu_name, "Cote Wina": cote_simple})
                else:
                    print(f"WARNING! {wina_original}({best_match}) matched {pmu_name}({nettoyer_nom(pmu_name)}) with low score of {score}")
            
            

        # MyMatch pour Combos
        print("Calcul des combinaisons sur Winamax...")
        df_pmu_combos = df_pmu[df_pmu['Taille Combi'] > 1]
        panier = page.locator("div[data-testid='sticky-wrap']")

        for i, (_, row) in enumerate(df_pmu_combos.iterrows()):
            print(f"Ligne {i+1} / {len(df_pmu_combos)}: {row['Joueurs']}")
            cle_universelle_pmu = row['Joueurs']
            joueurs_pmu_list = cle_universelle_pmu.split(' - ')
            
            joueurs_wina_list = [mapping_pmu_to_wina.get(j) for j in joueurs_pmu_list]
            if None in joueurs_wina_list: continue
                
            try:
                for j_wina in joueurs_wina_list:
                    dict_boutons[j_wina].scroll_into_view_if_needed()
                    dict_boutons[j_wina].click()
                    time.sleep(0.1)
                
                time.sleep(0.1) 
                
                match_cote = re.search(r"Cote\s*totale\s*(\d+(?:[.,]\d+)?)", panier.inner_text(), re.IGNORECASE)
                if match_cote:
                    df_wina_results.append({"Taille Combi": len(joueurs_pmu_list), "Joueurs": cle_universelle_pmu, "Cote Wina": float(match_cote.group(1).replace(',', '.'))})

                if panier.locator("svg[viewBox='0 0 20 20']").first.is_visible():
                    panier.locator("svg[viewBox='0 0 20 20']").first.click()
                    time.sleep(0.1)
                else:
                    for j_wina in joueurs_wina_list: dict_boutons[j_wina].click()

            except Exception:
                page.evaluate("window.localStorage.clear(); sessionStorage.clear();")
                page.reload()
                time.sleep(3)

        browser.close()
    return pd.DataFrame(df_wina_results)


# ==========================================
# 5. MODULE BETCLIC
# ==========================================
def run_betclic_extraction(url_match, df_pmu):
    print(f"\n--- DÉMARRAGE BETCLIC ---")
    df_bc_results = []
    mapping_bc_to_pmu = {} 

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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
                    print(f"{bc_joueur['Nom']}({nettoyer_nom(bc_joueur["Nom"])}) matched {pmu_original}({best_match}) with score of {score}")
                    mapping_bc_to_pmu[bc_joueur["Nom"]] = pmu_original
                    df_bc_results.append({"Taille Combi": 1, "Joueurs": pmu_original, "Cote Betclic": bc_joueur["Cote"]})
                else:
                    print(f"WARNING! {bc_joueur['Nom']}({nettoyer_nom(bc_joueur["Nom"])}) matched {pmu_original}({best_match}) with score of {score}")
                    

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
                    
                    # On cherche la correspondance de ces noms abrégés vers les noms originaux Betclic via Fuzzy
                    # puis on bascule vers la clé universelle PMU.
                    bc_noms_connus = list(mapping_bc_to_pmu.keys())
                    j1_complet, score1 = process.extractOne(j1_bc, bc_noms_connus)
                    j2_complet, score2 = process.extractOne(j2_bc, bc_noms_connus)
                    
                    if score1 >= 60 and score2 >= 60:
                        pmu_ref_1 = mapping_bc_to_pmu[j1_complet]
                        pmu_ref_2 = mapping_bc_to_pmu[j2_complet]
                        
                        # Création de la clé universelle
                        cle_combo_pmu = " - ".join(sorted([pmu_ref_1, pmu_ref_2]))
                        df_bc_results.append({"Taille Combi": 2, "Joueurs": cle_combo_pmu, "Cote Betclic": float(cote_str.replace(',', '.'))})

        browser.close()
    return pd.DataFrame(df_bc_results)


# ==========================================
# 6. EXECUTION PRINCIPALE
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--tailles", type=int, nargs="+", default=[2, 3], help="Tailles des combos PMU")
    parser.add_argument("-l", "--limit", type=int, default=None)
    parser.add_argument("-f", "--file", type=str)
    parser.add_argument("-p", "--event-pmu", type=str)
    parser.add_argument("-w", "--event-wina", type=str)
    parser.add_argument("-b", "--url-betclic", type=str)
    parser.add_argument("-c", "--cote-out", type=float, default=4.0)
    
    args = parser.parse_args()
    
    if args.file:
        matches = pd.read_csv(args.file, sep=";").values
    elif args.event_pmu and args.event_wina:
        matches = [[args.event_pmu, args.event_wina, args.url_betclic]]
    else:
        raise ValueError("No match specified")
        
    GSHEET_CREDENTIALS = "credentials.json"
    GSHEET_NAME = "Joueur décisif PMU/WINA/Betclic"
    for event_pmu, event_wina, url_betclic in matches:
        event_pmu = int(event_pmu)
        event_win = int(event_wina)
        print(f"Running script for {event_pmu = }, {event_wina = } and {url_betclic = }")
        
        # --- CONFIGURATION ---
        URL_WINA = f"https://www.winamax.fr/paris-sportifs/match/{event_wina}"
        EVENT_ID_PMU = event_pmu
        
    
        # 1. Base PMU
        df_final = run_pmu_analysis(event_pmu, tailles_combi=args.tailles, limit=args.limit, cote_out=args.cote_out)
        
        if not df_final.empty:
            # 2. Ajout Winamax
            df_wina = run_winamax_extraction(URL_WINA, df_final)
            if not df_wina.empty:
                df_final = pd.merge(df_final, df_wina, on=['Taille Combi', 'Joueurs'], how='left')
            else:
                df_final['Cote Wina'] = np.nan
                
            # 3. Ajout Betclic
            df_bc = run_betclic_extraction(url_betclic, df_final)
            if not df_bc.empty:
                df_final = pd.merge(df_final, df_bc, on=['Taille Combi', 'Joueurs'], how='left')
            else:
                df_final['Cote Betclic'] = np.nan
                
            # Tri et Nettoyage Final
            df_final['Ratio (PMU/Wina)'] = df_final['Cote PMU'] / df_final['Cote Wina']
            df_final['Ratio (PMU/Betclic)'] = df_final['Cote PMU'] / df_final['Cote Betclic']
            df_final = df_final.sort_values(by=['Taille Combi', 'home', 'Ratio (PMU/Wina)', 'Ratio (PMU/Betclic)'], ascending=[True, False, False, False])
            df_final = df_final.drop(columns=['home'])
            
            print("\n[RÉSULTAT FINAL]")
            print(df_final.to_string(index=False))
            export_to_google_sheets(df_final, GSHEET_CREDENTIALS, GSHEET_NAME, df_final.loc[0, 'Match'])
        else:
            print("Fin de l'exécution (Pas de données pour PMU).")
