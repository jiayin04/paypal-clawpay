"""Agent passport documents (lite, APS-shaped): pure builders and grant checks.

A delegation is a user's signed grant to one agent device: which merchants it
may buy from, the largest single order, and an expiry. A receipt records each
allowed or denied order decision made under a grant. Both are canonical JSON
(``canonical_json``: sorted keys, no whitespace, ASCII) signed with Ed25519 by
the ClawPay issuer key; signing itself lives in ``passport_service``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain.canonical import canonical_json

ISSUER = "clawpay"
DELEGATION_PURPOSE = "clawpay.delegation.v1"
RECEIPT_PURPOSE = "clawpay.receipt.v1"


def unix(value: datetime) -> int:
    return int(value.timestamp())


def delegation_document(
    *,
    delegation_id: str,
    user_id: str,
    device_id: str,
    merchants: list[str],
    max_order_minor: int,
    currency: str,
    issued_at: datetime,
    expires_at: datetime,
    key_id: str,
) -> bytes:
    return canonical_json({
        "agent": {"id": device_id, "type": "device"},
        "delegation_id": delegation_id,
        "expires_at": unix(expires_at),
        "issued_at": unix(issued_at),
        "issuer": ISSUER,
        "key_id": key_id,
        "principal": user_id,
        "purpose": DELEGATION_PURPOSE,
        "scope": {"currency": currency, "max_order_minor": max_order_minor, "merchants": sorted(merchants)},
    })


def receipt_document(
    *,
    receipt_id: str,
    delegation_id: str | None,
    device_id: str,
    order_id: str | None,
    decision: str,
    reason: str,
    merchant: str,
    amount_minor: int | None,
    currency: str,
    issued_at: datetime,
    key_id: str | None,
) -> bytes:
    return canonical_json({
        "agent": {"id": device_id, "type": "device"},
        "amount_minor": amount_minor,
        "currency": currency,
        "decision": decision,
        "delegation_id": delegation_id,
        "issued_at": unix(issued_at),
        "issuer": ISSUER,
        "key_id": key_id,
        "merchant": merchant,
        "order_id": order_id,
        "purpose": RECEIPT_PURPOSE,
        "reason": reason,
        "receipt_id": receipt_id,
    })


@dataclass(frozen=True)
class Grant:
    id: str
    merchants: tuple[str, ...]
    max_order_minor: int
    currency: str
    expires_at: datetime
    revoked_at: datetime | None


def grant_status(grant: Grant, now: datetime) -> str:
    if grant.revoked_at is not None:
        return "revoked"
    if grant.expires_at <= now:
        return "expired"
    return "active"


def check_merchant(grant: Grant | None, merchant: str, now: datetime) -> str | None:
    """Reason code when the grant does not allow ``merchant`` right now, else None."""
    if grant is None:
        return "no_delegation"
    status = grant_status(grant, now)
    if status != "active":
        return f"delegation_{status}"
    if merchant not in grant.merchants:
        return "merchant_not_delegated"
    return None


def check_amount(grant: Grant, amount_minor: int, currency: str) -> str | None:
    if currency != grant.currency:
        return "currency_not_delegated"
    if amount_minor > grant.max_order_minor:
        return "delegation_limit_exceeded"
    return None


def public_fields(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """API projection of a stored grant row."""
    grant = Grant(row["id"], tuple(row["merchants"]), int(row["max_order_minor"]), row["currency"],
                  row["expires_at"], row.get("revoked_at"))
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "status": grant_status(grant, now),
        "merchants": list(row["merchants"]),
        "max_order_minor": int(row["max_order_minor"]),
        "currency": row["currency"],
        "expires_at": row["expires_at"],
        "revoked_at": row.get("revoked_at"),
        "created_at": row["created_at"],
        "key_id": row["key_id"],
        "document": row["document"],
        "signature": row["signature"],
    }
