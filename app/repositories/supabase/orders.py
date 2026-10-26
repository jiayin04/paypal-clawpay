"""Service-role Supabase adapter for orders."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.repositories.ports.common import RpcResult
from app.repositories.ports.orders import OrderItemRecord, OrderRecord
from app.repositories.supabase.base import _SupabaseBase, parse_ts


_ORDER_COLUMNS = ("id,user_id,device_id,merchant_id,currency,subtotal_minor,total_minor,status,"
                  "quote_expires_at,created_at,updated_at,cart_sha256")


def _order(row: Mapping[str, Any]) -> OrderRecord:
    return OrderRecord(
        id=row["id"],
        user_id=row["user_id"],
        device_id=row.get("device_id"),
        merchant_id=row["merchant_id"],
        currency=row["currency"],
        subtotal_minor=int(row["subtotal_minor"]),
        total_minor=int(row["total_minor"]),
        status=row["status"],
        quote_expires_at=parse_ts(row.get("quote_expires_at")),
        created_at=parse_ts(row["created_at"]),  # type: ignore[arg-type]
        updated_at=parse_ts(row["updated_at"]),  # type: ignore[arg-type]
        cart_sha256=row.get("cart_sha256"),
    )


class SupabaseOrderRepository(_SupabaseBase):
    async def expire_stale_orders(self, limit: int = 100) -> None:
        await self._rpc("expire_stale_orders", {"p_limit": limit})

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
        row = await self._rpc_one(
            "create_order_from_quote",
            {
                "p_user_id": user_id,
                "p_device_id": device_id,
                "p_merchant_code": merchant_code,
                "p_currency": currency,
                "p_quote_expires_at": quote_expires_at.isoformat(),
                "p_quote_reference": quote_reference,
                "p_cart_sha256": cart_sha256,
                "p_items": [dict(i) for i in items],
                "p_total_minor": total_minor,
            },
        )
        return RpcResult(
            bool(row["ok"]),
            row["reason"],
            {"order_id": row.get("order_id"), "deduplicated": bool(row.get("deduplicated"))},
        )

    async def get_for_user(self, order_id: str, user_id: str) -> OrderRecord | None:
        response = await self._run(
            lambda c: c.table("orders")
            .select(_ORDER_COLUMNS)
            .eq("id", order_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return _order(rows[0]) if rows else None

    async def list_items(self, order_id: str) -> list[OrderItemRecord]:
        response = await self._run(
            lambda c: c.table("order_items")
            .select("product_reference,description,quantity,unit_price_minor,line_total_minor")
            .eq("order_id", order_id)
            .order("product_reference")
            .execute()
        )
        return [
            OrderItemRecord(
                r["product_reference"],
                r.get("description"),
                int(r["quantity"]),
                int(r["unit_price_minor"]),
                int(r["line_total_minor"]),
            )
            for r in response.data or []
        ]

    async def expire(self, order_id: str) -> RpcResult:
        row = await self._rpc_one("expire_order", {"p_order_id": order_id})
        return RpcResult(bool(row["ok"]), row["reason"], {"status": row.get("status")})

    async def cancel(
        self, *, order_id: str, user_id: str, actor_type: str, actor_id: str | None
    ) -> RpcResult:
        row = await self._rpc_one(
            "cancel_order",
            {
                "p_order_id": order_id,
                "p_user_id": user_id,
                "p_actor_type": actor_type,
                "p_actor_id": actor_id,
            },
        )
        return RpcResult(
            bool(row["ok"]), row["reason"], {"order_id": row.get("order_id"), "status": row.get("status")}
        )

    async def list_pending_for_device(self, device_id: str) -> list[OrderRecord]:
        response = await self._run(
            lambda c: c.table("orders")
            .select(_ORDER_COLUMNS)
            .eq("device_id", device_id)
            .eq("status", "AWAITING_APPROVAL")
            .order("created_at")
            .execute()
        )
        return [_order(r) for r in response.data or []]
