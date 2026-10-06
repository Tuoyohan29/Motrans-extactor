# MoTrans USSD Bridge (application Android compagnon)

Passerelle locale qui donne à l'Extracteur ce que Termux ne sait pas faire : **lire la
réponse USSD de l'opérateur** et **naviguer dans les menus**. Elle expose le contrat
`USSD_BACKEND=http` décrit dans [`../docs/PROTOCOLE.md`](../docs/PROTOCOLE.md).

Elle tourne sur le **même téléphone** que l'Extracteur et n'écoute que sur `127.0.0.1` :
aucune donnée ne sort de l'appareil (pas de permission `INTERNET`).

```
Extracteur (Termux, Python)  ──HTTP 127.0.0.1:8765──▶  USSD Bridge (cette appli)
   USSD_BACKEND=http                                      │
                                                          ├─ compose le code USSD (CALL_PHONE)
                                                          ├─ service d'accessibilité : lit le
                                                          │   dialogue USSD et renvoie les saisies
                                                          └─ renvoie {status, message} à l'Extracteur
```

## Contrat HTTP

| Méthode | Route | Corps | Réponse |
| --- | --- | --- | --- |
| GET | `/health` | — | `{ok, accessibility}` |
| POST | `/ussd/start` | `{code, simSlot?}` | `{sessionId, status, message, error}` |
| POST | `/ussd/reply` | `{sessionId, input}` | `{status, message, error}` |
| POST | `/ussd/cancel` | `{sessionId}` | `{}` |

`status` : `WAITING_INPUT` (menu / saisie attendue), `COMPLETED`, `TIMEOUT`, `FAILED`.
En-tête facultatif `X-Bridge-Token` si un jeton est configuré.

## Deux mécanismes

- **Service d'accessibilité activé** (recommandé) : capture le texte du dialogue USSD et
  peut renvoyer une saisie → **navigation dans les menus** (`start` + `reply`), donc
  l'exploration du catalogue fonctionne.
- **Sans accessibilité** : repli sur `TelephonyManager.sendUssdRequest` → une requête, une
  réponse capturée, **sans** navigation (`reply` renvoie une erreur).

## Installation

```bash
adb install -r MotransUssdBridge-debug.apk
```

Puis, dans l'application :
1. **Démarrer la passerelle** (accorde Téléphone + Notifications).
2. **Activer l'accessibilité** → activer « MoTrans USSD Bridge » dans la liste.
3. Laisser l'appli en tâche de fond (désactiver l'optimisation de batterie).

Côté Extracteur (`.env`) : `USSD_BACKEND=http` et `USSD_BRIDGE_URL=http://127.0.0.1:8765`.

## Construire soi-même

Nécessite le SDK Android (platform 34, build-tools 34) et un JDK 17+.

```bash
echo "sdk.dir=/chemin/vers/android-sdk" > local.properties
gradle :app:assembleDebug
# APK : app/build/outputs/apk/debug/app-debug.apk
```

`versionName 0.1.0`, `applicationId ci.motrans.ussdbridge`, minSdk 26, targetSdk 34.

## Limites (honnêtes)

- **Non testé sur un vrai téléphone** : la lecture du dialogue USSD par accessibilité
  dépend du constructeur (le paquet du dialogue, la présence d'un champ de saisie).
  `UssdAccessibilityService` couvre les cas courants (AOSP, Samsung, Oppo, Transsion) ;
  d'autres surcouches peuvent demander un ajustement de `dialogPackages` / des libellés
  de boutons.
- APK **debug** (signé avec la clé de debug) : pour une diffusion, générer un APK release
  signé avec votre propre clé.
- Le choix de la SIM (`simSlot`) sur un double-SIM n'est pas encore implémenté (SIM par
  défaut, comme Termux).
