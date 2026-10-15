# Comparaison des cotes de joueurs decisifs

Le script compare PMU, Winamax et Betclic et ajoute les resultats a Google
Sheets. Les dependances Python, Chromium Playwright et les identifiants Google
Sheets doivent deja etre disponibles dans l'environnement utilise.

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install --with-deps chromium
```

## Lancement depuis un navigateur avec GitHub Actions

Le workflow [Comparer les cotes](./.github/workflows/odds.yml) se lance
manuellement, sans application a installer sur le telephone ou l'ordinateur.
Il doit etre publie sur la branche par defaut du depot pour apparaitre dans
**Actions**. Aucune planification automatique n'est activee.

1. Pousser les fichiers sur GitHub, puis ouvrir **Actions > Comparer les cotes >
   Run workflow**.
2. Choisir le mode :
   - **automatic** decouvre la rencontre ; choisir la competition, indiquer le
     nom exact et la date (vide = aujourd'hui a Paris, `demain` ou `AAAA-MM-JJ`) ;
   - **manual** saute la decouverte du calendrier Winamax : saisir les
     identifiants PMU et Winamax du meme match, et eventuellement son URL Betclic.
     L'identifiant Winamax est utilise pour ouvrir directement sa page de match.
3. Pour le premier essai, garder les duos et laisser **Ecrire dans Google Sheets**
   decoche. En mode manuel, PMU et Winamax sont consultes, ainsi que Betclic si
   son URL est fournie, sans authentification Google ni ecriture dans Sheets.
   Aucune mise n'est placee.
4. Ouvrir l'execution pour consulter ses logs. Les fichiers CSV et le journal
   sont telechargeables dans ses **Artifacts**, conserves pendant sept jours.
   Une execution verte sans CSV peut simplement indiquer qu'aucun match n'est
   disponible ; verifier le journal.

Pour activer Sheets, creer dans **Settings > Secrets and variables > Actions >
New repository secret** un secret `GOOGLE_CREDENTIALS_JSON` contenant le JSON
complet d'une **nouvelle** cle de compte de service. Ne jamais reutiliser une
cle exposee dans Git. Partager le classeur avec l'adresse de ce compte.
Le classeur attendu est `Joueur decisif PMU/WINA/Betclic` avec les accents
definis par `GSHEET_NAME` dans [master_odds_parser.py](./master_odds_parser.py).
Cocher ensuite l'export au lancement et consulter le classeur dans Google Sheets.
L'export ajoute des lignes : les relances ne remplacent pas les precedentes.

La cle est ecrite uniquement sur la machine temporaire, supprimee apres le
traitement et exclue des artifacts. Ne pas afficher son contenu dans les logs.
Les executions de ce workflow sont serialisees pour limiter les exports
simultanes ; cela ne couvre pas les lancements locaux.

Chaque execution repart sans cache de matchs et est limitee a 60 minutes.
Les quotas et la facturation GitHub Actions dependent de l'offre du depot.
Les sites peuvent bloquer les IP des runners GitHub : le premier essai sert
a verifier cet acces, qui ne peut pas etre garanti par les tests hors ligne.
Le mode manuel evite uniquement la decouverte des identifiants : il ne contourne
pas un refus d'acces de Winamax sur la page du match ou ses cotes.
Un echec ne doit pas etre interprete comme une absence de cotes.
Si Winamax ne charge pas les donnees de competition, le journal et les artifacts
incluent un diagnostic JSON : statut HTTP de navigation, URL finale, titre,
presence des donnees attendues et debut du texte visible de la page publique.
Le diagnostic contient aussi le type et les cles de l'etat initial, la fin
du texte visible et les premiers liens de matchs ; une capture PNG de la page
est jointe pour voir les messages eventuels masques par le menu dans le journal.
Cela permet de distinguer une redirection, un refus d'acces et un chargement
incomplet sans conclure a un blocage sur la seule base d'un timeout.
La detection des marches sur les pages de match attend jusqu'a 30 secondes ;
si elle echoue encore, le journal et les artifacts contiennent aussi un
diagnostic JSON et une capture de la page Winamax ou Betclic.
Les compositions officielles restent filtrees uniquement si elles sont publiees.

Le meme mode de test est disponible localement :

```bash
python3 master_odds_parser.py -c l1 --date demain -m "Le Mans - Toulouse" --tailles 2 --no-export
```

`--no-export` ecrit les resultats dans `results/<identifiant_pmu>.csv`, sans
cle Google. Une relance du meme match remplace ce CSV local uniquement.

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

```bash
python3 -m unittest -q test_match_discovery test_flashscore_lineups \
  test_name_matching test_master_odds_parser test_betclic_odds
```

Certains tests utilisent Chromium localement sur des pages HTML de test.
Aucun test n'effectue de pari ou d'ecriture Google Sheets.
