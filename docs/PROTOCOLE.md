# Protocole Extracteur ↔ Central (Balanceur)

L'Extracteur **exécute, observe et rapporte**. Le Balanceur **distribue, verrouille,
analyse et décide du statut final**. Rien dans ce protocole ne permet à l'Extracteur de
dire « le transfert a réussi ».

## Généralités

- HTTP + JSON, toutes les routes sous `{CENTRAL_URL}/extractors/{extractorId}/`.
- En-têtes envoyés : `Authorization: Bearer {EXTRACTOR_TOKEN}`, `X-Extractor-Id`.
- Réponses `2xx` = reçu. `5xx`, `408`, `429` ou réseau coupé → l'Extracteur garde l'envoi
  en file d'attente locale et le renvoie plus tard, **dans l'ordre**. Autre `4xx` → l'envoi
  est abandonné (journalisé).
- Chaque événement a un `eventId` unique et un `seq` croissant : le Central doit ignorer
  un `eventId` déjà reçu (un renvoi après coupure est possible).

## Routes

| Méthode | Route | Rôle |
| --- | --- | --- |
| POST | `heartbeat` | État de l'Extracteur (toutes les `HEARTBEAT_INTERVAL` s, y compris pendant une opération) |
| GET | `operations/next` | Prochaine opération : `200 {"operation": {...}}`, ou `204` s'il n'y a rien |
| POST | `events` | Un événement observé |
| POST | `operations/{operationId}/result` | Bilan d'exécution (une fois par opération ; peut être renvoyé à l'identique) |
| GET | `operations/{operationId}` | État connu du Central (consulté à la reprise après coupure) |

L'Extracteur ne demande une opération que lorsqu'il est `IDLE` : une seule à la fois.
Le Central doit **verrouiller** l'opération quand il la remet (`operations/next`) pour ne pas
la confier à un second Extracteur.

### Heartbeat

```json
{
  "extractorId": "EXT-03",
  "status": "IDLE | BUSY | ERROR | OFFLINE",
  "lastSeen": "2026-10-06T10:00:00Z",
  "currentOperation": "OP-458",
  "errors": ["SIM non prête (état : absent)"],
  "deviceInfo": {"deviceId": "...", "model": "...", "androidVersion": "13", "termuxVersion": "...",
                 "simOperator": "Orange CI", "networkOperator": "Orange CI", "ussdBackend": "termux"},
  "health": {"ok": true, "critical": [], "warnings": [], "checks": {"battery": {...}, "sim": {...}}},
  "pendingEvents": 0,
  "version": "0.1.0"
}
```

`ERROR` : problème local bloquant, l'Extracteur ne prend plus d'opération tant qu'il dure.

### Opération (réponse de `operations/next`)

```json
{
  "operationId": "OP-458",
  "operator": "orange",
  "type": "transfer",
  "ussdCode": "*144*1*{beneficiary}*{amount}*{secret.pin}#",
  "beneficiary": "0707070707",
  "amount": 500,
  "expiresAt": "2026-10-06T10:05:00Z",
  "parameters": {
    "steps": [],
    "simSlot": 0,
    "ussdTimeout": 30,
    "smsWaitSeconds": 60,
    "smsExpectedCount": 1
  }
}
```

- `operationId` : lettres, chiffres, `_ . : -` (128 caractères max). Obligatoire, ainsi que
  `operator` et `ussdCode`.
- Repères : `{beneficiary}`, `{amount}` (entier), `{param.NOM}` (valeur de `parameters.NOM`),
  `{secret.NOM}` (secret local du `.env`, **jamais envoyé par le Central**). Les valeurs
  insérées sont contrôlées : chiffres (et `+` en tête) uniquement.
- `steps` : saisies successives dans le menu (`["1", "{beneficiary}", "{amount}", "{secret.pin}"]`),
  uniquement avec le mécanisme `http`.
- `expiresAt` : une opération reçue après cette date n'est pas exécutée (`EXPIRED`).
- `smsExpectedCount` : la fenêtre d'attente des SMS se ferme dès que ce nombre de SMS des
  expéditeurs de l'opérateur est reçu (sinon au bout de `smsWaitSeconds`).

### Événement

```json
{
  "eventId": "8214bffef9e54bfc92e0fb845be7eebe",
  "extractorId": "EXT-03",
  "operationId": "OP-458",
  "type": "SMS_RECEIVED",
  "seq": 4,
  "occurredAt": "2026-10-06T10:00:12Z",
  "payload": {}
}
```

| Type | Signification | `payload` |
| --- | --- | --- |
| `OPERATION_STARTED` | Opération prise en charge | `operation`, `ussdBackend` |
| `USSD_STARTED` | Le code est parti | `code` (PIN masqué), `steps`, `backend`, `simSlot`, `responseCaptured` |
| `USSD_RESPONSE` | Réponse de l'opérateur lue | `index`, `sessionStatus`, `error`, `response` (texte + analyse) |
| `SMS_RECEIVED` | SMS reçu | `attribution`, `sms` (voir plus bas) |
| `OPERATION_FINISHED` | Exécution terminée, observations transmises | identique au bilan `EXECUTED` |
| `OPERATION_FAILED` | Rien n'est parti chez l'opérateur | identique au bilan `NOT_EXECUTED` |
| `OPERATION_INTERRUPTED` | Coupure pendant l'exécution : état inconnu | identique au bilan `INTERRUPTED` |

`attribution` d'un SMS :
- `DURING_OPERATION` : reçu pendant la fenêtre d'attente de l'opération `operationId` ;
- `AFTER_OPERATION` : reçu après, dans les `LATE_SMS_GRACE` secondes : `operationId` est la
  dernière opération, **à vérifier** par le Balanceur ;
- `NONE` : aucun rattachement (`operationId` nul).

Contenu `sms` : `smsId`, `sender`, `address`, `body`, `receivedAt`, `operator` (d'après
l'expéditeur), `amount`, `amounts`, `fee`, `balance`, `phoneNumber(s)`, `reference(s)`.
Ce sont des **indices** extraits du texte ; le Balanceur vérifie montant, bénéficiaire et
unicité de la référence avant de conclure.

### Bilan (`operations/{id}/result`)

`outcome` vaut :

- `EXECUTED` : le code USSD est parti. Contient `ussd` (statut de session, transcription,
  PIN masqué) et `sms` (`receivedIds`, `fromOperatorSenders`, `expected`, `waitedSeconds`).
  **Ne veut pas dire « réussi ».**
- `NOT_EXECUTED` : `reason` parmi `INVALID_OPERATION`, `INVALID_USSD_CODE`,
  `UNKNOWN_PLACEHOLDER`, `MISSING_SECRET`, `INVALID_SECRET`, `EXPIRED`,
  `INTERACTION_NOT_SUPPORTED`, `USSD_LAUNCH_FAILED`, `INTERRUPTED_BEFORE_USSD`.
  Rien n'a été envoyé : l'opération peut être confiée à un autre Extracteur.
- `INTERRUPTED` : `phase` (`USSD_LAUNCHING`, `USSD_SENT`, `WAITING_SMS`), `ussdSent`
  (`true` ou `null` = inconnu). **Ne pas relancer** sans vérification (solde, SMS, opérateur).

Une opération déjà traitée n'est **jamais** réexécutée : si `operations/next` la renvoie,
l'Extracteur renvoie le bilan déjà établi.

## Passerelle USSD locale (`USSD_BACKEND=http`)

Contrat attendu d'une application Android compagnon (sur le même téléphone, écoute
`127.0.0.1`), qui utilise `TelephonyManager.sendUssdRequest` ou un service d'accessibilité :

| Route | Corps | Réponse |
| --- | --- | --- |
| `POST /ussd/start` | `{"code": "*144#", "simSlot": 0}` | `{"sessionId": "...", "status": "...", "message": "...", "error": null}` |
| `POST /ussd/reply` | `{"sessionId": "...", "input": "1"}` | idem |
| `POST /ussd/cancel` | `{"sessionId": "..."}` | `{}` |

`status` : `WAITING_INPUT` (menu ou saisie attendue), `COMPLETED`, `TIMEOUT`, `FAILED`.
Si `/ussd/start` est injoignable, l'opération est `NOT_EXECUTED` ; s'il ne répond pas à temps,
la session est notée `TIMEOUT` (le code a peut-être été composé).
