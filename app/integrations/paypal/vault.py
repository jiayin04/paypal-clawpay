"""PayPal Vault v3.4 operations (setup tokens and payment tokens)."""

from __future__ import annotations

from urllib.parse import urlparse

from app.integrations.paypal.client import PayPalClient
from app.integrations.paypal.errors import PayPalApiError
from app.integrations.paypal.models import PaymentToken, SetupToken


async def create_setup_token(
    client: PayPalClient,
    *,
    return_url: str,
    cancel_url: str,
    request_id: str,
    brand_name: str = "ClawPay",
) -> SetupToken:
    """Create a Vault setup-token for linking buyer's PayPal wallet (§3.7)."""
    headers = {"PayPal-Request-Id": request_id}
    body = {
        "payment_source": {
            "paypal": {
                "usage_type": "MERCHANT",
                "customer_type": "CONSUMER",
                "usage_pattern": "IMMEDIATE",
                "permit_multiple_payment_tokens": False,
                "experience_context": {
                    "brand_name": brand_name,
                    "shipping_preference": "NO_SHIPPING",
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                },
            }
        }
    }
    resp = await client.request("POST", "/v3/vault/setup-tokens", headers=headers, json=body)
    data = resp.json()

    approve_url: str | None = None
    for link in data.get("links", []):
        if link.get("rel") == "approve":
            approve_url = link.get("href")
            break

    if approve_url is not None:
        parsed = urlparse(approve_url)
        hostname = (parsed.hostname or "").lower()
        if not hostname.endswith(".paypal.com") and hostname != "paypal.com":
            raise PayPalApiError(502, "INVALID_APPROVE_URL", "Approve URL host must end with paypal.com")

    return SetupToken(
        id=str(data["id"]),
        status=str(data.get("status", "CREATED")),
        approve_url=approve_url,
    )


async def get_setup_token(client: PayPalClient, setup_token_id: str) -> SetupToken:
    """Retrieve setup token status to verify buyer approval."""
    resp = await client.request("GET", f"/v3/vault/setup-tokens/{setup_token_id}")
    data = resp.json()
    approve_url: str | None = None
    for link in data.get("links", []):
        if link.get("rel") == "approve":
            approve_url = link.get("href")
            break
    return SetupToken(
        id=str(data["id"]),
        status=str(data.get("status", "UNKNOWN")),
        approve_url=approve_url,
    )


async def create_payment_token(
    client: PayPalClient,
    *,
    setup_token_id: str,
    request_id: str,
) -> PaymentToken:
    """Exchange approved setup token for a permanent vaulted payment token."""
    headers = {"PayPal-Request-Id": request_id}
    body = {
        "payment_source": {
            "token": {
                "id": setup_token_id,
                "type": "SETUP_TOKEN",
            }
        }
    }
    resp = await client.request("POST", "/v3/vault/payment-tokens", headers=headers, json=body)
    data = resp.json()
    customer = data.get("customer")
    customer_id = customer.get("id") if isinstance(customer, dict) else None
    return PaymentToken(
        id=str(data["id"]),
        customer_id=customer_id,
    )


async def delete_payment_token(client: PayPalClient, payment_token_id: str) -> None:
    """Revoke and delete a vaulted payment token from PayPal (404 treated as success)."""
    try:
        await client.request("DELETE", f"/v3/vault/payment-tokens/{payment_token_id}")
    except PayPalApiError as exc:
        if exc.status == 404:
            return
        raise
