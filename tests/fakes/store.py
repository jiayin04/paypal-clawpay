"""In-memory stand-in for the Supabase repositories.

Each RPC method holds one asyncio.Lock (the Python stand-in for a Postgres
transaction) and returns the same reason codes as the SQL functions in
supabase/migrations/2026102509*.sql. Hooks:

- ``fail_next(method, code, constraint=None, times=1)``: raise RepositoryError.
- ``before_rpc(method, callback)``: run ``callback(store)`` before the method body
  (e.g. revoke a device between a pre-check and the RPC).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from app.domain.order_state import assert_transition
from app.repositories.errors import RepositoryError
from app.repositories.protocols import (
    DeviceRecord,
    MerchantRecord,
    OrderItemRecord,
    OrderRecord,
    RpcResult,
    SpendingSummaryRow,
)
from app.services.container import Repositories
from tests.fakes.clock import FakeClock

GROCER_ID = "aaaaaaaa-0000-0000-0000-0000000000a1"
ELECTRONICS_ID = "aaaaaaaa-0000-0000-0000-0000000000e1"


class InMemoryStore:
    def __init__(self, clock: FakeClock | None = None, timezone: str = "Asia/Kuala_Lumpur") -> None:
        from zoneinfo import ZoneInfo

        self.clock = clock or FakeClock()
        self.tz = ZoneInfo(timezone)
        self.lock = asyncio.Lock()
        self.users: set[str] = set()
        self.devices: dict[str, dict[str, Any]] = {}
        self.merchants: dict[str, dict[str, Any]] = {
            GROCER_ID: {"id": GROCER_ID, "merchant_code": "grocer", "display_name": "Grocer Demo", "status": "active"},
            ELECTRONICS_ID: {
                "id": ELECTRONICS_ID,
                "merchant_code": "electronics",
                "display_name": "Electronics Demo",
                "status": "active",
            },
        }
        self.orders: dict[str, dict[str, Any]] = {}
        self.order_items: list[dict[str, Any]] = []
        self.spending_records: list[dict[str, Any]] = []
        self.payment_methods: list[dict[str, Any]] = []
        self.conversations: dict[str, dict[str, Any]] = {}
        self.conversation_events: list[dict[str, Any]] = []
        self.a2a_messages: list[dict[str, Any]] = []
        self.audit_logs: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self._failures: dict[str, list[tuple[str, str | None]]] = {}
        self._before: dict[str, list[Callable[[InMemoryStore], Any]]] = {}

    # -- hooks -------------------------------------------------------------------------

    def fail_next(self, method: str, code: str, constraint: str | None = None, times: int = 1) -> None:
        self._failures.setdefault(method, []).extend([(code, constraint)] * times)

    def before_rpc(self, method: str, callback: Callable[[InMemoryStore], Any]) -> None:
        self._before.setdefault(method, []).append(callback)

    async def _enter(self, method: str) -> None:
        self.calls.append(method)
        for callback in self._before.pop(method, []):
            result = callback(self)
            if asyncio.iscoroutine(result):
                await result
        pending = self._failures.get(method)
        if pending:
            code, constraint = pending.pop(0)
            raise RepositoryError(code, constraint)

    # -- fixtures ----------------------------------------------------------------------

    def now(self) -> datetime:
        return self.clock.utc_now()

    def add_user(self, user_id: str | None = None) -> str:
        user_id = user_id or str(uuid.uuid4())
        self.users.add(user_id)
        return user_id

    def add_device(
        self,
        user_id: str,
        *,
        token_hash: str | None = None,
        llm_token_hash: str | None = None,
        status: str = "active",
        public_key: str | None = None,
        key_version: int = 1,
    ) -> str:
        device_id = str(uuid.uuid4())
        self.devices[device_id] = {
            "id": device_id,
            "user_id": user_id,
            "device_name": "test device",
            "public_key": public_key or f"pk-{device_id}",
            "key_algorithm": "ecdsa-p256-sha256",
            "key_version": key_version,
            "status": status,
            "token_hash": token_hash,
            "llm_token_hash": llm_token_hash,
            "token_issued_at": self.now(),
            "last_seen_at": None,
            "created_at": self.now(),
            "revoked_at": self.now() if status == "revoked" else None,
        }
        return device_id

    def add_payment_method(self, user_id: str, status: str = "active") -> None:
        self.payment_methods.append({"id": str(uuid.uuid4()), "user_id": user_id, "status": status})

    def add_spending(self, user_id: str, amount_minor: int, status: str = "committed") -> None:
        self.spending_records.append(
            {
                "user_id": user_id,
                "order_id": str(uuid.uuid4()),
                "amount_minor": amount_minor,
                "currency": "MYR",
                "status": status,
                "spending_day": self.now().astimezone(self.tz).date(),
            }
        )

    def set_order_status(self, order_id: str, status: str) -> None:
        """Test helper that bypasses RPCs but still enforces the transition trigger."""
        order = self.orders[order_id]
        assert_transition(order["status"], status)
        order["status"] = status

    def audit_events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        return [a for a in self.audit_logs if event_type is None or a["event_type"] == event_type]

    def _audit(self, user_id: str | None, actor_type: str, actor_id: str | None, event: str,
               entity_type: str, entity_id: str, details: Mapping[str, Any] | None = None) -> None:
        self.audit_logs.append(
            {
                "user_id": user_id,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "event_type": event,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "details": dict(details or {}),
                "created_at": self.now(),
            }
        )

    def _transition(self, order: dict[str, Any], to: str, actor_type: str, actor_id: str | None,
                    reason: str) -> None:
        assert_transition(order["status"], to)  # mirrors orders_enforce_status_transition
        self._audit(order["user_id"], actor_type, actor_id, "order.status_changed", "order", order["id"],
                    {"from": order["status"], "to": to, "reason": reason})
        order["status"] = to
        order["updated_at"] = self.now()

    def repositories(self) -> Repositories:
        return Repositories(
            devices=_Devices(self),
            merchants=_Merchants(self),
            orders=_Orders(self),
            spending=_Spending(self),
            payment_methods=_PaymentMethods(self),
            conversations=_Conversations(self),
            a2a_messages=_Insert(self, self.a2a_messages, "a2a_messages.insert"),
            audit=_Insert(self, self.audit_logs, "audit_logs.insert"),
        )

    def user_reader_factory(self) -> Callable[[str], Any]:
        """Simulates RLS: the JWT is mapped to ``sub`` by ``jwt_subjects``."""
        store = self

        @asynccontextmanager
        async def factory(token: str):  # type: ignore[no-untyped-def]
            import jwt as pyjwt

            # The route already verified the JWT; take sub like PostgREST's auth.uid().
            sub = pyjwt.decode(token, options={"verify_signature": False})["sub"]
            yield _UserReader(store, sub)

        return factory


def _device_record(row: Mapping[str, Any]) -> DeviceRecord:
    return DeviceRecord(row["id"], row["user_id"], row["status"], row["key_version"], row["public_key"],
                        row["last_seen_at"])


class _Devices:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def find_by_token_hash(self, token_hash: str) -> DeviceRecord | None:
        await self.s._enter("devices.find_by_token_hash")
        for row in self.s.devices.values():
            if row["token_hash"] == token_hash:
                return _device_record(row)
        return None

    async def find_by_llm_token_hash(self, token_hash: str) -> DeviceRecord | None:
        await self.s._enter("devices.find_by_llm_token_hash")
        for row in self.s.devices.values():
            if row["llm_token_hash"] == token_hash:
                return _device_record(row)
        return None

    async def touch_last_seen(self, device_id: str, at: datetime) -> None:
        await self.s._enter("devices.touch_last_seen")
        if device_id in self.s.devices:
            self.s.devices[device_id]["last_seen_at"] = at

    async def register(self, *, user_id: str, device_name: str, public_key: str, key_version: int,
                       token_hash: str, llm_token_hash: str, actor_type: str,
                       actor_id: str | None) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("register_device")
            if user_id not in s.users:
                return RpcResult(False, "user_not_found", {"device_id": None})
            if any(d["public_key"] == public_key and d["status"] != "revoked" for d in s.devices.values()):
                return RpcResult(False, "public_key_already_registered", {"device_id": None})
            if any(d["token_hash"] == token_hash for d in s.devices.values()):
                raise RepositoryError("23505", "devices_token_hash_key")
            device_id = s.add_device(user_id, token_hash=token_hash, llm_token_hash=llm_token_hash,
                                     public_key=public_key, key_version=key_version)
            s.devices[device_id]["device_name"] = device_name
            s._audit(user_id, actor_type, actor_id, "device.registered", "device", device_id,
                     {"key_version": key_version})
            s._audit(user_id, actor_type, actor_id, "device.token_issued", "device", device_id,
                     {"kinds": ["device", "llm"]})
            return RpcResult(True, "registered", {"device_id": device_id})

    async def revoke(self, *, device_id: str, user_id: str | None, actor_type: str,
                     actor_id: str | None) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("revoke_device")
            device = s.devices.get(device_id)
            if device is None or (user_id is not None and device["user_id"] != user_id):
                return RpcResult(False, "not_found", {"device_id": None, "cancelled_orders": 0})
            reason = "already_revoked"
            if device["status"] != "revoked":
                device["status"] = "revoked"
                device["revoked_at"] = s.now()
                reason = "revoked"
                s._audit(device["user_id"], actor_type, actor_id, "device.revoked", "device", device_id)
            cancelled = 0
            for order in sorted(s.orders.values(), key=lambda o: o["id"]):
                if order["device_id"] == device_id and order["status"] in ("CREATED", "QUOTED", "AWAITING_APPROVAL"):
                    s._transition(order, "CANCELLED", actor_type, actor_id, "device_revoked")
                    cancelled += 1
            return RpcResult(True, reason, {"device_id": device_id, "cancelled_orders": cancelled})


class _Merchants:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def get_active_by_code(self, merchant_code: str) -> MerchantRecord | None:
        await self.s._enter("merchants.get_active_by_code")
        for m in self.s.merchants.values():
            if m["merchant_code"] == merchant_code and m["status"] == "active":
                return MerchantRecord(m["id"], m["merchant_code"], m["display_name"], m["status"])
        return None

    async def get_by_id(self, merchant_id: str) -> MerchantRecord | None:
        m = self.s.merchants.get(merchant_id)
        return MerchantRecord(m["id"], m["merchant_code"], m["display_name"], m["status"]) if m else None


def _order_record(o: Mapping[str, Any]) -> OrderRecord:
    return OrderRecord(o["id"], o["user_id"], o["device_id"], o["merchant_id"], o["currency"],
                       o["subtotal_minor"], o["total_minor"], o["status"], o["quote_expires_at"],
                       o["created_at"], o["updated_at"], o.get("cart_sha256"))


class _Orders:
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

    async def cancel(self, *, order_id: str, user_id: str, actor_type: str, actor_id: str | None) -> RpcResult:
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


class _Spending:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def summary(self, user_id: str, timezone: str) -> SpendingSummaryRow:
        from zoneinfo import ZoneInfo

        await self.s._enter("spending_summary")
        day = self.s.now().astimezone(ZoneInfo(timezone)).date()
        rows = [r for r in self.s.spending_records if r["user_id"] == user_id and r["spending_day"] == day]
        return SpendingSummaryRow(
            day,
            sum(r["amount_minor"] for r in rows if r["status"] == "committed"),
            sum(r["amount_minor"] for r in rows if r["status"] == "reserved"),
        )


class _PaymentMethods:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def has_active(self, user_id: str) -> bool:
        await self.s._enter("payment_methods.has_active")
        return any(p["user_id"] == user_id and p["status"] == "active" for p in self.s.payment_methods)


class _Conversations:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def upsert_conversation(self, *, user_id: str, device_id: str, external_session_id: str) -> str:
        await self.s._enter("conversations.upsert")
        for conv in self.s.conversations.values():
            if conv["external_session_id"] == external_session_id:
                return conv["id"]
        conv_id = str(uuid.uuid4())
        self.s.conversations[conv_id] = {"id": conv_id, "user_id": user_id, "device_id": device_id,
                                         "source": "esp_claw", "external_session_id": external_session_id}
        return conv_id

    async def add_event(self, *, conversation_id: str, event_type: str, summary: Mapping[str, Any],
                        order_id: str | None = None, correlation_id: str | None = None) -> None:
        await self.s._enter("conversation_events.insert")
        self.s.conversation_events.append({
            "conversation_id": conversation_id, "event_type": event_type, "summary": dict(summary),
            "payload": {}, "order_id": order_id, "correlation_id": correlation_id,
        })


class _Insert:
    def __init__(self, store: InMemoryStore, table: list[dict[str, Any]], name: str) -> None:
        self.s = store
        self.table = table
        self.name = name

    async def insert(self, row: Mapping[str, Any]) -> None:
        await self.s._enter(self.name)
        self.table.append(dict(row))


class _UserReader:
    """Simulated RLS: every read is filtered by the JWT subject."""

    def __init__(self, store: InMemoryStore, user_id: str) -> None:
        self.s = store
        self.user_id = user_id

    async def list_orders(self, *, limit: int, before: datetime | None) -> list[dict[str, Any]]:
        rows = [o for o in self.s.orders.values() if o["user_id"] == self.user_id
                and (before is None or o["created_at"] < before)]
        rows.sort(key=lambda o: o["created_at"], reverse=True)
        cols = ("id", "merchant_id", "currency", "total_minor", "status", "quote_expires_at", "created_at", "updated_at")
        return [{k: (o[k].isoformat() if isinstance(o[k], datetime) else o[k]) for k in cols} for o in rows[:limit]]

    async def get_order_detail(self, order_id: str) -> dict[str, Any] | None:
        order = self.s.orders.get(order_id)
        if order is None or order["user_id"] != self.user_id:
            return None
        cols = ("id", "merchant_id", "currency", "subtotal_minor", "total_minor", "status", "quote_expires_at",
                "created_at", "updated_at", "device_id")
        out = {k: (order[k].isoformat() if isinstance(order[k], datetime) else order[k]) for k in cols}
        out["items"] = [
            {k: i[k] for k in ("product_reference", "description", "quantity", "unit_price_minor", "line_total_minor")}
            for i in self.s.order_items if i["order_id"] == order_id
        ]
        out["approvals"] = []
        out["payment_attempts"] = []
        out["merchant"] = self.s.merchants[order["merchant_id"]]["display_name"]
        out["mandate_status"] = None
        return out

    async def list_devices(self) -> list[dict[str, Any]]:
        cols = ("id", "device_name", "key_algorithm", "key_version", "status", "created_at", "revoked_at", "last_seen_at")
        return [
            {k: (d[k].isoformat() if isinstance(d[k], datetime) else d[k]) for k in cols}
            for d in self.s.devices.values() if d["user_id"] == self.user_id
        ]
