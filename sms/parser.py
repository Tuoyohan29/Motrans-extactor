"""Extrait d'un SMS : expéditeur, message, date, référence, montant, numéro, opérateur.

Le parseur ne décide pas que la transaction est réussie : il rapporte « j'ai reçu ce SMS »
avec les informations qu'il y lit. La correspondance avec l'opération est le travail du
Balanceur.
"""

from __future__ import annotations

from typing import Any

from sms.reader import RawSms
from utils.helpers import extract_money_info, extract_phone_numbers, extract_references


def _norm(value: str) -> str:
    return "".join(value.split()).casefold()


def detect_operator(sms: RawSms, operator_senders: dict[str, list[str]]) -> str | None:
    candidates = {_norm(sms.address), _norm(sms.sender)}
    for operator, senders in operator_senders.items():
        if candidates & {_norm(s) for s in senders}:
            return operator
    return None


def parse_sms(sms: RawSms, operator_senders: dict[str, list[str]] | None = None) -> dict[str, Any]:
    references = extract_references(sms.body)
    phones = extract_phone_numbers(sms.body)
    return {
        "smsId": sms.id,
        "sender": sms.sender,
        "address": sms.address,
        "body": sms.body,
        "receivedAt": sms.received,
        "operator": detect_operator(sms, operator_senders or {}),
        **extract_money_info(sms.body),
        "phoneNumbers": phones,
        "phoneNumber": phones[0] if phones else None,
        "references": references,
        "reference": references[0] if references else None,
    }
