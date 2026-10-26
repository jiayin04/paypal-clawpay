"""In-memory orders adapter sharing the store transaction lock."""
from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

from app.repositories.ports.common import RpcResult
from app.repositories.ports.orders import OrderItemRecord, OrderRecord


if TYPE_CHECKING:
    from tests.fakes.store import InMemoryStore


def _order_record(o: Mapping[str, Any]) -> OrderRecord:
    return OrderRecord(
        o["id"],
        o["user_id"],
        o["device_id"],
        o["merchant_id"],
        o["currency"],
        o.get("subtotal_minor", o.get("total_minor", 0)),
        o["total_minor"],
        o["status"],
        o["quote_expires_at"],
        o["created_at"],
        o["updated_at"],
        o.get("cart_sha256"),
    )


class FakeOrderRepository:
    async def expire_stale_orders(self, limit: int = 100) -> None:
        for row in list(self.s.orders.values())[:limit]:
            if row['status'] in ('CREATED','QUOTED','AWAITING_APPROVAL') and row.get('quote_expires_at') and row['quote_expires_at'] <= self.s.now():
                await self.expire(row['id'])

    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def create_from_quote(self, *, user_id: str, device_id: str, merchant_code: str, currency: str,
                                quote_expires_at: datetime, quote_reference: str | None, cart_sha256: str,
                                items: Sequence[Mapping[str, Any]], total_minor: int) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("create_order_from_quote")
            device = s.devices.get(device_id)
            if device is None or device["user_id"] != user_id or device["status"] != "active":
                return RpcResult(False, "device_revoked", {"order_id": None, "deduplicated": False})
            merchant = next((m for m in s.merchants.values()
                             if m["merchant_code"] == merchant_code and m["status"] == "active"), None)
            if merchant is None:
                return RpcResult(False, "merchant_unavailable", {"order_id": None, "deduplicated": False})
            now = s.now()
            candidates = [
                o for o in s.orders.values()
                if o["device_id"] == device_id and o["cart_sha256"] == cart_sha256
                and o["merchant_id"] == merchant["id"] and o["currency"] == currency
                and o["status"] == "AWAITING_APPROVAL"
                and o["created_at"] > now - timedelta(seconds=60)
                and o["quote_expires_at"] > now
            ]
            if candidates:
                newest = max(candidates, key=lambda o: o["created_at"])
                return RpcResult(True, "deduplicated", {"order_id": newest["id"], "deduplicated": True})
            if not items:
                raise AssertionError("create_order_from_quote: items must be a non-empty array")
            total = 0
            for item in items:
                assert item["qty"] > 0 and item["unit_price_minor"] > 0 and item["sku"]
                total += item["qty"] * item["unit_price_minor"]
            if total != total_minor:
                raise AssertionError("create_order_from_quote: item sum does not match total")
            order_id = str(uuid.uuid4())
            order = {
                "id": order_id, "user_id": user_id, "device_id": device_id, "merchant_id": merchant["id"],
                "currency": currency, "subtotal_minor": total, "total_minor": total, "status": "CREATED",
                "idempotency_key": f"mcp:{uuid.uuid4()}", "quote_expires_at": quote_expires_at,
                "cart_sha256": cart_sha256, "quote_reference": quote_reference,
                "created_at": now, "updated_at": now,
            }
            s.orders[order_id] = order
            for item in items:
                s.order_items.append({
                    "order_id": order_id, "product_reference": item["sku"], "description": item.get("name"),
                    "quantity": item["qty"], "unit_price_minor": item["unit_price_minor"],
                    "line_total_minor": item["qty"] * item["unit_price_minor"],
                })
            s._transition(order, "QUOTED", "device", device_id, "quote_validated")
            s._transition(order, "AWAITING_APPROVAL", "device", device_id, "order_created")
            s._audit(user_id, "device", device_id, "order.created", "order", order_id,
                     {"merchant": merchant_code, "total_minor": total, "currency": currency})
            return RpcResult(True, "created", {"order_id": order_id, "deduplicated": False})

    async def get_for_user(self, order_id: str, user_id: str) -> OrderRecord | None:
        await self.s._enter("orders.get_for_user")
        order = self.s.orders.get(order_id)
        if order is None or order["user_id"] != user_id:
            return None
        return _order_record(order)

    async def list_items(self, order_id: str) -> list[OrderItemRecord]:
        return [
            OrderItemRecord(i["product_reference"], i["description"], i["quantity"], i["unit_price_minor"],
                            i["line_total_minor"])
            for i in self.s.order_items if i["order_id"] == order_id
        ]

    def _release(self, order_id: str) -> int:
        released = 0
        for record in self.s.spending_records:
            if record["order_id"] == order_id and record["status"] == "reserved":
                record["status"] = "released"
                released += 1
        return released

    async def expire(self, order_id: str) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("expire_order")
            order = s.orders.get(order_id)
            if order is None:
                return RpcResult(False, "order_not_found", {"status": None})
            expires = order["quote_expires_at"]
            if order["status"] not in ("CREATED", "QUOTED", "AWAITING_APPROVAL") or expires is None or expires > s.now():
                return RpcResult(False, "not_expirable", {"status": order["status"]})
            s._transition(order, "EXPIRED", "system", None, "quote_expired")
            s._audit(order["user_id"], "system", None, "order.expired", "order", order_id)
            if self._release(order_id):
                s._audit(order["user_id"], "system", None, "spending.released", "order", order_id,
                         {"reason": "expired"})
            return RpcResult(True, "expired", {"status": "EXPIRED"})

    async def cancel(
        self,
        *,
        order_id: str,
        user_id: str,
        actor_type: str,
        actor_id: str | None,
    ) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("cancel_order")
            order = s.orders.get(order_id)
            if order is None or order["user_id"] != user_id:
                return RpcResult(False, "not_found", {"order_id": None, "status": None})
            if order["status"] not in ("CREATED", "QUOTED", "AWAITING_APPROVAL", "APPROVED"):
                return RpcResult(False, "invalid_order_status", {"order_id": order_id, "status": order["status"]})
            s._transition(order, "CANCELLED", actor_type, actor_id, "cancelled")
            s._audit(user_id, actor_type, actor_id, "order.cancelled", "order", order_id)
            if self._release(order_id):
                s._audit(user_id, actor_type, actor_id, "spending.released", "order", order_id,
                         {"reason": "cancelled"})
            return RpcResult(True, "cancelled", {"order_id": order_id, "status": "CANCELLED"})

    async def list_pending_for_device(self, device_id: str) -> list[OrderRecord]:
        await self.s._enter("orders.list_pending_for_device")
        now = self.s.now()
        candidates = [
            o for o in self.s.orders.values()
            if o.get("device_id") == device_id
            and o.get("status") in ("AWAITING_APPROVAL", "awaiting_device_approval")
            and (o.get("quote_expires_at") is None or o["quote_expires_at"] > now)
        ]
        candidates.sort(key=lambda o: o.get("created_at", now))
        return [_order_record(o) for o in candidates]
