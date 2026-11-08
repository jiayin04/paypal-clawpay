"""Orders persistence contracts; no driver dependencies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from app.repositories.ports.common import RpcResult


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


class OrderRepository(Protocol):
    async def expire_stale_orders(self, limit: int = 100) -> None: ...

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

    async def list_pending_for_device(self, device_id: str) -> list[OrderRecord]: ...
