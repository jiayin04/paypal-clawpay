"""PayPal Orders v2 operations (create with vault, capture, get)."""

from __future__ import annotations

from typing import Any

from app.integrations.paypal.client import PayPalClient
from app.integrations.paypal.models import ProviderOrder, from_paypal_value, to_paypal_value


def parse_provider_order(data: dict[str, Any]) -> ProviderOrder:
    """Extract order and capture details from PayPal v2 order JSON."""
    order_id = str(data["id"])
    status = str(data.get("status", "UNKNOWN"))

    capture_id: str | None = None
    capture_status: str | None = None
    capture_status_reason: str | None = None
    capture_amount_minor: int | None = None
    capture_currency: str | None = None
    capture_amount_value: str | None = None

    pus = data.get("purchase_units", [])
    if pus and isinstance(pus[0], dict):
        payments = pus[0].get("payments", {})
        captures = payments.get("captures", [])
        if captures and isinstance(captures[0], dict):
            cap = captures[0]
            capture_id = cap.get("id")
            capture_status = cap.get("status")
            status_details = cap.get("status_details")
            if isinstance(status_details, dict):
                capture_status_reason = status_details.get("reason")

            amount_obj = cap.get("amount")
            if isinstance(amount_obj, dict):
                capture_currency = amount_obj.get("currency_code")
                val_str = amount_obj.get("value")
                capture_amount_value = val_str if isinstance(val_str,str) else None
                if val_str is not None:
                    try:
                        capture_amount_minor = from_paypal_value(str(val_str))
                    except (ValueError, TypeError):
                        pass

    return ProviderOrder(
        id=order_id,
        status=status,
        capture_id=capture_id,
        capture_status=capture_status,
        capture_status_reason=capture_status_reason,
        capture_amount_minor=capture_amount_minor,
        capture_currency=capture_currency,
        capture_amount_value=capture_amount_value,
    )


async def create_order_with_vault(
    client: PayPalClient,
    *,
    request_id: str,
    order_ref: str,
    invoice_id: str,
    description: str,
    amount_minor: int,
    currency: str,
    vault_id: str,
) -> ProviderOrder:
    """Create and capture order in single step using vaulted instrument (§3.7)."""
    val_str = to_paypal_value(amount_minor, currency)
    headers = {
        "PayPal-Request-Id": request_id,
        "Prefer": "return=representation",
    }
    body = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": "default",
                "custom_id": order_ref,
                "invoice_id": invoice_id,
                "description": description,
                "amount": {
                    "currency_code": currency,
                    "value": val_str,
                },
            }
        ],
        "payment_source": {
            "paypal": {
                "vault_id": vault_id,
            }
        },
    }
    resp = await client.request("POST", "/v2/checkout/orders", headers=headers, json=body)
    return parse_provider_order(resp.json())


async def capture_order(
    client: PayPalClient,
    *,
    provider_order_id: str,
    request_id: str,
) -> ProviderOrder:
    """Explicitly capture an existing authorized/created order (§3.7)."""
    headers = {
        "PayPal-Request-Id": request_id,
        "Prefer": "return=representation",
    }
    resp = await client.request(
        "POST",
        f"/v2/checkout/orders/{provider_order_id}/capture",
        headers=headers,
    )
    return parse_provider_order(resp.json())


async def get_order(
    client: PayPalClient,
    provider_order_id: str,
) -> ProviderOrder:
    """Poll order status and captures by PayPal order ID (§3.7)."""
    resp = await client.request("GET", f"/v2/checkout/orders/{provider_order_id}")
    return parse_provider_order(resp.json())
