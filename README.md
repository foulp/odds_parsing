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
un ecart d'horaire maximal de 15 minutes. Une association absente est signalee
avec les candidats disponibles et leurs horaires : seule cette rencontre est
ignoree, les autres restent analysees. La competition incomplete n'est pas
mise en cache ; ses references seront recherchees au prochain passage.
Les autres competitions completes restent en cache. Une association ambigue ou
reutilisee reste bloquante : aucune reference n'est devinee.
Une competition sans rencontre aujourd'hui est ignoree normalement.
Les journaux de calendrier identifient la competition (`[LDC]`, `[L1]`, `[PL]`).
Le navigateur de decouverte utilise explicitement `Europe/Paris` et `fr-FR` ;
les journaux Betclic indiquent les groupes de dates lus et le nombre de rencontres.

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
Un nom de titulaire ambigu n'arrete plus le match : il est exclu du calcul,
avec tous les candidats PMU proches et leurs scores dans le journal.
Si plusieurs titulaires ciblent un meme joueur PMU, tous ces rapprochements
sont exclus, independamment de l'ordre des joueurs. Seuls les rapprochements
non ambigus sont conserves ; les journaux donnent le nombre de titulaires
reconnus par equipe. Une equipe sans aucun titulaire reconnu reste une erreur.
La composition officielle doit toujours etre complete sur Flashscore :
ce traitement partiel concerne uniquement son rapprochement avec le marche PMU.

### Rapprochement des noms PMU / Winamax / Betclic

Les noms sont compares sans accents, ponctuation ni casse ; une date de naissance
entre parentheses ajoutee par Betclic pour distinguer des homonymes
(`C. Eduardo (10/08/2002)`) est ignoree. Deux homonymes restent alors ambigus
et ne sont pas rapproches. Quand les sites retiennent des prenoms differents
pour un meme joueur, ajouter une ligne `nom PMU;nom bookmaker` dans
[aliases.csv](./aliases.csv) (lignes `#` ignorees) :

```
Caumenan N'Guessan;Ange N'Guessan
```

Un alias prime sur le seuil `--name-match-score` et vaut pour Winamax et Betclic.
Les candidats `NON RECONNU` du journal indiquent les noms a ajouter.

### Taches planifiees : uniquement les compositions officielles

```bash
python3 master_odds_parser.py -c l1 --official-lineups-only
python3 master_odds_parser.py -c pl --official-lineups-only
```

Cette option ignore les rencontres dont le coup d'envoi est deja atteint et
celles dont les compositions officielles ne sont pas encore publiees. Seules
deux compositions completes valides declenchent l'analyse et l'export, avec
10 joueurs de champ par equipe. Le coup d'envoi est reverifie au demarrage
du traitement et apres la lecture des compositions. Un traitement deja lance
n'est pas interrompu si le match commence pendant le calcul des cotes.
Une erreur technique ou une composition incomplete reste une erreur, pas une
absence normale. `--flashscore`, qui accepte les probables, est incompatible.

Dans les arguments du Planificateur Windows, ajouter `--official-lineups-only`
a la commande validee :

```text
--user timotheeboulet --exec bash -lc "cd /home/timotheeboulet/vscode-repos/foulp/odds_parsing && .venv/bin/python master_odds_parser.py -c l1 --official-lineups-only >> scheduled-run.log 2>&1"
```

Pour la Premier League, remplacer `-c l1` par `-c pl`.
Sans cette option, le comportement habituel est conserve. Elle ne memorise pas
les matchs deja exportes : deux lancements avant le coup d'envoi avec les
compositions publiees ajoutent tous deux des lignes dans Sheets.
Un lancement ponctuel ne surveille pas l'arrivee des compositions : si elles
ne sont pas publiees a cet instant, il faut relancer avant le coup d'envoi.
Prevoir plusieurs passages dans l'heure qui precede les rencontres plutot
qu'un unique passage exactement une heure avant ; des passages repetes peuvent
cependant dupliquer les exports des matchs deja traites.

### Passages automatiques toutes les 15 minutes

```bash
python3 master_odds_parser.py --scheduled -c l1 pl
```

Ce mode effectue un seul passage puis se termine. Il utilise le cache quotidien
et ne traite que les rencontres dont le coup d'envoi est strictement a venir,
dans les **90 prochaines minutes incluses**. Il impose les compositions
officielles completes : sans elles, le match attend le prochain passage.
Sans rencontre eligible, aucun navigateur d'analyse n'est ouvert ; le premier
passage du jour peut toutefois ouvrir le navigateur pour decouvrir les references.
Le bilan dans l'onglet Executions necessite toujours un acces Google.

Les exports reussis sont memorises sur disque dans `.match-cache/exported.json`,
par identifiant PMU. Ils ne sont plus analyses par les passages `--scheduled`
suivants, meme apres redemarrage du PC. Les absences de composition et les erreurs
ne sont pas memorisees comme des succes : elles sont retentees au prochain
passage, tant que le match n'a pas commence. Les cotes et compositions ne sont
pas mises en cache. Un etat d'exports invalide provoque une erreur explicite.

En mode `--scheduled`, un match dont les cotes simples sont incompletes est
**differe**, sans ecriture dans Sheets ni memorisation comme succes : toutes les
cotes simples PMU et Winamax des joueurs retenus sont exigees (cote finie et
strictement superieure a 1 ; un joueur absent ou non rapproche compte comme
manquant). Le controle PMU precede l'ouverture de Winamax et le controle Winamax
precede les calculs de combinaisons. Les cotes Betclic, les combinaisons et le
nombre de titulaires reconnus ne bloquent jamais l'export.

Les cotes bougent fortement juste apres la publication des compositions : le
premier passage qui voit les compositions officielles d'un match ne l'exporte pas.
Il memorise ce moment dans `.match-cache/lineups-seen.json` (entrees de plus de
2 jours purgees) et l'export a lieu au passage suivant, 15 minutes plus tard.

A 30 minutes ou moins du coup d'envoi (`LAST_CHANCE_MINUTES`), c'est le dernier
essai : le match est exporte sans attente apres les compositions et meme si des
cotes simples manquent, pour ne pas le perdre a cause d'un nom non rapproche ou
d'un joueur jamais propose.
Un passage contenant un match differe ajoute une ligne a l'historique Executions,
avec la raison dans le detail (`Differe : ...`) ; le match
est compte parmi les ignores. Sans `--scheduled`, les exports partiels habituels
restent autorises.

Si la lecture MyMatch echoue, le marche Winamax est recharge et la meme
combinaison retentee une fois. Une recuperation impossible arrete le match sans
exporter les calculs partiels ; le journal conserve la combinaison et la cause.
En mode planifie, le match reste eligible au prochain passage avant le coup d'envoi.

Le succes est enregistre apres le retour de l'export Sheets. Un arret brutal
entre l'ecriture Sheets et la sauvegarde locale, ou un export partiellement
effectue avant une erreur Google, peut encore occasionner un doublon a la relance :
il ne s'agit pas d'une transaction commune entre Google et le disque local.
Le mode habituel, sans `--scheduled`, reste disponible pour un recalcul volontaire
et ne consulte ni ne modifie cette memoire des exports.

Tous les lancements du script prennent un verrou local commun, libere a la fin
ou par le systeme si le processus est tue. Un lancement pendant un autre
traitement est ignore avec un message dans le journal, sans ligne Executions.
Ne pas supprimer le fichier `run.lock` pour debloquer un traitement actif.
Ce verrou utilise `flock` sous Linux/WSL.
`--scheduled` est incompatible avec `--file`, `--date` et `--flashscore`.

Dans le Planificateur Windows, utiliser une seule tache :

- Programme : `wsl.exe`.
- Declencheur a l'ouverture de session Windows, avec repetition toutes les
  **15 minutes**, pendant une duree **indefinie**.
- Choisir « Executer uniquement si l'utilisateur est connecte ».
- Dans les parametres, choisir « Ne pas demarrer une nouvelle instance » si
  la tache est deja en cours ; eviter une limite de duree courte.
- Desactiver les anciennes taches L1/PL a horaires fixes, pour eviter leurs
  exports sans deduplication.

Arguments :

```text
--user timotheeboulet --exec bash -lc "cd /home/timotheeboulet/vscode-repos/foulp/odds_parsing && .venv/bin/python master_odds_parser.py --scheduled -c l1 pl >> scheduled-run.log 2>&1"
```

Le PC doit etre allume et la session ouverte. Apres redemarrage et connexion,
la repetition reprend automatiquement ; les matchs deja commences sont ignores.
Sans `-c`, les trois competitions habituelles sont incluses.
Apres un changement d'horaire, rafraichir les references avec
`--scheduled --refresh-matches -c l1 pl`, puis retirer `--refresh-matches` des
passages reguliers.

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
- Avec `--official-lineups-only`, `flashscore_url` et `start` sont obligatoires
  pour chaque rencontre ; `start` est un horaire ISO avec fuseau, par exemple
  `2026-10-16T20:45:00+02:00`.

Sans `flashscore_url`, le mode manuel signale qu'il ne filtre pas les titulaires.
Avec `--flashscore`, il recherche la rencontre sur les pages des competitions
selectionnees ; pour une competition differente, fournir `flashscore_url`.

## Suivi des executions

Chaque lancement avec au moins un export, un match differe ou une erreur ajoute une ligne dans l'onglet **Executions** (affiche
**Exécutions** dans Sheets) du meme classeur : debut et fin en heure de Paris,
competitions, statut, matchs exportes, ignores et en erreur, et detail d'erreur.
L'onglet est cree automatiquement. Les passages sans export, report ni erreur n'ajoutent
pas de ligne d'historique ; ils restent dans le journal local.
Un bloc **Dernier passage** en `J2:K10` est actualise a chaque fin d'execution,
y compris sans activite, avec les horaires, le statut et les compteurs.
Les colonnes J et K sont reservees a ce bloc, hors du tri de l'historique A:H.
Les anciennes lignes sont conservees sans nettoyage retroactif.
Une rencontre sans reference lors de la decouverte est comptee en erreur
et detaillee dans le bilan, sans empecher les autres rencontres d'etre exportees.
Une erreur technique globale de decouverte est indiquee dans le statut
et le detail, sans etre comptee comme un match en erreur.
Il s'agit d'un bilan de fin de lancement, pas d'une notification ni d'un statut
en temps reel. Un arret brutal ou une panne d'acces Google peut empecher son
enregistrement ; consulter alors le journal local.

Les bilans sont tries par debut d'execution, du plus recent au plus ancien
(puis par fin d'execution en cas d'egalite). L'en-tete reste en premiere ligne.
Aucune ligne de cet historique n'est supprimee.

La colonne Detail liste les matchs exportes et associe chaque erreur au match
concerne, avec le type et le message d'erreur. Sans noms d'equipes en mode manuel,
les identifiants PMU et Winamax identifient la rencontre. Le detail est limite
a 4000 caracteres dans Sheets ; les traces completes restent dans le journal.
Une erreur de traitement sur un match n'annule pas les autres matchs du lot :
tous sont executes, puis le lancement se termine avec le statut
`Termine avec erreurs` si au moins un a echoue. Une erreur avant le lancement
du lot (nettoyage, lecture de l'etat ou erreur technique globale de decouverte)
empeche en revanche son analyse.

### Nettoyage quotidien des onglets de matchs

Avec `--scheduled`, le premier passage quotidien supprime les onglets des matchs
des jours precedents, selon la date du coup d'envoi en Europe/Paris. Les matchs
du jour restent disponibles jusqu'au lendemain : on ne deduit pas leur fin
d'une duree de rencontre estimee.

Par securite, seuls les **nouveaux onglets crees par le script**, exportes avec
un horaire de rencontre connu, sont suivis. Les anciens onglets deja presents
ne sont pas adoptes, meme lors d'un nouvel export dans ces onglets.
Les onglets personnels, renommes et l'onglet Executions ne sont pas supprimes.
Le suivi utilise l'identifiant du classeur et de l'onglet, pas seulement son nom.
Un onglet suivi reutilise pour un autre match reste conserve jusqu'au lendemain
du dernier coup d'envoi connu.

Le registre local `.match-cache/sheets-cleanup.json` conserve les horaires,
les onglets suivis et la date du dernier nettoyage reussi. Ne pas le supprimer
si l'on veut conserver ce suivi. Une perte du registre laisse les onglets
existants intacts. Une erreur Google ou un registre invalide est signale dans
le bilan et fait echouer le passage ; un nettoyage incomplet est retente au
prochain passage. Les anciens resultats ne sont pas archives avant suppression.
Un nettoyage termine ne modifie pas la memoire des matchs deja exportes.

## Validation hors ligne

Pour les combinaisons Winamax, le panier est vide avant chaque calcul. Chaque
clic est confirme par le nombre et les noms exacts des lignes Joueur decisif
dans l'unique item MyMatch (le compteur global peut afficher une seule selection
pour un duo ou un trio).
Les espaces de mise en page autour et entre les mots des joueurs sont toleres,
mais pas les noms differents ou les joueurs supplementaires.
La cote finale doit etre stable sur trois lectures
espacees de 0,5 seconde. Une incoherence declenche la reconstruction et une
nouvelle tentative du combo ; un second echec arrete le match.

```bash
python3 -m unittest -q test_match_discovery test_flashscore_lineups \
  test_name_matching test_master_odds_parser test_betclic_odds test_executions \
  test_scheduled_runs test_sheets_cleanup
```

Certains tests utilisent Chromium localement sur des pages HTML de test.
Aucun test n'effectue de pari ou d'ecriture Google Sheets.
