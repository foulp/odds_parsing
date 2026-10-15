# Comparaison des cotes de joueurs decisifs

Le script compare PMU, Winamax et Betclic et ajoute les resultats a Google
Sheets. Les dependances Python, Chromium Playwright et les identifiants Google
Sheets doivent deja etre disponibles dans l'environnement utilise.

## Matchs du jour

```bash
python3 master_odds_parser.py
```

Sans argument, le script recherche uniquement les matchs du jour dans le fuseau
`Europe/Paris`, en Ligue des Champions (`ldc`), Ligue 1 (`l1`) et Premier League
(`pl`). Flashscore fournit le calendrier ; les references des bookmakers sont
rapprochees dans la meme competition, avec les equipes dans le meme ordre et
un ecart d'horaire maximal de 15 minutes. Une association absente, ambigue ou
reutilisee provoque une erreur avant le calcul des cotes : aucune reference
n'est devinee. Une competition sans rencontre aujourd'hui est ignoree normalement.

Les URL des competitions et les chemins de l'API publique PMU sont regroupes
dans `COMPETITIONS` dans [match_discovery.py](./match_discovery.py).

```bash
# Une ou deux competitions seulement
python3 master_odds_parser.py -c l1
python3 master_odds_parser.py --competition l1 pl

# Un ou plusieurs matchs du jour, dans le perimetre choisi
python3 master_odds_parser.py -c l1 -m "PSG - Marseille"
python3 master_odds_parser.py -c l1 pl --match "PSG - Marseille" "Arsenal - Chelsea"

# Les duos seulement, en plus des cotes simples (pas de trios)
python3 master_odds_parser.py -c pl --tailles 2

# Tester les matchs de demain, ou une date precise
python3 master_odds_parser.py -c l1 --date demain --tailles 2
python3 master_odds_parser.py -c l1 --date 2026-10-16 -m "Le Mans - Toulouse"
```

`--date` est reserve au mode automatique. Sans cette option, la date reste
aujourd'hui dans le fuseau `Europe/Paris`. La date choisie determine aussi le
fichier de cache. Les cotes et compositions lues sont celles disponibles au
moment de l'execution, pas une projection de leur valeur a la date choisie.

Un nom d'equipe seul est accepte par `-m` s'il identifie une seule rencontre.
Toutes les selections doivent etre resolues avant de lancer l'analyse.
Sans `-c`, les trois competitions sont incluses ; sans `-m`, toutes leurs
rencontres du jour sont analysees.

**Changement de raccourci :** `-c` signifie maintenant `--competition`.
Pour le seuil de cote, employer `--cote-out 4`, et non plus `-c 4`.

## Cache et relance

Les references (identifiants, URL, equipes et horaire) sont sauvegardees dans
`.match-cache/AAAA-MM-JJ.json`, par competition. Les competitions deja en cache
ne sont pas visitees a nouveau. Le cache d'une autre date n'est jamais reutilise.
Il n'enregistre ni les cotes, ni les compositions, ni la progression d'une analyse.

```bash
# Rafraichir les references de Ligue 1, par exemple apres un changement d'horaire
python3 master_odds_parser.py -c l1 --refresh-matches

# Recalculer uniquement ce match a partir de ses references en cache
python3 master_odds_parser.py -c l1 -m "PSG - Marseille"
```

La relance relit les cotes et les compositions du match selectionne ; elle ne
recalcule pas les autres matchs. Un cache vide peut etre rafraichi si de nouvelles
rencontres sont ajoutees. Un cache invalide est signale et n'est remplace que
sur demande avec `--refresh-matches`.

## Compositions

Par defaut, seule une **composition officielle complete** declenche le filtre :
10 joueurs de champ par equipe, sans les gardiens. Les compositions probables
ne filtrent pas. Une absence normale de composition ne filtre pas non plus.
Une erreur technique, une page inconnue ou une composition officielle incomplete
arrete le match au lieu de poursuivre avec des donnees douteuses.

```bash
# Autoriser aussi les compositions probables
python3 master_odds_parser.py -c ldc --flashscore
```

Les compositions sont relues a chaque analyse, meme si les references sont en
cache. Une relance apres leur publication applique donc les titulaires officiels.

## Mode manuel

```bash
python3 master_odds_parser.py --file matchs.csv
```

Ce mode ne remplit ni ne modifie le CSV. Les colonnes obligatoires, dans un
fichier separe par `;`, sont `event_pmu`, `event_wina`, `url_betclic`. Une URL
Betclic vide desactive ce bookmaker. Ce mode explicite permet aussi des matchs
hors des trois competitions, sans appliquer la restriction automatique au jour.

Colonnes optionnelles :
- `flashscore_url` : fiche du match, pour verifier automatiquement sa composition ;
- `competition` (`ldc`, `l1`, `pl`) : necessaire pour utiliser `-c` avec le CSV ;
- `home`, `away` : necessaires pour utiliser `-m` avec le CSV.

Sans `flashscore_url`, le mode manuel signale qu'il ne filtre pas les titulaires.
Avec `--flashscore`, il recherche la rencontre sur les pages des competitions
selectionnees ; pour une competition differente, fournir `flashscore_url`.

## Validation hors ligne

Pour les combinaisons Winamax, le panier est vide avant chaque calcul. Chaque
clic est confirme par le nombre et les noms exacts des lignes Joueur decisif
dans l'unique item MyMatch (le compteur global peut afficher une seule selection
pour un duo ou un trio). La cote finale doit etre stable sur trois lectures
espacees de 0,5 seconde. Une incoherence declenche la reconstruction et une
nouvelle tentative du combo ; un second echec arrete le match.

```bash
python3 -m unittest -q test_match_discovery test_flashscore_lineups \
  test_name_matching test_master_odds_parser test_betclic_odds
```

Certains tests utilisent Chromium localement sur des pages HTML de test.
Aucun test n'effectue de pari ou d'ecriture Google Sheets.
