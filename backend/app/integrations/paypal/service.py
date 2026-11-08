"""Concrete PayPal gateway: delegates provider operations to focused adapters."""
from typing import Mapping

from app.integrations.paypal.client import PayPalClient
from app.integrations.paypal.models import PaymentToken, ProviderOrder, SetupToken
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
        from app.integrations.paypal.webhooks import verify_webhook
        return await verify_webhook(self._client, headers=headers, raw_body=raw_body, webhook_id=webhook_id)
