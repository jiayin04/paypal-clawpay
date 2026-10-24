"""Repository interfaces. Services depend only on these; tests use in-memory fakes.

Multi-row / invariant-enforcing writes are Postgres functions (RPCs) that
return ``(ok, reason, ...)``; the fake implements the same reason codes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class DeviceRecord:
    id: str
    user_id: str
    status: str
    key_version: int
    public_key: str
    last_seen_at: datetime | None = None


@dataclass(frozen=True)
class MerchantRecord:
    id: str
    merchant_code: str
    display_name: str
    status: str


@dataclass(frozen=True)
class OrderRecord:
    id: str
    user_id: str
    device_id: str | None
    merchant_id: str
    currency: str
    subtotal_minor: int
    total_minor: int
    status: str
    quote_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    cart_sha256: str | None = None


@dataclass(frozen=True)
class OrderItemRecord:
    product_reference: str
    description: str | None
    quantity: int
    unit_price_minor: int
    line_total_minor: int


@dataclass(frozen=True)
class RpcResult:
    ok: bool
    reason: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpendingSummaryRow:
    day: date
    committed_minor: int
    reserved_minor: int


class DeviceRepository(Protocol):
    async def find_by_token_hash(self, token_hash: str) -> DeviceRecord | None: ...

    async def find_by_llm_token_hash(self, token_hash: str) -> DeviceRecord | None: ...

    async def touch_last_seen(self, device_id: str, at: datetime) -> None: ...

    async def register(
        self,
        *,
        user_id: str,
        device_name: str,
        public_key: str,
        key_version: int,
        token_hash: str,
        llm_token_hash: str,
        actor_type: str,
        actor_id: str | None,
    ) -> RpcResult:
        """RPC register_device -> data {device_id}. Raises RepositoryError on 23505."""
        ...

    async def revoke(
        self, *, device_id: str, user_id: str | None, actor_type: str, actor_id: str | None
    ) -> RpcResult:
        """RPC revoke_device -> data {device_id, cancelled_orders}."""
        ...


class MerchantRepository(Protocol):
    async def get_active_by_code(self, merchant_code: str) -> MerchantRecord | None: ...

    async def get_by_id(self, merchant_id: str) -> MerchantRecord | None: ...


class OrderRepository(Protocol):
    async def create_from_quote(
        self,
        *,
        user_id: str,
        device_id: str,
        merchant_code: str,
        currency: str,
        quote_expires_at: datetime,
        quote_reference: str | None,
        cart_sha256: str,
        items: Sequence[Mapping[str, Any]],
        total_minor: int,
    ) -> RpcResult:
        """RPC create_order_from_quote -> data {order_id, deduplicated}."""
        ...

    async def get_for_user(self, order_id: str, user_id: str) -> OrderRecord | None: ...

    async def list_items(self, order_id: str) -> list[OrderItemRecord]: ...

    async def expire(self, order_id: str) -> RpcResult:
        """RPC expire_order -> data {status}."""
        ...

    async def cancel(
        self, *, order_id: str, user_id: str, actor_type: str, actor_id: str | None
    ) -> RpcResult:
        """RPC cancel_order -> data {order_id, status}."""
        ...


class SpendingRepository(Protocol):
    async def summary(self, user_id: str, timezone: str) -> SpendingSummaryRow: ...


class PaymentMethodRepository(Protocol):
    async def has_active(self, user_id: str) -> bool: ...


class ConversationRepository(Protocol):
    async def upsert_conversation(
        self, *, user_id: str, device_id: str, external_session_id: str
    ) -> str: ...

    async def add_event(
        self,
        *,
        conversation_id: str,
        event_type: str,
        summary: Mapping[str, Any],
        order_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None: ...


class A2AMessageRepository(Protocol):
    async def insert(self, row: Mapping[str, Any]) -> None: ...


class AuditRepository(Protocol):
    async def insert(self, row: Mapping[str, Any]) -> None: ...


class UserScopedReader(Protocol):
    """Reads with the caller's JWT (RLS-enforced). Explicit column lists only."""

    async def list_orders(self, *, limit: int, before: datetime | None) -> list[dict[str, Any]]: ...

    async def get_order_detail(self, order_id: str) -> dict[str, Any] | None: ...

    async def list_devices(self) -> list[dict[str, Any]]: ...


class UserScopedReaderFactory(Protocol):
    def __call__(self, jwt: str) -> AbstractAsyncContextManager[UserScopedReader]: ...
