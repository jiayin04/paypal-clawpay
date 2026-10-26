"""PayPal integration package exposing PayPalGateway protocol and adapter (§3.7)."""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol

from app.core.config import Settings
from app.integrations.paypal.client import PayPalClient
from app.integrations.paypal.errors import (
    PaymentClassification,
    PayPalApiError,
    PayPalAuthError,
    PayPalError,
    PayPalUnavailable,
    classify,
)
from app.integrations.paypal.models import (
    PaymentToken,
    ProviderOrder,
    SetupToken,
    from_paypal_value,
    to_paypal_value,
)
from app.integrations.paypal.orders import (
    capture_order as _capture_order,
    create_order_with_vault as _create_order_with_vault,
    get_order as _get_order,
)
from app.integrations.paypal.vault import (
    create_payment_token as _create_payment_token,
    create_setup_token as _create_setup_token,
    delete_payment_token as _delete_payment_token,
    get_setup_token as _get_setup_token,
)


class PayPalGateway(Protocol):
    """Protocol for PayPal Sandbox gateway operations (§3.7)."""

    async def create_setup_token(
        self,
        *,
        return_url: str,
        cancel_url: str,
        request_id: str,
    ) -> SetupToken: ...

    async def get_setup_token(self, setup_token_id: str) -> SetupToken: ...

    async def create_payment_token(
        self,
        *,
        setup_token_id: str,
        request_id: str,
    ) -> PaymentToken: ...

    async def delete_payment_token(self, payment_token_id: str) -> None: ...

    async def create_order_with_vault(
        self,
        *,
        request_id: str,
        order_ref: str,
        invoice_id: str,
        description: str,
        amount_minor: int,
        currency: str,
        vault_id: str,
    ) -> ProviderOrder: ...

    async def capture_order(
        self,
        *,
        provider_order_id: str,
        request_id: str,
    ) -> ProviderOrder: ...

    async def get_order(self, provider_order_id: str) -> ProviderOrder: ...

    async def ensure_access_token(self) -> str: ...

    async def verify_webhook(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        webhook_id: str,
    ) -> bool: ...


class PayPalService:
    """Concrete implementation of PayPalGateway using PayPalClient."""

    def __init__(self, client: PayPalClient) -> None:
        self._client = client

    async def ensure_access_token(self) -> str:
        return await self._client.ensure_access_token()

    async def create_setup_token(
        self,
        *,
        return_url: str,
        cancel_url: str,
        request_id: str,
    ) -> SetupToken:
        return await _create_setup_token(
            self._client,
            return_url=return_url,
            cancel_url=cancel_url,
            request_id=request_id,
        )

    async def get_setup_token(self, setup_token_id: str) -> SetupToken:
        return await _get_setup_token(self._client, setup_token_id)

    async def create_payment_token(
        self,
        *,
        setup_token_id: str,
        request_id: str,
    ) -> PaymentToken:
        return await _create_payment_token(
            self._client,
            setup_token_id=setup_token_id,
            request_id=request_id,
        )

    async def delete_payment_token(self, payment_token_id: str) -> None:
        await _delete_payment_token(self._client, payment_token_id)

    async def create_order_with_vault(
        self,
        *,
        request_id: str,
        order_ref: str,
        invoice_id: str,
        description: str,
        amount_minor: int,
        currency: str,
        vault_id: str,
    ) -> ProviderOrder:
        return await _create_order_with_vault(
            self._client,
            request_id=request_id,
            order_ref=order_ref,
            invoice_id=invoice_id,
            description=description,
            amount_minor=amount_minor,
            currency=currency,
            vault_id=vault_id,
        )

    async def capture_order(
        self,
        *,
        provider_order_id: str,
        request_id: str,
    ) -> ProviderOrder:
        return await _capture_order(
            self._client,
            provider_order_id=provider_order_id,
            request_id=request_id,
        )

    async def get_order(self, provider_order_id: str) -> ProviderOrder:
        return await _get_order(self._client, provider_order_id)

    async def verify_webhook(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        webhook_id: str,
    ) -> bool:
        """Verify webhook signature via POST /v1/notifications/verify-webhook-signature (§4.1)."""
        # PayPal header keys are case-insensitive
        h = {k.upper(): v for k, v in headers.items()}
        auth_algo = h.get("PAYPAL-AUTH-ALGO")
        cert_url = h.get("PAYPAL-CERT-URL")
        transmission_id = h.get("PAYPAL-TRANSMISSION-ID")
        transmission_sig = h.get("PAYPAL-TRANSMISSION-SIG")
        transmission_time = h.get("PAYPAL-TRANSMISSION-TIME")

        if not all([auth_algo, cert_url, transmission_id, transmission_sig, transmission_time]):
            return False

        try:
            parsed_event = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return False

        body = {
            "auth_algo": auth_algo,
            "cert_url": cert_url,
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": transmission_time,
            "webhook_id": webhook_id,
            "webhook_event": parsed_event,
        }
        try:
            resp = await self._client.request(
                "POST",
                "/v1/notifications/verify-webhook-signature",
                json=body,
            )
            data = resp.json()
            return bool(data.get("verification_status") == "SUCCESS")
        except Exception:
            return False


__all__ = [
    "PayPalGateway",
    "PayPalService",
    "PayPalClient",
    "SetupToken",
    "PaymentToken",
    "ProviderOrder",
    "PaymentClassification",
    "PayPalError",
    "PayPalApiError",
    "PayPalAuthError",
    "PayPalUnavailable",
    "classify",
    "to_paypal_value",
    "from_paypal_value",
]
