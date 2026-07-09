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
    """
    Exporte un DataFrame Pandas vers un Google Sheet spécifique en mode "Ajout" (Append).
    Ajoute une colonne 'Heure', préserve l'historique et met à jour les filtres/formats.
    """
    print(f"\n[GSHEETS] Connexion avec {json_key_path}...")
    if df.empty:
        print("[GSHEETS] Le DataFrame est vide, annulation de l'export.")
        return
        
    # 1. Ajout de la colonne "Heure" (Timestamp) au tout début du DataFrame
    df_export = df.copy()
    timestamp = pd.Timestamp.now().strftime("%d/%m/%Y %H:%M:%S")
    df_export.insert(0, 'Heure', timestamp)
        
    try:
        # 2. Authentification et Ouverture
        gc = gspread.service_account(filename=json_key_path)
        sh = gc.open(spreadsheet_name)
        
        new_ws = False
        try:
            worksheet = sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            print(f"[GSHEETS] L'onglet '{sheet_name}' n'existe pas. Création en cours...")
            worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="20")
            new_ws = True
        
        # 3. Détecter si la feuille est vide pour savoir où insérer
        donnees_existantes = worksheet.get_all_values()
        ligne_depart = len(donnees_existantes) + 1 if not new_ws else 1
        
        # 4. Écriture des données
        if new_ws:
            # Si vide, on inclut les en-têtes
            set_with_dataframe(worksheet, df_export, row=ligne_depart, include_column_header=True)
        else:
            # Sinon, on ajoute juste en dessous sans répéter les en-têtes
            set_with_dataframe(worksheet, df_export, row=ligne_depart, include_column_header=False)
            
        # Calcul du nombre total de lignes après l'ajout
        total_lignes = ligne_depart + len(df_export) - (1 if not new_ws else 0)

        print("[GSHEETS] Application de la mise en forme et des filtres...")

        # 5. Figer la première ligne
        worksheet.freeze(rows=1)

        # 6. Mettre à jour le filtre global
        try:
            worksheet.clear_basic_filter() 
        except Exception:
            pass
            
        lettre_max = rowcol_to_a1(1, len(df_export.columns))[:-1]
        plage_totale = f"A1:{lettre_max}{total_lignes}"
        worksheet.set_basic_filter(plage_totale)

        # 7. Formatage des nombres et pourcentages
        for col_idx, col_name in enumerate(df_export.columns):
            col_lettre = rowcol_to_a1(1, col_idx + 1)[:-1]
            plage_colonne = f"{col_lettre}2:{col_lettre}{total_lignes}"

            if col_name in ["Cote PMU", "Cote Wina"]:
                worksheet.format(plage_colonne, {
                    "numberFormat": {
                        "type": "NUMBER",
                        "pattern": "0.00"
                    }
                })
            elif col_name == "Ratio (PMU/Wina)":
                worksheet.format(plage_colonne, {
                    "numberFormat": {
                        "type": "PERCENT",
                        "pattern": "0%"
                    }
                })

        # 8. Ajuster la largeur des colonnes au contenu
        body = {
            "requests": [{
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": worksheet.id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": len(df_export.columns)
                    }
                }
            }]
        }
        sh.batch_update(body)

        print(f"[GSHEETS] Succès : {len(df_export)} lignes ajoutées à '{spreadsheet_name} -> {sheet_name}'.")
        
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"[ERREUR] Le fichier '{spreadsheet_name}' est introuvable.")
    except Exception as e:
        print(f"[ERREUR] Échec de l'exportation : {type(e).__name__} - {e}")


# ==========================================
# 2. MODULE PMU
# ==========================================

def get_pmu_players_dict(event_id, market_label):
    url = f"https://eu.offering-api.kambicdn.com/offering/v2018/pmusportsfr/betoffer/event/{event_id}.json"
    params = {"lang": "fr_MC", "market": "FR", "client_id": "200", "channel_id": "1", "includeParticipants": "true"}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

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
        criterion_label = offer.get("criterion", {}).get("label", "")
        if criterion_label == market_label:
            for outcome in offer.get("outcomes", []):
                cote_solo = outcome.get("odds", 0) / 1000 
                player_name = outcome.get("participant")
                outcome_id = outcome.get("id")
                team_id = outcome.get("eventParticipantId")
                
                if player_name and outcome_id:
                    players_dict[player_name] = {
                        "Match": match_name,
                        "Equipe": teams_mapping.get(team_id),
                        "Taille Combi": 1,
                        "Joueurs": player_name,
                        "outcome_id": str(outcome_id),
                        "Cote PMU": cote_solo,
                        "Home": teams_order.get(team_id),
                    }
          
    players_data = pd.DataFrame.from_dict(players_dict, orient='index')
    if not players_data.empty:
        players_data = players_data.sort_values(['Home', 'Cote PMU'], ascending=[False, True])
    return players_data

def get_pmu_combi_odds(event_id, outcome_ids_list):
    outcomes_str = ",".join(outcome_ids_list)
    url = f"https://eu.offering-api.kambicdn.com/offering/v2018/pmusportsfr/onDemandPricing/event/{event_id}/outcome/{outcomes_str}.json"
    params = {"lang": "fr_MC", "market": "FR", "client_id": "200", "channel_id": "1", "ncid": int(time.time() * 1000)}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        response = requests.get(url, params=params, headers=headers)
        if response.status_code != 200: return None
        data = response.json()
        cote_combi = data.get("selectedOdds", {}).get("decimal")
        if cote_combi and cote_combi > 100: cote_combi = cote_combi / 1000
        return cote_combi
    except Exception:
        return None

def run_pmu_analysis(event_id, market="Buteur ou passeur décisif", tailles_combi=[3], limit=None, cote_out=4):
    print(f"\n--- DÉMARRAGE PMU ---")
    players_data = get_pmu_players_dict(event_id, market)
    
    if players_data.empty:
        print("Aucun joueur trouvé.")
        return pd.DataFrame()
        
    valid_combos = []
    teams = players_data['Equipe'].unique()
    
    for team in teams:
        players_in_team = players_data[players_data['Equipe'] == team].index.tolist()
        for taille in tailles_combi:
            for combo in combinations(players_in_team, taille):
                cotes_combo = [players_data.loc[p, "Cote PMU"] for p in combo]
                if any(cote >= cote_out for cote in cotes_combo): continue
                valid_combos.append(combo)
            
    print(f"Génération de {len(valid_combos)} combinaisons valides...")
    results = []
    
    for combo in valid_combos[:limit]:
        outcome_ids = [players_data.loc[p, "outcome_id"] for p in combo]
        cote_combi = get_pmu_combi_odds(event_id, outcome_ids)
        cle_jointure = " - ".join(sorted([p for p in combo]))
        
        results.append({
            "Match": players_data.iloc[0]['Match'], 
            "Home": players_data.loc[combo[0], "Home"],
            "Equipe": players_data.loc[combo[0], "Equipe"],
            "Taille Combi": len(combo),
            "Joueurs": cle_jointure,
            "Cote PMU": cote_combi
        })
        time.sleep(0.5)
        
    if len(results):
        df_results = pd.DataFrame(results)    
        df_pmu_final = pd.concat([players_data.drop(columns=['outcome_id']), df_results])
        return df_pmu_final.reset_index(drop=True)
    
    return players_data.drop(columns=['outcome_id']).reset_index(drop=True)


# ==========================================
# 3. MODULE WINAMAX + MATCHING À LA VOLÉE
# ==========================================
def nettoyer_nom(nom):
    """ Supprime les accents et passe en minuscules pour thefuzz """
    return unidecode.unidecode(str(nom)).lower().strip()

def run_winamax_master_extraction(url_match, df_pmu):
    """
    Ouvre Winamax, extrait les cotes simples, fait le matching avec PMU, 
    et teste uniquement les combinaisons PMU dans le panier MyMatch.
    Retourne le DataFrame fusionné final.
    """
    print(f"\n--- DÉMARRAGE WINAMAX & MYMATCH ---")
    
    # Isolation des données PMU
    pmu_simples = df_pmu[df_pmu['Taille Combi'] == 1]['Joueurs'].tolist()
    df_pmu_combos = df_pmu[df_pmu['Taille Combi'] > 1]
    
    df_wina_simples_list = []
    df_wina_combos_list = []
    mapping_dict = {} # Mapping PMU -> Wina

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        page = context.new_page()

        # Interception et blocage des éléments non essentiels
        page.route("**/*", lambda route: route.abort() 
            if route.request.resource_type in ["image", "media", "font"] 
            else route.continue_()
        )

        print("Ouverture de la page Winamax...")
        page.goto(url_match)

        # Attente et Isolation Section
        try:
            page.get_by_text("Joueur décisif", exact=True).first.wait_for(timeout=10000)
        except Exception:
            print("[ERREUR] Section 'Joueur décisif' introuvable sur Winamax.")
            browser.close()
            return pd.DataFrame()

        section_decisif = page.locator("div").filter(has=page.get_by_text("Joueur décisif", exact=True)).filter(has=page.locator("div.bet-group-template")).last
        tableaux = section_decisif.locator("div.bet-group-template")

        # Déplier les listes
        for i in range(tableaux.count()):
            tableau = tableaux.nth(i)
            tableau.scroll_into_view_if_needed()
            time.sleep(0.5)
            btn_plus = tableau.locator("xpath=..").get_by_text("Plus de sélections")
            if btn_plus.count() > 0 and btn_plus.first.is_visible():
                btn_plus.first.click()
                time.sleep(1) 

        # --- ETAPE A : CARTOGRAPHIE & EXTRACTION SIMPLES ---
        print("Mémorisation des boutons et extraction des cotes simples...")
        dict_boutons = {} 
        
        for i in range(tableaux.count()):
            tableau = tableaux.nth(i)
            parent_bloc = tableau.locator("xpath=..")
            
            # L'ASTUCE : i=0 correspond à Domicile (home=1), i=1 correspond à Extérieur (home=0)
            is_home = 1 if i == 0 else 0
                
            noms_elements = parent_bloc.locator("div:has(> .label-line)")
            boutons_cotes = parent_bloc.locator(".odd-button-wrapper")
            valeurs_cotes = parent_bloc.locator(".odd-button-value")
            
            nb_joueurs = noms_elements.count()
            if nb_joueurs > 0 and nb_joueurs * 3 == boutons_cotes.count():
                for j in range(nb_joueurs):
                    nom = noms_elements.nth(j).inner_text().strip()
                    btn_decisif = boutons_cotes.nth(j * 3 + 2)
                    cote = float(valeurs_cotes.nth(j * 3 + 2).inner_text().replace(',', '.'))
                    
                    dict_boutons[nom] = btn_decisif
                    
                    df_wina_simples_list.append({
                        "Home": is_home,
                        "Joueurs_Wina": nom,
                        "Cote Wina": cote
                    })
            else:
                print(f"[ERREUR] Désalignement Winamax détecté pour le bloc {i}.")

        # --- ETAPE B : FUZZY MATCHING (VIA LE FLAG 'HOME') ---
        print("\nRéconciliation des noms PMU/Wina par bloc (Domicile/Extérieur)...")
        
        # On itère sur [1, 0] (Domicile puis Extérieur)
        for home_status in [1, 0]:
            label_equipe = "Domicile" if home_status == 1 else "Extérieur"
            print(f" -> Mapping pour l'équipe : {label_equipe}")
            
            pmu_simples_equipe = df_pmu[(df_pmu['Home'] == home_status) & (df_pmu['Taille Combi'] == 1)]['Joueurs'].tolist()
            wina_simples_equipe = [x["Joueurs_Wina"] for x in df_wina_simples_list if x["Home"] == home_status]
            wina_clean_equipe = [nettoyer_nom(n) for n in wina_simples_equipe]
            
            if not wina_simples_equipe:
                print(f"    [!] Aucun joueur Winamax extrait pour {label_equipe}.")
                continue
                
            for pmu_name in pmu_simples_equipe:
                pmu_name_clean = nettoyer_nom(pmu_name)
                best_match, score = process.extractOne(pmu_name_clean, wina_clean_equipe)
                best_wina_name = wina_simples_equipe[wina_clean_equipe.index(best_match)]
                if score >= 70:
                    mapping_dict[pmu_name] = best_wina_name
                else:
                    print(f" -> Ignoré (Fuzzy) : PMU '{pmu_name}' (Score trop faible : {score} pour {best_wina_name}[{best_match}] et {pmu_name}[{pmu_name_clean}])")

        # --- ETAPE C : SIMULATION DU MYMATCH (COMBOS) ---
        print("\nDébut du test MyMatch des combinaisons PMU...")
        panier = page.locator("div[data-testid='sticky-wrap']")

        for index, row in df_pmu_combos.iterrows():
            combo_str_pmu = row['Joueurs']
            joueurs_pmu_list = combo_str_pmu.split(' - ')
            
            joueurs_wina_list = [mapping_dict.get(j) for j in joueurs_pmu_list]
            
            if None in joueurs_wina_list:
                print(f" -> Ignoré : Combo {combo_str_pmu} (Joueur non mappé)")
                continue
                
            print(f"Test MyMatch : {' + '.join(joueurs_wina_list)} ...", end=" ")
            
            try:
                for j_wina in joueurs_wina_list:
                    dict_boutons[j_wina].scroll_into_view_if_needed()
                    dict_boutons[j_wina].click()
                    time.sleep(0.2)
                
                time.sleep(0.2) 
                
                texte_panier = panier.inner_text()
                match_cote = re.search(r"Cote\s*totale\s*(\d+(?:[.,]\d+)?)", texte_panier, re.IGNORECASE | re.DOTALL)
                
                if match_cote:
                    cote_totale = float(match_cote.group(1).replace(',', '.'))
                    print(f"Cote : {cote_totale}")
                    df_wina_combos_list.append({
                        "Joueurs_PMU_Ref": combo_str_pmu,
                        "Cote Wina": cote_totale
                    })
                else:
                    print("Cote introuvable dans le panier.")

                poubelle = panier.locator("svg[viewBox='0 0 20 20']").first
                if poubelle.is_visible():
                    poubelle.click()
                    time.sleep(0.2)
                else:
                    for j_wina in joueurs_wina_list:
                        dict_boutons[j_wina].click()
                        time.sleep(0.2)

            except Exception as e:
                print(f"Échec. Nettoyage forcé...")
                page.evaluate("window.localStorage.clear(); window.sessionStorage.clear();")
                page.reload()
                time.sleep(4)
                for i in range(tableaux.count()):
                    btn_plus = tableaux.nth(i).locator("xpath=..").get_by_text("Plus de sélections")
                    if btn_plus.count() > 0 and btn_plus.first.is_visible():
                        btn_plus.first.click()
                time.sleep(1)

        browser.close()

    # --- ETAPE D : CREATION DU DATAFRAME FINAL ---
    df_wina_simples = pd.DataFrame(df_wina_simples_list)
    df_wina_combos = pd.DataFrame(df_wina_combos_list)
    
    # Merge Simples
    df_pmu_singles = df_pmu[df_pmu['Taille Combi'] == 1].copy()
    df_pmu_singles['Joueurs_Wina_Mapped'] = df_pmu_singles['Joueurs'].map(mapping_dict)
    df_singles_final = pd.merge(df_pmu_singles, df_wina_simples, left_on=['Home', 'Joueurs_Wina_Mapped'], right_on=['Home', 'Joueurs_Wina'], how='inner')
    
    # Merge Combos
    df_pmu_combos_to_merge = df_pmu[df_pmu['Taille Combi'] > 1].copy()
    df_combos_final = pd.merge(df_pmu_combos_to_merge, df_wina_combos, left_on='Joueurs', right_on='Joueurs_PMU_Ref', how='inner')
    
    # Concaténation
    df_final = pd.concat([df_singles_final, df_combos_final], ignore_index=True)
    
    # Nettoyage des colonnes temporaires
    df_final = df_final.drop(columns=['Joueurs_Wina_Mapped', 'Joueurs_Wina', 'Joueurs_PMU_Ref'], errors='ignore')
    
    # Calcul final de l'écart
    if not df_final.empty:
        df_final['Ratio (PMU/Wina)'] = df_final['Cote PMU'] / df_final['Cote Wina']
        df_final = df_final.sort_values(by=['Taille Combi', 'Home', 'Ratio (PMU/Wina)'], ascending=[True, False, False])
        
    return df_final.drop(columns=['Home'])


# ==========================================
# 4. EXECUTION (MAIN)
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Générateur de cotes Décisif PMU/Wina")
    parser.add_argument("-t", "--tailles", type=int, nargs="+", default=[2], help="Tailles des combinaisons (ex: -t 2 3)")
    parser.add_argument("-l", "--limit", type=int, default=None, help="Nombre max de combos à tester par équipe")
    parser.add_argument("-f", "--file", type=str, help="CSV file that include event_pmu and event_wina columns")
    parser.add_argument("-p", "--event-pmu", type=str, help="Match ID sur PMU")
    parser.add_argument("-w", "--event-wina", type=str, help="Match ID sur Wina")
    parser.add_argument("-c", "--cote-out", type=float, default=4.0, help="Cote max PMU d'un joueur pour l'inclure dans un combo")
    
    args = parser.parse_args()
    
    if args.file:
        matches = pd.read_csv(args.file, sep=";").values
    elif args.event_pmu and args.event_wina:
        matches = [[args.event_pmu, args.event_wina]]
    else:
        raise ValueError("No match specified")
           
    
    for event_pmu, event_wina in matches:
        event_pmu = int(event_pmu)
        event_win = int(event_wina)
        print(f"Running script for {event_pmu = } and {event_wina = }")
        
        # --- CONFIGURATION ---
        URL_WINA = f"https://www.winamax.fr/paris-sportifs/match/{event_wina}"
        EVENT_ID_PMU = event_pmu
        GSHEET_CREDENTIALS = "credentials.json"
        GSHEET_NAME = "Joueur décisif PMU/WINA"
        
        # 1. Traitement PMU
        df_pmu = run_pmu_analysis(EVENT_ID_PMU, market="Buteur ou passeur décisif", tailles_combi=args.tailles, limit=args.limit, cote_out=args.cote_out)
        print(df_pmu.to_string(index=False))
        if df_pmu.empty:
            print("Fin de l'exécution (Pas de données PMU).")
            exit()
            
        # 2. Winamax + Matching + Combinaisons (Magie tout-en-un)
        df_comparateur = run_winamax_master_extraction(URL_WINA, df_pmu)
        
        # 3. Affichage et Export
        print("\n[RÉSULTAT FINAL : COMPARAISON PMU / WINAMAX]")
        if not df_comparateur.empty:
            print(df_comparateur.to_string(index=False))
            print("\n--- EXPORT GOOGLE SHEETS ---")
            match_name = df_comparateur.loc[0, 'Match']
            export_to_google_sheets(df_comparateur, GSHEET_CREDENTIALS, GSHEET_NAME, match_name)
        else:
            print("Aucune donnée réconciliée.")