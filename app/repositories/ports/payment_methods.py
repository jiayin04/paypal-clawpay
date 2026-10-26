"""Payment methods persistence contracts; no driver dependencies."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from app.repositories.ports.common import RpcResult


class PaymentMethodRepository(Protocol):
    async def fail_pending(self, pm_id: str) -> None: ...

    async def has_active(self, user_id: str) -> bool: ...

    async def list_active(self, user_id: str) -> list[dict[str, Any]]: ...

    async def get_by_id(self, payment_method_id: str, user_id: str) -> dict[str, Any] | None: ...

    async def create_pending(
        self,
        *,
        user_id: str,
        state_token: str,
        state_expires_at: datetime,
        setup_token_reference: str,
    ) -> str:
        """Insert a pending payment_method row and return its id."""
        ...

    async def activate_payment_method(
        self,
        *,
        payment_method_id: str,
        user_id: str,
        vault_token_reference: str,
        payer_email_masked: str | None,
        provider_customer_reference: str | None = None,
    ) -> RpcResult:
        """RPC activate_payment_method -> ok, reason (23505 -> conflict retry)."""
        ...

    async def revoke_payment_method(
        self,
        *,
        payment_method_id: str,
        user_id: str,
    ) -> RpcResult:
        """RPC revoke_payment_method -> ok, reason (payment_in_progress)."""
        ...

    async def find_by_state(
        self,
        state_token: str,
    ) -> dict[str, Any] | None:
        """Look up a pending method by hashed state; activation consumes the state."""
        ...
