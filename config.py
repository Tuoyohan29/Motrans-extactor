"""Configuration de l'Extracteur.

Les valeurs viennent du fichier `.env` (à côté de ce fichier), puis des variables
d'environnement, qui ont la priorité. Aucune dépendance externe : le parseur `.env`
est volontairement minimal (CLE=valeur, commentaires `#`, guillemets optionnels).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

BASE_DIR = Path(__file__).resolve().parent
VERSION = "0.1.0"

USSD_BACKENDS = ("termux", "http", "mock")
SMS_BACKENDS = ("termux", "file", "none")


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def _int(env: Mapping[str, str], key: str, default: int | None) -> int | None:
    value = env.get(key, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{key} doit être un entier (reçu : {value!r})") from exc


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    value = env.get(key, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{key} doit être un nombre (reçu : {value!r})") from exc


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    value = env.get(key, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "oui", "on")


def _list(env: Mapping[str, str], key: str) -> list[str]:
    return [item.strip() for item in env.get(key, "").split(",") if item.strip()]


@dataclass
class Config:
    extractor_id: str = ""
    extractor_token: str = ""
    central_url: str = ""
    http_timeout: float = 15.0
    poll_interval: float = 5.0
    heartbeat_interval: float = 30.0

    ussd_backend: str = "termux"
    ussd_bridge_url: str = "http://127.0.0.1:8765"
    ussd_timeout: float = 30.0
    ussd_sim_slot: int | None = None
    mock_ussd_script: Path | None = None

    sms_backend: str = "termux"
    sms_mock_file: Path = field(default_factory=lambda: BASE_DIR / "data" / "mock_sms.jsonl")
    sms_poll_interval: float = 3.0
    sms_wait_seconds: float = 60.0
    sms_list_limit: int = 30
    sms_senders: list[str] = field(default_factory=list)
    operator_sms_senders: dict[str, list[str]] = field(default_factory=dict)
    late_sms_grace: float = 600.0

    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")
    log_level: str = "INFO"
    log_to_file: bool = True
    wake_lock: bool = True
    termux_cmd_timeout: float = 20.0
    battery_min: int = 15

    # SECRET_PIN=1234 -> {"pin": "1234"} ; SECRET_PIN_ORANGE=... -> {"pin_orange": ...}
    secrets: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def needs_termux(self) -> bool:
        return self.ussd_backend == "termux" or self.sms_backend == "termux"

    @property
    def real_device(self) -> bool:
        """Vrai si l'Extracteur pilote une vraie SIM (pas un mode de démonstration)."""
        return self.ussd_backend != "mock"

    @classmethod
    def load(cls, env_path: Path | None = None, environ: Mapping[str, str] | None = None) -> "Config":
        env: dict[str, str] = load_env_file(env_path or BASE_DIR / ".env")
        env.update(os.environ if environ is None else environ)

        data_dir = Path(env.get("DATA_DIR") or BASE_DIR / "data").expanduser()
        if not data_dir.is_absolute():
            data_dir = BASE_DIR / data_dir

        operator_senders = {
            key[len("SMS_SENDERS_"):].lower(): _list(env, key)
            for key in env
            if key.startswith("SMS_SENDERS_") and _list(env, key)
        }
        secrets = {
            key[len("SECRET_"):].lower(): value
            for key, value in env.items()
            if key.startswith("SECRET_") and value
        }
        mock_script = env.get("MOCK_USSD_SCRIPT", "").strip()
        sms_mock_file = env.get("SMS_MOCK_FILE", "").strip()

        return cls(
            extractor_id=env.get("EXTRACTOR_ID", "").strip(),
            extractor_token=env.get("EXTRACTOR_TOKEN", "").strip(),
            central_url=env.get("CENTRAL_URL", "").strip().rstrip("/"),
            http_timeout=_float(env, "HTTP_TIMEOUT", 15.0),
            poll_interval=_float(env, "POLL_INTERVAL", 5.0),
            heartbeat_interval=_float(env, "HEARTBEAT_INTERVAL", 30.0),
            ussd_backend=env.get("USSD_BACKEND", "termux").strip().lower(),
            ussd_bridge_url=env.get("USSD_BRIDGE_URL", "http://127.0.0.1:8765").strip().rstrip("/"),
            ussd_timeout=_float(env, "USSD_TIMEOUT", 30.0),
            ussd_sim_slot=_int(env, "USSD_SIM_SLOT", None),
            mock_ussd_script=Path(mock_script).expanduser() if mock_script else None,
            sms_backend=env.get("SMS_BACKEND", "termux").strip().lower(),
            sms_mock_file=Path(sms_mock_file).expanduser() if sms_mock_file else data_dir / "mock_sms.jsonl",
            sms_poll_interval=_float(env, "SMS_POLL_INTERVAL", 3.0),
            sms_wait_seconds=_float(env, "SMS_WAIT_SECONDS", 60.0),
            sms_list_limit=_int(env, "SMS_LIST_LIMIT", 30) or 30,
            sms_senders=_list(env, "SMS_SENDERS"),
            operator_sms_senders=operator_senders,
            late_sms_grace=_float(env, "LATE_SMS_GRACE", 600.0),
            data_dir=data_dir,
            log_level=env.get("LOG_LEVEL", "INFO").strip().upper(),
            log_to_file=_bool(env, "LOG_TO_FILE", True),
            wake_lock=_bool(env, "WAKE_LOCK", True),
            termux_cmd_timeout=_float(env, "TERMUX_CMD_TIMEOUT", 20.0),
            battery_min=_int(env, "BATTERY_MIN", 15) or 0,
            secrets=secrets,
        )

    def problems(self) -> list[str]:
        """Erreurs bloquantes de configuration (liste vide si tout va bien)."""
        problems = []
        if not self.central_url:
            problems.append("CENTRAL_URL est obligatoire (adresse du Balanceur).")
        elif not self.central_url.startswith(("http://", "https://")):
            problems.append("CENTRAL_URL doit commencer par http:// ou https://.")
        if not self.extractor_token:
            problems.append("EXTRACTOR_TOKEN est obligatoire (jeton fourni par le Balanceur).")
        if self.ussd_backend not in USSD_BACKENDS:
            problems.append(f"USSD_BACKEND doit valoir {', '.join(USSD_BACKENDS)}.")
        if self.sms_backend not in SMS_BACKENDS:
            problems.append(f"SMS_BACKEND doit valoir {', '.join(SMS_BACKENDS)}.")
        if self.poll_interval <= 0 or self.heartbeat_interval <= 0 or self.sms_poll_interval <= 0:
            problems.append("Les intervalles (POLL, HEARTBEAT, SMS_POLL) doivent être > 0.")
        return problems

    def senders_for(self, operator: str | None) -> list[str]:
        """Expéditeurs SMS attendus pour un opérateur (liste vide = pas de filtre)."""
        if operator and self.operator_sms_senders.get(operator.lower()):
            return self.operator_sms_senders[operator.lower()]
        return self.monitored_senders()

    def monitored_senders(self) -> list[str]:
        senders = list(self.sms_senders)
        for values in self.operator_sms_senders.values():
            senders.extend(values)
        return senders
