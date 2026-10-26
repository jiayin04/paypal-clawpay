"""PayPal integration data models and currency conversion (contracts.md §3.7)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUPPORTED_CURRENCIES: frozenset[str] = frozenset({"MYR", "USD"})


def to_paypal_value(amount_minor: int, currency: str) -> str:
    """Format minor amount as 2-decimal string for PayPal.

    Currencies outside {MYR, USD} raise ValueError before any HTTP call.
    """
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Unsupported PayPal currency: {currency!r}. Must be one of {sorted(SUPPORTED_CURRENCIES)}")
    return f"{amount_minor / 100:.2f}"


def from_paypal_value(value: str) -> int:
    """Convert PayPal decimal string to minor units (integer cents)."""
    return int(round(float(value) * 100))


@dataclass(frozen=True)
class SetupToken:
    """PayPal Vault v3.4 setup-token representation."""

    id: str
    status: str
    approve_url: str | None = None


@dataclass(frozen=True)
class PaymentToken:
    """PayPal Vault v3.4 payment-token representation."""

    id: str
    customer_id: str | None = None


@dataclass(frozen=True)
class ProviderOrder:
    """PayPal Orders v2 representation with capture details."""

    id: str
    status: str
    capture_id: str | None = None
    capture_status: str | None = None
    capture_status_reason: str | None = None
    capture_amount_minor: int | None = None
    capture_currency: str | None = None
