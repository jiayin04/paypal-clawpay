"""Provider gateway protocol shared by application services and test doubles."""
from typing import Mapping, Protocol

from app.integrations.paypal.models import PaymentToken, ProviderOrder, SetupToken


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
