# Motrans — Extracteur

Programme Python qui tourne dans **Termux**, sur le téléphone qui possède la SIM.

> L'Extracteur est le bras opérationnel de Motrans : il exécute les opérations sur le
> téléphone, récupère les réponses de l'opérateur et transmet les preuves au Balanceur.
> Il ne décide **jamais** qu'une opération a réussi : c'est le Balanceur qui confronte
> les observations à l'opération et fixe le statut final.

```
BALANCEUR ──OP-458──▶ communication/ ──▶ core/executor
                                             │
                                     ┌───────┴───────┐
                                     ▼               ▼
                                   ussd/            sms/
                                     │               │
                                 réponse        confirmation
                                     └───────┬───────┘
                                             ▼
                               communication/reporter ──▶ CENTRAL
```

Protocole complet (routes, opération, événements, bilan) : [`docs/PROTOCOLE.md`](docs/PROTOCOLE.md).

## Contenu

```
main.py                    Démarrage, identification, heartbeat, boucle d'attente
config.py                  Lecture du .env
core/executor.py           Orchestration d'une opération (USSD -> SMS -> rapport)
core/operation.py          Structure d'une opération, contrôle et préparation du code USSD
core/state.py              IDLE / BUSY / ERROR / OFFLINE
ussd/launcher.py           Mécanismes USSD : termux, http (passerelle Android), mock
ussd/session.py            Session : STARTED, WAITING_RESPONSE, INTERACTION_REQUIRED, COMPLETED, TIMEOUT, FAILED
ussd/explorer.py           Exploration des menus : sort le catalogue (pass, prix, validité)
ussd/parser.py             Menu, montants, numéros, références, offres dans une réponse USSD
sms/reader.py              Lecture des SMS (termux-sms-list, fichier de test)
sms/listener.py            Détection des nouveaux SMS (curseur persistant)
sms/parser.py              Expéditeur, montant, frais, solde, numéro, référence, opérateur
communication/client.py    Appels HTTP au Central
communication/heartbeat.py Battement de cœur + contrôle de santé
communication/reporter.py  Événements et bilans, file d'attente si le Central est injoignable
device/info.py             Appareil, Android, Termux, SIM, opérateur
device/health.py           Termux:API, SIM, réseau, batterie, contact avec le Central
storage/local_state.py     État local (data/state.json) pour reprendre après une coupure
utils/                     Journal (secrets masqués), outils communs
tools/fake_central.py      Faux Central pour tester sans Balanceur
tests/                     Tests unitaires (python -m unittest)
```

## Installation sur le téléphone

1. Installer **Termux** et **Termux:API** depuis la même source (F-Droid ou GitHub, pas de
   mélange avec le Play Store).
2. Dans Android, donner à **Termux:API** les permissions *Téléphone* et *SMS*.
3. Dans Termux :

   ```bash
   pkg update && pkg install python termux-api git
   git clone https://github.com/Tuoyohan29/Motrans-extactor.git
   cd Motrans-extactor
   cp .env.example .env && nano .env      # CENTRAL_URL, EXTRACTOR_TOKEN, SECRET_PIN_..., SMS_SENDERS_...
   python main.py --check                 # Termux:API, SIM, réseau, batterie, Central
   python main.py
   ```

4. Désactiver l'optimisation de batterie pour Termux et Termux:API (sinon Android coupe
   l'Extracteur). `termux-wake-lock` est appelé au démarrage (`WAKE_LOCK=true`).

Démarrage automatique au boot avec **Termux:Boot** (`~/.termux/boot/motrans-extractor`) :

```bash
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/Motrans-extactor && python main.py >> data/boot.log 2>&1
```

## Mécanismes USSD

| `USSD_BACKEND` | Fonctionne avec | Capture la réponse | Menus (`steps`) |
| --- | --- | --- | --- |
| `termux` | Termux:API seul | non | non |
| `http` | une appli Android passerelle (contrat dans `docs/PROTOCOLE.md`) | oui | oui |
| `mock` | rien (tests) | oui (scriptée) | oui |

Avec `termux`, Android affiche la réponse dans une fenêtre système que Termux ne peut pas
lire, et Termux:API ne signale pas d'erreur si l'appel est refusé. Il faut donc des codes
**en une fois** (`*144*1*{beneficiary}*{amount}*{secret.pin}#`) : la preuve vient des SMS.
Penser à fermer la fenêtre de réponse : elle peut bloquer l'USSD suivant. Le code composé
(PIN compris) peut aussi rester dans l'historique du composeur : réserver le téléphone à
l'Extracteur.

Pour lire les réponses et naviguer dans les menus, utiliser `USSD_BACKEND=http` avec la
passerelle Android fournie dans [`android-ussd-bridge/`](android-ussd-bridge/) (service
d'accessibilité qui lit le dialogue USSD) : seul `ussd/` change côté Extracteur.

## Explorer les menus pour sortir le catalogue (pass, prix)

L'Extracteur peut fouiller les menus USSD d'un opérateur et en sortir les pass avec leur
prix, leur volume et leur validité, pour alimenter la base avec de vraies données. Il faut
`USSD_BACKEND=http` (la passerelle qui lit les réponses).

```bash
# en direct, pour voir le catalogue sans Balanceur :
python main.py --explore '*144#' --operator orange
```

Ou via une opération envoyée par le Balanceur (`type: "explore"`, voir `docs/PROTOCOLE.md`).
Chaque offre trouvée est renvoyée avec le chemin de touches qui y mène (`path` + `optionKey`),
donc le Balanceur sait ensuite comment l'acheter.

**L'exploration ne peut pas acheter**, par construction : l'Extracteur n'envoie que des
touches lues dans un menu, s'arrête à tout écran de saisie (jamais de PIN ni de montant) et
ne sélectionne jamais une option « confirmer / valider / payer / oui / retour »
(`EXPLORE_BLOCK_LABELS`). Un arbre de menus simulé (`tools/mock_ussd_tree.json`) permet de
tester l'exploration sans SIM — voir `tests/test_explorer.py`.

## Sécurité

- Le **PIN** reste dans le `.env` du téléphone (`SECRET_PIN_ORANGE=...`) et s'insère via
  `{secret.pin}`. Il n'est jamais reçu du Central, jamais rapporté (remplacé par `****`
  dans les événements, le bilan, le journal et l'état local).
- Les valeurs insérées dans le code (numéro, montant, paramètres) sont contrôlées : un
  numéro contenant `*` ou `#` ne peut pas détourner le menu.
- Une opération n'est **jamais exécutée deux fois** : les opérations traitées sont
  mémorisées, et une coupure pendant l'exécution donne un bilan `INTERRUPTED` (« ne pas
  relancer sans vérifier ») au lieu d'une nouvelle tentative.
- Les commandes Termux sont lancées sans shell.

## Reprise après coupure

```
Extracteur redémarre
  -> l'état local indique OP-458 en phase USSD_SENT
  -> demande au Central l'état de OP-458 (journalisé)
  -> envoie OPERATION_INTERRUPTED + bilan INTERRUPTED (le Balanceur vérifie)
  -> rapporte les SMS arrivés pendant la coupure (rattachés à OP-458)
  -> renvoie les événements restés en file d'attente, dans l'ordre
```

## Tester sans téléphone ni Balanceur

```bash
# Terminal 1 : faux Central qui distribue tools/sample_operations.json
python tools/fake_central.py --operations tools/sample_operations.json --port 8080

# Terminal 2 : Extracteur en mode simulé
CENTRAL_URL=http://127.0.0.1:8080 EXTRACTOR_TOKEN=dev SECRET_PIN=1234 \
USSD_BACKEND=mock MOCK_USSD_SCRIPT=tools/mock_ussd_script.json \
SMS_BACKEND=file SMS_SENDERS_ORANGE=OrangeMoney python main.py
```

Le faux Central affiche chaque événement reçu. Pour simuler un SMS à la main, ajouter une
ligne dans `data/mock_sms.jsonl` : `{"sender": "OrangeMoney", "body": "..."}`.

Tests unitaires :

```bash
python -m unittest discover -s tests -t .
```
