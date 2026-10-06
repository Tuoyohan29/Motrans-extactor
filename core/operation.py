"""Structure locale d'une opération reçue du Balanceur, et préparation du code USSD.

Exemple d'opération :

    {
      "operationId": "OP-458",
      "operator": "orange",
      "type": "transfer",
      "ussdCode": "*144*1*{beneficiary}*{amount}*{secret.pin}#",
      "beneficiary": "0707070707",
      "amount": 500,
      "parameters": {"steps": [], "smsWaitSeconds": 60, "smsExpectedCount": 1},
      "expiresAt": "2026-10-06T12:00:00Z"
    }

Repères possibles dans `ussdCode` et dans `parameters.steps` :
- `{beneficiary}`, `{amount}` : champs de l'opération ;
- `{param.NOM}` : valeur de `parameters.NOM` ;
- `{secret.NOM}` : secret local (variable `SECRET_NOM_<OPERATEUR>` ou `SECRET_NOM` du `.env`).
  Le PIN reste sur le téléphone : il ne transite jamais par le réseau et n'est jamais rapporté.

Toute valeur insérée est contrôlée (chiffres uniquement) : un numéro contenant `*` ou `#`
ne peut pas modifier le menu de l'opérateur.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from utils.helpers import parse_iso
from utils.logger import MASK

OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")
DIAL_RE = re.compile(r"^[0-9*#+]+$")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")
BENEFICIARY_RE = re.compile(r"^\+?\d{4,15}$")
PARAM_VALUE_RE = re.compile(r"^\+?\d{1,20}$")
SECRET_VALUE_RE = re.compile(r"^[0-9*#]{1,32}$")


class OperationError(ValueError):
    """Opération inexécutable. `code` est repris tel quel dans l'événement OPERATION_FAILED."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class DialInput:
    """Une saisie USSD : la valeur réelle (envoyée) et sa version masquée (rapportée)."""

    value: str
    redacted: str


@dataclass
class DialPlan:
    code: DialInput
    steps: list[DialInput]


@dataclass
class Operation:
    operation_id: str
    operator: str
    type: str
    ussd_code: str
    beneficiary: str | None = None
    amount: int | float | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "Operation":
        if not isinstance(data, dict):
            raise OperationError("INVALID_OPERATION", "l'opération doit être un objet JSON")

        operation_id = str(data.get("operationId") or "").strip()
        if not OPERATION_ID_RE.match(operation_id):
            raise OperationError("INVALID_OPERATION", f"operationId invalide : {operation_id!r}")

        operator = str(data.get("operator") or "").strip()
        if not operator:
            raise OperationError("INVALID_OPERATION", "operator manquant")

        ussd_code = str(data.get("ussdCode") or "").strip()
        if not ussd_code:
            raise OperationError("INVALID_OPERATION", "ussdCode manquant")

        beneficiary = data.get("beneficiary")
        beneficiary = None if beneficiary in (None, "") else str(beneficiary).strip()

        amount = data.get("amount")
        if amount in (None, ""):
            amount = None
        else:
            try:
                amount = float(amount)
            except (TypeError, ValueError) as exc:
                raise OperationError("INVALID_OPERATION", f"amount invalide : {data.get('amount')!r}") from exc
            if amount <= 0:
                raise OperationError("INVALID_OPERATION", "amount doit être positif")
            amount = int(amount) if amount.is_integer() else amount

        parameters = data.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise OperationError("INVALID_OPERATION", "parameters doit être un objet")
        steps = parameters.get("steps", [])
        if not isinstance(steps, list) or not all(isinstance(s, (str, int)) for s in steps):
            raise OperationError("INVALID_OPERATION", "parameters.steps doit être une liste de saisies")

        expires_at = None
        if data.get("expiresAt"):
            try:
                expires_at = parse_iso(str(data["expiresAt"]))
            except ValueError as exc:
                raise OperationError("INVALID_OPERATION", f"expiresAt invalide : {data['expiresAt']!r}") from exc

        return cls(
            operation_id=operation_id,
            operator=operator,
            type=str(data.get("type") or "unknown"),
            ussd_code=ussd_code,
            beneficiary=beneficiary,
            amount=amount,
            parameters=parameters,
            expires_at=expires_at,
        )

    # --- Paramètres d'exécution --------------------------------------------------

    @property
    def steps(self) -> list[str]:
        return [str(step) for step in self.parameters.get("steps", [])]

    def _number_param(self, key: str) -> float | None:
        value = self.parameters.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def sim_slot(self) -> int | None:
        value = self._number_param("simSlot")
        return int(value) if value is not None else None

    @property
    def ussd_timeout(self) -> float | None:
        return self._number_param("ussdTimeout")

    @property
    def sms_wait_seconds(self) -> float | None:
        return self._number_param("smsWaitSeconds")

    @property
    def sms_expected_count(self) -> int:
        value = self._number_param("smsExpectedCount")
        return max(int(value), 0) if value is not None else 1

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(timezone.utc)) >= self.expires_at

    def summary(self) -> dict[str, Any]:
        return {
            "operationId": self.operation_id,
            "operator": self.operator,
            "type": self.type,
            "beneficiary": self.beneficiary,
            "amount": self.amount,
        }

    # --- Préparation du code USSD ------------------------------------------------

    def dial_plan(self, secrets: dict[str, str]) -> DialPlan:
        code = self._resolve(self.ussd_code, secrets, "ussdCode")
        if not (code.value[0] in "*#" and code.value.endswith("#")):
            raise OperationError("INVALID_USSD_CODE", f"code USSD invalide : {code.redacted}")
        steps = [self._resolve(step, secrets, f"steps[{i}]") for i, step in enumerate(self.steps)]
        return DialPlan(code=code, steps=steps)

    def _resolve(self, template: str, secrets: dict[str, str], where: str) -> DialInput:
        value_parts: list[str] = []
        redacted_parts: list[str] = []
        position = 0
        for match in PLACEHOLDER_RE.finditer(template):
            literal = template[position:match.start()]
            value_parts.append(literal)
            redacted_parts.append(literal)
            value, is_secret = self._placeholder(match.group(1), secrets, where)
            value_parts.append(value)
            redacted_parts.append(MASK if is_secret else value)
            position = match.end()
        value_parts.append(template[position:])
        redacted_parts.append(template[position:])

        value, redacted = "".join(value_parts), "".join(redacted_parts)
        if not value or not DIAL_RE.match(value):
            raise OperationError("INVALID_USSD_CODE", f"{where} contient des caractères interdits : {redacted!r}")
        return DialInput(value=value, redacted=redacted)

    def _placeholder(self, name: str, secrets: dict[str, str], where: str) -> tuple[str, bool]:
        if name == "beneficiary":
            if not self.beneficiary or not BENEFICIARY_RE.match(self.beneficiary):
                raise OperationError("INVALID_OPERATION", f"bénéficiaire absent ou invalide ({where})")
            return self.beneficiary, False
        if name == "amount":
            if self.amount is None or not float(self.amount).is_integer():
                raise OperationError("INVALID_OPERATION", f"montant absent ou non entier ({where})")
            return str(int(self.amount)), False
        if name.startswith("param."):
            key = name[len("param."):]
            value = str(self.parameters.get(key, ""))
            if not PARAM_VALUE_RE.match(value):
                raise OperationError("INVALID_OPERATION", f"parameters.{key} absent ou invalide ({where})")
            return value, False
        if name.startswith("secret."):
            key = name[len("secret."):].lower()
            value = secrets.get(f"{key}_{self.operator.lower()}") or secrets.get(key)
            if not value:
                raise OperationError(
                    "MISSING_SECRET",
                    f"secret {key!r} absent du .env (SECRET_{key.upper()}_{self.operator.upper()} ou SECRET_{key.upper()})",
                )
            if not SECRET_VALUE_RE.match(value):
                raise OperationError("INVALID_SECRET", f"secret {key!r} : chiffres, * ou # uniquement")
            return value, True
        raise OperationError("UNKNOWN_PLACEHOLDER", f"repère inconnu {{{name}}} dans {where}")
