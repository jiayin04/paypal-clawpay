"""Payments persistence contracts; no driver dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.repositories.ports.common import RpcResult


@dataclass(frozen=True)
class PaymentAttemptRecord:
    id: str
    order_id: str
    user_id: str
    payment_method_id: str | None
    provider: str
    status: str
    idempotency_key: str
    amount_minor: int
    currency: str
    provider_order_id: str | None
    provider_capture_id: str | None
    provider_status: str | None
    failure_code: str | None
    claimed_until: datetime | None
    first_sent_at: datetime | None
    capture_sent_at: datetime | None
    capture_send_count: int
    vault_token_reference: str | None
    completed_at: datetime | None
    exhausted_at: datetime | None
    created_at: datetime


class PaymentRepository(Protocol):
    async def get_attempt(self, attempt_id: str) -> PaymentAttemptRecord | None: ...

    async def get_attempt_for_order(self, order_id: str) -> PaymentAttemptRecord | None: ...

    async def mark_attempt_sent(self, attempt_id: str) -> datetime | None: ...

    async def mark_capture_sent(self, attempt_id: str) -> tuple[datetime | None, int]: ...

    async def apply_payment_result(
        self,
        *,
        attempt_id: str,
        classification: str,
        provider_order_id: str | None = None,
        provider_capture_id: str | None = None,
        provider_status: str | None = None,
        failure_code: str | None = None,
        source: str,
    ) -> RpcResult: ...

    async def mark_attempt_exhausted(self, attempt_id: str) -> RpcResult: ...

    async def claim_reconcilable_attempts(
        self, limit: int = 20, lease_seconds: int = 60
    ) -> list[PaymentAttemptRecord]: ...

    async def claim_reconcilable_attempt(
        self, order_id: str, lease_seconds: int = 60
    ) -> PaymentAttemptRecord | None: ...
