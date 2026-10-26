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
    ApprovalChallengeRecord,
    DeviceRecord,
    MerchantRecord,
    OrderItemRecord,
    OrderRecord,
    PaymentAttemptRecord,
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
        self.approval_challenges: dict[str, dict[str, Any]] = {}
        self.approvals: list[dict[str, Any]] = []
        self.mandates: list[dict[str, Any]] = []
        self.payment_attempts: dict[str, dict[str, Any]] = {}
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

    def add_payment_method(
        self,
        user_id: str,
        status: str = "active",
        vault_token_reference: str | None = None,
        vault_token: str | None = None,
    ) -> None:
        pm_id = str(uuid.uuid4())
        self.payment_methods.append({
            "id": pm_id,
            "user_id": user_id,
            "status": status,
            "vault_token_reference": vault_token_reference or vault_token or f"vault-{pm_id}",
        })

    def add_merchant(self, merchant_code: str = "grocer", display_name: str = "Grocer Demo") -> str:
        for m_id, m in self.merchants.items():
            if m.get("merchant_code") == merchant_code:
                return m_id
        m_id = str(uuid.uuid4())
        self.merchants[m_id] = {
            "id": m_id,
            "merchant_code": merchant_code,
            "display_name": display_name,
            "status": "active",
        }
        return m_id

    def create_order(
        self,
        *,
        user_id: str,
        merchant_id: str,
        device_id: str,
        total_minor: int,
        currency: str,
        items: list[dict[str, Any]] | None = None,
        quote_expires_at: datetime | None = None,
        status: str = "created",
    ) -> str:
        order_id = str(uuid.uuid4())
        self.orders[order_id] = {
            "id": order_id,
            "user_id": user_id,
            "merchant_id": merchant_id,
            "device_id": device_id,
            "status": status,
            "subtotal_minor": total_minor,
            "total_minor": total_minor,
            "currency": currency,
            "quote_expires_at": quote_expires_at or (self.now() + timedelta(minutes=15)),
            "created_at": self.now(),
            "updated_at": self.now(),
        }
        if items:
            for item in items:
                self.order_items.append({
                    "id": str(uuid.uuid4()),
                    "order_id": order_id,
                    "product_reference": item.get("reference", "item-1"),
                    "description": item.get("name", "Item"),
                    "quantity": item.get("qty", 1),
                    "unit_price_minor": total_minor // max(1, item.get("qty", 1)),
                    "line_total_minor": total_minor,
                })
        return order_id

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
                "event": event,
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

    @property
    def payments(self) -> _Payments:
        return _Payments(self)

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
            approvals=_Approvals(self),
            payments=_Payments(self),
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

    async def list_active(self, user_id: str) -> list[dict[str, Any]]:
        await self.s._enter("payment_methods.list_active")
        cols = ("id", "user_id", "status", "payer_email_masked", "created_at", "updated_at")
        return [
            {k: p.get(k) for k in cols}
            for p in self.s.payment_methods
            if p["user_id"] == user_id and p["status"] == "active"
        ]

    async def get_by_id(self, payment_method_id: str, user_id: str) -> dict[str, Any] | None:
        await self.s._enter("payment_methods.get_by_id")
        for p in self.s.payment_methods:
            if p["id"] == payment_method_id and p["user_id"] == user_id:
                return dict(p)
        return None

    async def create_pending(
        self,
        *,
        user_id: str,
        state_token: str,
        state_expires_at: datetime,
    ) -> str:
        await self.s._enter("payment_methods.create_pending")
        pm_id = str(uuid.uuid4())
        self.s.payment_methods.append({
            "id": pm_id,
            "user_id": user_id,
            "status": "pending",
            "state_token": state_token,
            "state_expires_at": state_expires_at,
            "vault_token_reference": None,
            "payer_email_masked": None,
            "created_at": self.s.now(),
            "updated_at": self.s.now(),
        })
        return pm_id

    async def activate_payment_method(
        self,
        *,
        payment_method_id: str,
        user_id: str,
        vault_token_reference: str,
        payer_email_masked: str | None,
    ) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("activate_payment_method")
            pm = next((p for p in s.payment_methods if p["id"] == payment_method_id and p["user_id"] == user_id), None)
            if pm is None:
                return RpcResult(False, "not_found", {})
            # Deactivate any existing active payment methods
            for other in s.payment_methods:
                if other["user_id"] == user_id and other["status"] == "active" and other["id"] != payment_method_id:
                    other["status"] = "revoked"
            pm["status"] = "active"
            pm["vault_token_reference"] = vault_token_reference
            pm["payer_email_masked"] = payer_email_masked
            pm["updated_at"] = s.now()
            s._audit(user_id, "user", user_id, "payment_method.activated", "payment_method", payment_method_id, {})
            return RpcResult(True, "activated", {"payment_method_id": payment_method_id})

    async def revoke_payment_method(
        self,
        *,
        payment_method_id: str,
        user_id: str,
    ) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("revoke_payment_method")
            pm = next((p for p in s.payment_methods if p["id"] == payment_method_id and p["user_id"] == user_id), None)
            if pm is None:
                return RpcResult(False, "not_found", {})
            if pm["status"] != "active":
                return RpcResult(False, "not_found", {})
            # Check for in-progress payment attempts
            for att in s.payment_attempts.values():
                order = s.orders.get(att["order_id"])
                if order and order["user_id"] == user_id:
                    if att.get("payment_method_id") == payment_method_id and att["status"] in ("initiated", "pending", "unknown"):
                        return RpcResult(False, "payment_in_progress", {})
            vault_ref = pm.get("vault_token_reference")
            pm["status"] = "revoked"
            pm["updated_at"] = s.now()
            s._audit(user_id, "user", user_id, "payment_method.revoked", "payment_method", payment_method_id, {})
            return RpcResult(True, "revoked", {"vault_token_reference": vault_ref})

    async def find_by_state(self, state_token: str) -> dict[str, Any] | None:
        s = self.s
        async with s.lock:
            await s._enter("payment_methods.find_by_state")
            now = s.now()
            for pm in s.payment_methods:
                if pm.get("state_token") == state_token and pm["status"] == "pending":
                    if pm.get("state_expires_at") and pm["state_expires_at"] > now:
                        # Single-use: clear state token so it can't be reused
                        pm["state_token"] = None
                        return dict(pm)
            return None


class _Approvals:
    """Fake ApprovalRepository mirroring SQL RPCs in phase3 migration."""

    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def issue_challenge(
        self,
        *,
        order_id: str,
        device_id: str,
        user_id: str,
        key_version: int,
        nonce: str,
        canonical: str,
        payload_sha256: str,
        amount_minor: int,
        currency: str,
        expires_at: datetime,
    ) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("issue_approval_challenge")
            order = s.orders.get(order_id)
            if order is None or order["user_id"] != user_id or order["device_id"] != device_id:
                return RpcResult(False, "order_not_found", {"challenge_id": None})
            device = s.devices.get(device_id)
            if device is None or device["status"] != "active":
                return RpcResult(False, "device_revoked", {"challenge_id": None})
            if order["status"] != "AWAITING_APPROVAL":
                return RpcResult(False, "invalid_order_status", {"challenge_id": None})
            now = s.now()
            if order["quote_expires_at"] is not None and order["quote_expires_at"] <= now:
                order["status"] = "EXPIRED"
                return RpcResult(False, "quote_expired", {"challenge_id": None})
            # Return existing open challenge if any
            for ch in s.approval_challenges.values():
                if (
                    ch["order_id"] == order_id
                    and ch["device_id"] == device_id
                    and ch["consumed_at"] is None
                    and ch["invalidated_at"] is None
                    and ch["expires_at"] > now
                ):
                    return RpcResult(
                        True,
                        "existing",
                        {
                            "challenge_id": ch["id"],
                            "nonce": ch["nonce"],
                            "canonical_payload": ch["canonical_payload"],
                            "expires_at": ch["expires_at"],
                            "key_version": ch["key_version"],
                        },
                    )
            # Invalidate expired open challenges
            for ch in s.approval_challenges.values():
                if (
                    ch["order_id"] == order_id
                    and ch["device_id"] == device_id
                    and ch["consumed_at"] is None
                    and ch["invalidated_at"] is None
                    and ch["expires_at"] <= now
                ):
                    ch["invalidated_at"] = now
            ch_id = str(uuid.uuid4())
            s.approval_challenges[ch_id] = {
                "id": ch_id,
                "order_id": order_id,
                "device_id": device_id,
                "user_id": user_id,
                "nonce": nonce,
                "canonical_payload": canonical,
                "payload_sha256": payload_sha256,
                "amount_minor": amount_minor,
                "currency": currency,
                "key_version": key_version,
                "expires_at": expires_at,
                "consumed_at": None,
                "invalidated_at": None,
                "created_at": now,
            }
            s._audit(user_id, "device", device_id, "approval.challenge_issued", "challenge", ch_id,
                     {"order_id": order_id, "expires_at": expires_at})
            return RpcResult(
                True,
                "issued",
                {
                    "challenge_id": ch_id,
                    "nonce": nonce,
                    "canonical_payload": canonical,
                    "expires_at": expires_at,
                    "key_version": key_version,
                },
            )

    async def get_challenge(self, challenge_id: str) -> ApprovalChallengeRecord | None:
        await self.s._enter("approval_challenges.get")
        ch = self.s.approval_challenges.get(challenge_id)
        if ch is None:
            return None
        return ApprovalChallengeRecord(
            id=ch["id"],
            order_id=ch["order_id"],
            device_id=ch["device_id"],
            user_id=ch["user_id"],
            nonce=ch["nonce"],
            canonical_payload=ch["canonical_payload"],
            payload_sha256=ch["payload_sha256"],
            amount_minor=ch["amount_minor"],
            currency=ch["currency"],
            key_version=ch["key_version"],
            expires_at=ch["expires_at"],
            consumed_at=ch["consumed_at"],
            invalidated_at=ch["invalidated_at"],
            created_at=ch["created_at"],
        )

    async def record_denial(
        self,
        *,
        challenge_id: str,
        device_id: str,
        user_id: str,
        nonce: str,
        payload_sha256: str,
        key_version: int,
        actor_type: str,
        actor_id: str | None,
    ) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("record_gadget_denial")
            ch = s.approval_challenges.get(challenge_id)
            if ch is None or ch["device_id"] != device_id or ch["user_id"] != user_id:
                return RpcResult(False, "challenge_not_found", {})
            device = s.devices.get(device_id)
            if device is None or device["status"] != "active":
                return RpcResult(False, "device_revoked", {})
            order = s.orders.get(ch["order_id"])
            if order is None or order["status"] != "AWAITING_APPROVAL":
                return RpcResult(False, "invalid_order_status", {})
            if ch["consumed_at"] is not None or ch["invalidated_at"] is not None:
                return RpcResult(False, "challenge_consumed", {})
            now = s.now()
            if ch["expires_at"] <= now:
                return RpcResult(False, "challenge_expired", {})
            if ch["key_version"] != key_version:
                return RpcResult(False, "key_version_mismatch", {})
            if ch["nonce"] != nonce:
                return RpcResult(False, "nonce_mismatch", {})
            ch["consumed_at"] = now
            s.approvals.append({
                "order_id": ch["order_id"], "user_id": user_id, "device_id": device_id,
                "approval_type": "gadget", "decision": "denied", "verification_status": "pending",
                "nonce": nonce, "signed_payload_hash": payload_sha256, "key_version": key_version,
                "consumed_at": None, "approved_at": None, "expires_at": ch["expires_at"],
            })
            s._transition(order, "DENIED", actor_type, actor_id, "denied")
            s._audit(user_id, actor_type, actor_id, "approval.denied", "order", ch["order_id"],
                     {"challenge_id": challenge_id})
            return RpcResult(True, "denied", {})

    def _reserve_spending(
        self, user_id: str, order_id: str, amount_minor: int, currency: str,
        daily_limit_minor: int, timezone: str
    ) -> tuple[bool, str]:
        from zoneinfo import ZoneInfo
        day = self.s.now().astimezone(ZoneInfo(timezone)).date()
        existing_currency = next(
            (r["currency"] for r in self.s.spending_records if r["user_id"] == user_id
             and r["spending_day"] == day and r["status"] in ("committed", "reserved")), None
        )
        if existing_currency is not None and existing_currency != currency:
            return False, "currency_mismatch"
        committed = sum(
            r["amount_minor"] for r in self.s.spending_records
            if r["user_id"] == user_id and r["spending_day"] == day
            and r["status"] in ("committed", "reserved")
        )
        if committed + amount_minor > daily_limit_minor:
            return False, "daily_limit_exceeded"
        self.s.spending_records.append({
            "user_id": user_id, "order_id": order_id, "amount_minor": amount_minor,
            "currency": currency, "status": "reserved", "spending_day": day,
        })
        return True, "reserved"

    async def accept_approval(
        self,
        *,
        challenge_id: str,
        device_id: str,
        user_id: str,
        nonce: str,
        payload_sha256: str,
        policy_currency: str,
        daily_limit_minor: int,
        timezone: str,
        phone_threshold_minor: int,
    ) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("accept_gadget_approval")
            ch = s.approval_challenges.get(challenge_id)
            if ch is None or ch["device_id"] != device_id or ch["user_id"] != user_id:
                return RpcResult(False, "challenge_not_found", {})
            device = s.devices.get(device_id)
            if device is None or device["status"] != "active":
                return RpcResult(False, "device_revoked", {})
            order = s.orders.get(ch["order_id"])
            if order is None or order["status"] != "AWAITING_APPROVAL":
                return RpcResult(False, "invalid_order_status", {})
            now = s.now()
            if order["quote_expires_at"] is not None and order["quote_expires_at"] <= now:
                order["status"] = "EXPIRED"
                return RpcResult(False, "quote_expired", {})
            if ch["consumed_at"] is not None or ch["invalidated_at"] is not None:
                return RpcResult(False, "challenge_consumed", {})
            if ch["expires_at"] <= now:
                return RpcResult(False, "challenge_expired", {})
            if device["key_version"] != ch["key_version"]:
                return RpcResult(False, "key_version_mismatch", {})
            if ch["nonce"] != nonce:
                return RpcResult(False, "nonce_mismatch", {})
            if ch["payload_sha256"] != payload_sha256:
                return RpcResult(False, "hash_mismatch", {})
            # Phone threshold (unconditional)
            if order["total_minor"] > phone_threshold_minor:
                return RpcResult(False, "phone_approval_required", {})
            # Active payment method required
            pm = next(
                (p for p in s.payment_methods
                 if p["user_id"] == user_id and p["status"] == "active"), None
            )
            if pm is None:
                return RpcResult(False, "no_payment_method", {})
            vault_token = pm.get("vault_token_reference", "vault-token")
            # reserve_spending
            sp_ok, sp_reason = self._reserve_spending(
                user_id, ch["order_id"], order["total_minor"],
                ch["currency"], daily_limit_minor, timezone,
            )
            if not sp_ok:
                if sp_reason == "currency_mismatch":
                    return RpcResult(False, "currency_not_supported", {})
                return RpcResult(False, sp_reason, {})
            # Commit: approval, challenge consumed, mandate, transitions, attempt
            ch["consumed_at"] = now
            s.approvals.append({
                "order_id": ch["order_id"], "user_id": user_id, "device_id": device_id,
                "approval_type": "gadget", "decision": "approved", "verification_status": "verified",
                "nonce": nonce, "signed_payload_hash": payload_sha256, "key_version": ch["key_version"],
                "consumed_at": now, "approved_at": now, "expires_at": ch["expires_at"],
            })
            s.mandates.append({
                "order_id": ch["order_id"], "user_id": user_id,
                "mandate_type": "clawpay.gadget_cart.v1",
                "provider_mandate_reference": payload_sha256, "status": "verified",
                "expires_at": order["quote_expires_at"],
            })
            s._transition(order, "APPROVED", "device", device_id, "gadget_approved")
            s._transition(order, "PAYMENT_PENDING", "device", device_id, "payment_initiated")
            attempt_id = str(uuid.uuid4())
            idem_key = f"clawpay-{ch['order_id']}-1"
            lease = now + timedelta(seconds=30)
            s.payment_attempts[attempt_id] = {
                "id": attempt_id, "order_id": ch["order_id"],
                "payment_method_id": pm["id"], "provider": "paypal",
                "status": "initiated", "idempotency_key": idem_key,
                "amount_minor": order["total_minor"], "currency": ch["currency"],
                "claimed_until": lease, "first_sent_at": None, "capture_sent_at": None,
                "capture_send_count": 0, "completed_at": None, "exhausted_at": None,
                "provider_order_id": None, "provider_capture_id": None,
                "provider_status": None, "failure_code": None,
                "vault_token_reference": vault_token,
                "created_at": now, "updated_at": now,
            }
            s._audit(user_id, "device", device_id, "approval.verified", "order", ch["order_id"],
                     {"challenge_id": challenge_id})
            s._audit(user_id, "device", device_id, "payment.initiated", "payment_attempt", attempt_id,
                     {"idempotency_key": idem_key})
            return RpcResult(
                True, "approved",
                {"attempt_id": attempt_id, "idempotency_key": idem_key,
                 "vault_token_reference": vault_token},
            )


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


class _Payments:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    def _to_record(self, att: dict[str, Any]) -> PaymentAttemptRecord:
        order = self.s.orders.get(att["order_id"], {})
        user_id = order.get("user_id", "")
        pm = next((p for p in self.s.payment_methods if p["id"] == att.get("payment_method_id")), None)
        vault_ref = pm.get("vault_token_reference") if pm else att.get("vault_token_reference")
        return PaymentAttemptRecord(
            id=att["id"],
            order_id=att["order_id"],
            user_id=user_id,
            payment_method_id=att.get("payment_method_id"),
            provider=att.get("provider", "paypal"),
            status=att.get("status", "initiated"),
            idempotency_key=att["idempotency_key"],
            amount_minor=att["amount_minor"],
            currency=att["currency"],
            provider_order_id=att.get("provider_order_id"),
            provider_capture_id=att.get("provider_capture_id"),
            provider_status=att.get("provider_status"),
            failure_code=att.get("failure_code"),
            claimed_until=att.get("claimed_until"),
            first_sent_at=att.get("first_sent_at"),
            capture_sent_at=att.get("capture_sent_at"),
            capture_send_count=att.get("capture_send_count", 0),
            vault_token_reference=vault_ref,
            completed_at=att.get("completed_at"),
            exhausted_at=att.get("exhausted_at"),
            created_at=att.get("created_at", self.s.now()),
        )

    async def get_attempt(self, attempt_id: str) -> PaymentAttemptRecord | None:
        await self.s._enter("payment_attempts.get")
        att = self.s.payment_attempts.get(attempt_id)
        if att is None:
            return None
        return self._to_record(att)

    async def get_attempt_for_order(self, order_id: str) -> PaymentAttemptRecord | None:
        await self.s._enter("payment_attempts.get_for_order")
        for att in self.s.payment_attempts.values():
            if att["order_id"] == order_id:
                return self._to_record(att)
        return None

    async def mark_attempt_sent(self, attempt_id: str) -> datetime | None:
        s = self.s
        async with s.lock:
            await s._enter("mark_attempt_sent")
            att = s.payment_attempts.get(attempt_id)
            if att is None:
                raise RepositoryError("no_data_found")
            prev = att.get("first_sent_at")
            if prev is None:
                att["first_sent_at"] = s.now()
            return prev

    async def mark_capture_sent(self, attempt_id: str) -> tuple[datetime | None, int]:
        s = self.s
        async with s.lock:
            await s._enter("mark_capture_sent")
            att = s.payment_attempts.get(attempt_id)
            if att is None:
                raise RepositoryError("no_data_found")
            prev = att.get("capture_sent_at")
            att["capture_send_count"] = att.get("capture_send_count", 0) + 1
            if prev is None:
                att["capture_sent_at"] = s.now()
            return prev, att["capture_send_count"]

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
    ) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("apply_payment_result")
            att = s.payment_attempts.get(attempt_id)
            if att is None:
                return RpcResult(False, "attempt_not_found", {})
            order = s.orders.get(att["order_id"])
            if order is None:
                return RpcResult(False, "order_not_found", {})
            user_id = order["user_id"]
            now = s.now()

            if provider_order_id:
                if att.get("provider_order_id") and att["provider_order_id"] != provider_order_id:
                    s._audit(user_id, "system", None, "payment.conflicting_result", "payment_attempt", attempt_id, {"kind": "provider_order_id_mismatch", "source": source})
                else:
                    att["provider_order_id"] = provider_order_id
            if provider_capture_id:
                if att.get("provider_capture_id") and att["provider_capture_id"] != provider_capture_id:
                    s._audit(user_id, "system", None, "payment.conflicting_result", "payment_attempt", attempt_id, {"kind": "provider_capture_id_mismatch", "source": source})
                else:
                    att["provider_capture_id"] = provider_capture_id
            if provider_status:
                att["provider_status"] = provider_status
            if failure_code:
                att["failure_code"] = failure_code

            att_status = att["status"]
            if att_status in ("initiated", "pending", "unknown"):
                if classification == "completed":
                    att["status"] = "completed"
                    att["completed_at"] = now
                    if order["status"] != "PAID":
                        s._transition(order, "PAID", "system", None, "payment_completed")
                    for sp in s.spending_records:
                        if sp["order_id"] == order["id"] and sp["status"] == "reserved":
                            sp["status"] = "committed"
                    s._audit(user_id, "system", None, "spending.committed", "order", order["id"], {"source": source})
                    s._audit(user_id, "system", None, "payment.completed", "payment_attempt", attempt_id, {"source": source})
                    return RpcResult(True, "applied", {"new_status": "completed"})

                elif classification == "pending":
                    att["status"] = "pending"
                    if order["status"] != "PAYMENT_PENDING":
                        s._transition(order, "PAYMENT_PENDING", "system", None, "payment_pending")
                    return RpcResult(True, "applied", {"new_status": "pending"})

                elif classification == "failed":
                    att["status"] = "failed"
                    if order["status"] != "PAYMENT_FAILED":
                        s._transition(order, "PAYMENT_FAILED", "system", None, "payment_failed")
                    for sp in s.spending_records:
                        if sp["order_id"] == order["id"] and sp["status"] == "reserved":
                            sp["status"] = "released"
                    s._audit(user_id, "system", None, "spending.released", "order", order["id"], {"source": source})
                    s._audit(user_id, "system", None, "payment.failed", "payment_attempt", attempt_id, {"source": source})
                    return RpcResult(True, "applied", {"new_status": "failed"})

                elif classification == "uncertain":
                    att["status"] = "unknown"
                    if order["status"] != "REQUIRES_RECONCILIATION":
                        s._transition(order, "REQUIRES_RECONCILIATION", "system", None, "payment_uncertain")
                    s._audit(user_id, "system", None, "payment.uncertain", "payment_attempt", attempt_id, {"source": source})
                    return RpcResult(True, "applied", {"new_status": "unknown"})

            elif att_status == "completed":
                if classification == "failed":
                    s._audit(user_id, "system", None, "payment.conflicting_result", "payment_attempt", attempt_id, {"kind": "failed_after_completed", "source": source})
                return RpcResult(True, "already_completed", {"new_status": "completed"})

            elif att_status == "failed":
                if classification == "completed":
                    att["status"] = "completed"
                    att["completed_at"] = now
                    s._transition(order, "PAID", "system", None, "late_completion")
                    orig_day = next((sp["spending_day"] for sp in s.spending_records if sp["order_id"] == order["id"]), now.date())
                    s.spending_records.append({
                        "user_id": user_id,
                        "order_id": order["id"],
                        "amount_minor": order["total_minor"],
                        "currency": order["currency"],
                        "spending_day": orig_day,
                        "status": "committed",
                    })
                    s._audit(user_id, "system", None, "spending.committed", "order", order["id"], {"late": True, "source": source})
                    s._audit(user_id, "system", None, "payment.late_completion", "payment_attempt", attempt_id, {"source": source})
                    return RpcResult(True, "late_completed", {"new_status": "completed"})
                return RpcResult(True, "already_failed", {"new_status": "failed"})

            return RpcResult(True, "no_change", {"new_status": att_status})

    async def mark_attempt_exhausted(self, attempt_id: str) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("mark_attempt_exhausted")
            att = s.payment_attempts.get(attempt_id)
            if att is None:
                return RpcResult(False, "attempt_not_found", {})
            order = s.orders.get(att["order_id"])
            order_status = order["status"] if order else None
            user_id = order["user_id"] if order else None
            att["exhausted_at"] = s.now()
            att["status"] = "unknown"
            if order and order_status not in ("REQUIRES_RECONCILIATION", "PAID", "PAYMENT_FAILED", "EXPIRED", "CANCELLED", "DENIED"):
                s._transition(order, "REQUIRES_RECONCILIATION", "system", None, "payment_exhausted")
            s._audit(user_id, "system", None, "payment.reconcile_exhausted", "payment_attempt", attempt_id, {})
            return RpcResult(True, str(order_status), {"order_status": order_status})

    async def claim_reconcilable_attempts(
        self, limit: int = 20, lease_seconds: int = 60
    ) -> list[PaymentAttemptRecord]:
        s = self.s
        async with s.lock:
            await s._enter("claim_reconcilable_attempts")
            now = s.now()
            lease_until = now + timedelta(seconds=lease_seconds)
            candidates = []
            for att in s.payment_attempts.values():
                if att["status"] not in ("initiated", "pending", "unknown"):
                    continue
                if att.get("exhausted_at") is not None:
                    continue
                claimed_until = att.get("claimed_until")
                if claimed_until is not None and claimed_until > now:
                    continue
                created_at = att.get("created_at", now)
                if att["status"] == "initiated" and (now - created_at).total_seconds() < 60:
                    continue
                candidates.append(att)

            candidates.sort(key=lambda a: a.get("created_at", now))
            claimed = candidates[:limit]
            for att in claimed:
                att["claimed_until"] = lease_until
            return [self._to_record(a) for a in claimed]

    async def claim_reconcilable_attempt(
        self, order_id: str, lease_seconds: int = 60
    ) -> PaymentAttemptRecord | None:
        s = self.s
        async with s.lock:
            await s._enter("claim_reconcilable_attempt")
            now = s.now()
            for att in s.payment_attempts.values():
                if att["order_id"] != order_id:
                    continue
                if att["status"] not in ("initiated", "pending", "unknown"):
                    continue
                if att.get("exhausted_at") is not None:
                    continue
                claimed_until = att.get("claimed_until")
                if claimed_until is not None and claimed_until > now:
                    continue
                created_at = att.get("created_at", now)
                if att["status"] == "initiated" and (now - created_at).total_seconds() < 60:
                    continue
                att["claimed_until"] = now + timedelta(seconds=lease_seconds)
                return self._to_record(att)
            return None
