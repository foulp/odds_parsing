import argparse
import requests
import time
import re
import numpy as np
import pandas as pd
from itertools import combinations
import unidecode
from thefuzz import process
from concurrent.futures import ThreadPoolExecutor, as_completed
import random
import gspread
from gspread_dataframe import set_with_dataframe
from gspread.utils import rowcol_to_a1
from playwright.sync_api import sync_playwright

# ==========================================
# 1. MODULE GOOGLE SHEETS
# ==========================================
def export_to_google_sheets(df, json_key_path, spreadsheet_name, sheet_name="Feuille 1"):
    print(f"\n[GSHEETS] Connexion avec {json_key_path} pour l'onglet '{sheet_name}'...")
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
        
        # --- CORRECTION DU BUG HEADER ---
        donnees_existantes = worksheet.get_all_values()
        # On ne garde que les lignes qui contiennent au moins un caractère
        lignes_non_vides = [row for row in donnees_existantes if any(str(cell).strip() for cell in row)]
        
        est_vide = len(lignes_non_vides) == 0
        ligne_depart = len(lignes_non_vides) + 1 if not est_vide else 1
        
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
# 2. MODULE PMU (FONCTIONS DE BASE)
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
                        "Cotes Solos PMU": str(cote_solo),
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


# ==========================================
# 3. UTILITAIRES DE MATCHING
# ==========================================
def nettoyer_nom(nom):
    return unidecode.unidecode(str(nom)).lower().strip()


# ==========================================
# 4. MODULE MASTER (WINA + CRÉATION PMU COMBOS)
# ==========================================
def run_master_wina_and_pmu(url_match, event_id_pmu, df_pmu_simples, tailles_combi, limit, cote_out, ratio_min, name_match_score=85):
    """
    Extrait les cotes simples Wina, filtre les joueurs intéressants (Cote < limite OU Value Wina > 115%),
    génère les combos PMU sur cette base réduite, et lance le MyMatch Winamax dessus.
    """
    print(f"\n--- DÉMARRAGE WINAMAX & GÉNÉRATION CIBLÉE ---")
    df_wina_results = []
    mapping_pmu_to_wina = {} 
    ratio_dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True) # Mettez False si vous voulez voir l'action
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font", "media"] else route.continue_())
        page.goto(url_match)

        try: page.locator("button:has-text('Accepter')").first.click(timeout=2000)
        except: pass

        try: page.get_by_text("Joueur décisif", exact=True).first.wait_for(timeout=10000)
        except: return df_pmu_simples.drop(columns=['outcome_id']), pd.DataFrame()

        section = page.locator("div").filter(has=page.get_by_text("Joueur décisif", exact=True)).filter(has=page.locator("div.bet-group-template")).last
        tableaux = section.locator("div.bet-group-template")

        for i in range(tableaux.count()):
            btn_plus = tableaux.nth(i).locator("xpath=..").get_by_text("Plus de sélections")
            if btn_plus.count() > 0 and btn_plus.first.is_visible():
                btn_plus.first.click()
                time.sleep(0.5)

        dict_boutons = {} 
        wina_simples_bruts = []
        
        # A. Extraction des simples Winamax
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

        # B. Matching et calcul du Ratio (Value)
        print("Analyse des Ratios PMU/Wina...")
        for home_status in [1, 0]:
            pmu_names = df_pmu_simples[df_pmu_simples['home'] == home_status]['Joueurs'].tolist()
            wina_names = [x["Nom"] for x in wina_simples_bruts if x["home"] == home_status]
            wina_clean = [nettoyer_nom(n) for n in wina_names]
            
            for pmu_name in pmu_names:
                best_match, score = process.extractOne(nettoyer_nom(pmu_name), wina_clean)
                wina_original = wina_names[wina_clean.index(best_match)]
                if score >= name_match_score:
                    print(f"{wina_original}({best_match}) matched {pmu_name}({nettoyer_nom(pmu_name)}) with score of {score}")
                    wina_original = wina_names[wina_clean.index(best_match)]
                    mapping_pmu_to_wina[pmu_name] = wina_original
                    # Ajout de la cote simple trouvée directement avec la clé universelle PMU
                    cote_simple = next(x["Cote"] for x in wina_simples_bruts if x["Nom"] == wina_original)
                    df_wina_results.append({"Taille Combi": 1, "Joueurs": pmu_name, "Cote Wina": cote_simple})
                else:
                    print(f"WARNING! {wina_original}({best_match}) matched {pmu_name}({nettoyer_nom(pmu_name)}) with low score of {score}")

        # C. SMART PRUNING : Génération des combinaisons PMU
        print("\n[SMART PRUNING] Filtrage des joueurs (Cote < limite OU Ratio > ratio_min)...")
        valid_combos = []
        for home_status in [1, 0]:
            team_idx = df_pmu_simples[df_pmu_simples['home'] == home_status].index.tolist()
            eligible_idx = []
            
            # Écrémage du pool de joueurs
            for idx in team_idx:
                p_name = df_pmu_simples.loc[idx, 'Joueurs']
                c_pmu = df_pmu_simples.loc[idx, 'Cote PMU']
                p_ratio = ratio_dict.get(p_name, 0)
                
                if c_pmu < cote_out or p_ratio >= ratio_min:
                    eligible_idx.append(idx)
                    
            # Génération des combinaisons sur ce pool restreint
            for taille in tailles_combi:
                for combo in combinations(eligible_idx, taille):
                    valid_combos.append(combo)
                    
        print(f"-> {len(valid_combos)} combinaisons ultra-ciblées générées.")

        # D. Récupération des cotes PMU sur ces combinaisons (API)
        pmu_combos_data = []
        for combo in valid_combos[:limit]:
            outcome_ids = [df_pmu_simples.loc[p, "outcome_id"] for p in combo]
            cote_combi = get_pmu_combi_odds(event_id_pmu, outcome_ids)
            cle_jointure = " - ".join(sorted([df_pmu_simples.loc[p, 'Joueurs'] for p in combo]))

            pmu_combos_data.append({
                "Match": df_pmu_simples.iloc[0]['Match'],
                "Equipe": df_pmu_simples.loc[combo[0], "Equipe"],
                "home": df_pmu_simples.loc[combo[0], "home"],
                "Taille Combi": len(combo),
                "Joueurs": cle_jointure,
                "Cote PMU": cote_combi
            })
            time.sleep(0.2)

        df_pmu_combos = pd.DataFrame(pmu_combos_data)
        
        # Le DataFrame PMU Master est maintenant complet (Simples + Combos filtrés)
        df_pmu_final = pd.concat([df_pmu_simples.drop(columns=['outcome_id']), df_pmu_combos], ignore_index=True)

        # E. Lancement du MyMatch Winamax sur ce pool restreint
        print("\nCalcul des combinaisons ciblées sur Winamax (MyMatch)...")
        panier = page.locator("div[data-testid='sticky-wrap']")

        if not df_pmu_combos.empty:
            for i, ( _, row) in enumerate(df_pmu_combos.iterrows()):
                print(f"- Computing {i+1} / {len(df_pmu_combos)} : {row['Joueurs']}")
                cle_universelle_pmu = row['Joueurs']
                joueurs_pmu_list = cle_universelle_pmu.split(' - ')
                
                joueurs_wina_list = [mapping_pmu_to_wina.get(j) for j in joueurs_pmu_list]
                if None in joueurs_wina_list: continue
                    
                try:
                    for j_wina in joueurs_wina_list:
                        dict_boutons[j_wina].scroll_into_view_if_needed()
                        dict_boutons[j_wina].click()
                        time.sleep(0.2)
                    
                    time.sleep(0.2) 
                    
                        
                    # Ciblage direct de la classe HTML contenant la cote
                    element_cote = panier.locator(".odds-indicator-value")
                        
                    if element_cote.count() > 0 and element_cote.first.is_visible():
                        texte_cote = element_cote.first.inner_text().strip()
                        try:
                            match_cote = float(texte_cote.replace(',', '.'))
                        except ValueError:
                            pass
                    print(match_cote)
                    if match_cote:
                        df_wina_results.append({
                            "Taille Combi": len(joueurs_pmu_list), 
                            "Joueurs": cle_universelle_pmu, 
                            "Cote Wina": match_cote
                        })
                    else:
                        print(f"    [!] Échec lecture cote Wina pour : {cle_universelle_pmu}")

                    if panier.locator("svg[viewBox='0 0 20 20']").first.is_visible():
                        panier.locator("svg[viewBox='0 0 20 20']").first.click()
                        time.sleep(0.2)
                    else:
                        for j_wina in joueurs_wina_list: dict_boutons[j_wina].click()

                except Exception:
                    page.evaluate("window.localStorage.clear(); sessionStorage.clear();")
                    page.reload()
                    time.sleep(3)
                    for i in range(tableaux.count()):
                        btn_plus = tableaux.nth(i).locator("xpath=..").get_by_text("Plus de sélections")
                        if btn_plus.count() > 0 and btn_plus.first.is_visible(): btn_plus.first.click()

        browser.close()
    return df_pmu_final, pd.DataFrame(df_wina_results)


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

def process_single_match(event_pmu, event_wina, url_betclic, args, gsheet_credentials, delay):
    """Encapsule la logique d'un match avec un délai de démarrage anti-bot."""
    # Désynchronisation initiale pour éviter les pics de requêtes
    time.sleep(delay)
    
    try:
        print(f"\n[{event_pmu}] Démarrage du traitement...")
        url_wina_full = f"https://www.winamax.fr/paris-sportifs/match/{event_wina}"
        
        # 1. PMU
        df_pmu_simples = get_pmu_players_dict(event_pmu, "Buteur ou passeur décisif")
        if df_pmu_simples.empty:
            print(f"[{event_pmu}] Fin : Pas de données PMU.")
            return

        # 2. Winamax + PMU Combos
        df_pmu_final, df_wina = run_master_wina_and_pmu(url_wina_full, event_pmu, df_pmu_simples, args.tailles, args.limit, args.cote_out, args.ratio_min, args.name_match_score)
        
        df_final = pd.merge(df_pmu_final, df_wina, on=['Taille Combi', 'Joueurs'], how='left') if not df_wina.empty else df_pmu_final.assign(**{'Cote Wina': np.nan})
            
        # 3. Betclic
        # df_bc = run_betclic_extraction(url_betclic, df_final)
        # df_final = pd.merge(df_final, df_bc, on=['Taille Combi', 'Joueurs'], how='left') if not df_bc.empty else df_final.assign(**{'Cote Betclic': np.nan})
            
        # 4. Nettoyage et Export
        df_final['Ratio (PMU/Wina)'] = df_final['Cote PMU'] / df_final['Cote Wina']
        df_final['Ratio (PMU/Betclic)'] = np.nan  # df_final['Cote PMU'] / df_final['Cote Betclic']
        df_final = df_final.sort_values(by=['Taille Combi', 'home', 'Ratio (PMU/Wina)', 'Ratio (PMU/Betclic)'], ascending=[True, False, False, False])
        df_final = df_final.drop(columns=['home'], errors='ignore')
            
        export_to_google_sheets(df_final, gsheet_credentials, GSHEET_NAME, df_final.loc[0, 'Match'])
        print(f"[{event_pmu}] Terminé avec succès.")
        
    except Exception as e:
        print(f"[{event_pmu}] ERREUR GLOBALE : {e}")
        
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--tailles", type=int, nargs="+", default=[2, 3], help="Tailles des combos PMU")
    parser.add_argument("-l", "--limit", type=int, default=None)
    parser.add_argument("-f", "--file", type=str)
    parser.add_argument("-p", "--event-pmu", type=str)
    parser.add_argument("-w", "--event-wina", type=str)
    parser.add_argument("-b", "--url-betclic", type=str)
    parser.add_argument("-c", "--cote-out", type=float, default=4.0)
    parser.add_argument("-r", "--ratio-min", type=float, default=1.15)
    parser.add_argument("-s", "--name-match-score", type=int, default=85)
    
    args = parser.parse_args()
    
    if args.file:
        matches = pd.read_csv(args.file, sep=";").values
    elif args.event_pmu and args.event_wina:
        matches = [[args.event_pmu, args.event_wina, args.url_betclic]]
    else:
        raise ValueError("No match specified")
        
    GSHEET_CREDENTIALS = "credentials.json"
    GSHEET_NAME = "Joueur décisif PMU/WINA/Betclic"
    
    # Paramètre anti-bot clé : 2 à 3 workers max pour ne pas alerter Datadome
    MAX_WORKERS = 2 
    
    print(f"Lancement du traitement par lots ({len(matches)} matchs, {MAX_WORKERS} processus simultanés)...")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for index, (event_pmu, event_wina, url_betclic) in enumerate(matches):
            event_pmu = int(event_pmu)
            event_wina = int(event_wina)
            
            # Attribue un délai aléatoire croissant pour séparer les lancements de navigateurs
            # Le 1er démarre de suite, le 2ème entre 3s et 7s plus tard, etc.
            jitter = index * random.uniform(3.0, 7.0)
            
            futures.append(executor.submit(
                process_single_match, 
                event_pmu, event_wina, url_betclic, 
                args, GSHEET_CREDENTIALS, jitter
            ))
            
        for future in as_completed(futures):
            # Capture d'éventuelles exceptions silencieuses dans les threads
            if future.exception() is not None:
                print(f"Un thread a planté : {future.exception()}")
