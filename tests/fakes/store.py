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
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from app.domain.order_state import assert_transition
from app.repositories.bundle import Repositories
from app.repositories.errors import RepositoryError
from tests.fakes.clock import FakeClock
from tests.fakes.repositories.approvals import FakeApprovalRepository
from tests.fakes.repositories.dashboard import FakeUserReader
from tests.fakes.repositories.devices import FakeDeviceRepository
from tests.fakes.repositories.events import FakeConversationRepository, FakeInsertRepository
from tests.fakes.repositories.merchants import FakeMerchantRepository
from tests.fakes.repositories.orders import FakeOrderRepository
from tests.fakes.repositories.payment_methods import FakePaymentMethodRepository
from tests.fakes.repositories.payments import FakePaymentRepository
from tests.fakes.repositories.spending import FakeSpendingRepository
from tests.fakes.repositories.webhooks import FakeWebhookRepository


GROCER_ID = "aaaaaaaa-0000-0000-0000-0000000000a1"
ELECTRONICS_ID = "aaaaaaaa-0000-0000-0000-0000000000e1"
CHANNEL3_ID = "aaaaaaaa-0000-0000-0000-0000000000c3"
SHOPEE_ID = "aaaaaaaa-0000-0000-0000-0000000005e1"
FOODPANDA_ID = "aaaaaaaa-0000-0000-0000-0000000000f1"
GRAB_ID = "aaaaaaaa-0000-0000-0000-0000000006b1"


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
            # Seeded by migration 20261030090000_channel3_merchant.sql.
            CHANNEL3_ID: {
                "id": CHANNEL3_ID,
                "merchant_code": "channel3",
                "display_name": "Channel3 Marketplace",
                "status": "active",
            },
            # Seeded by migration 20261103090000_merchant_fulfillment.sql.
            **{
                m_id: {"id": m_id, "merchant_code": code, "display_name": name, "status": "active"}
                for m_id, code, name in (
                    (SHOPEE_ID, "shopee", "Shopee Demo"),
                    (FOODPANDA_ID, "foodpanda", "Foodpanda Demo"),
                    (GRAB_ID, "grab", "Grab Demo"),
                )
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
        self.webhook_events = {}
        self.calls: list[str] = []
        self._failures: dict[str, list[tuple[str, str | None]]] = {}
        self._before: dict[str, list[Callable[[InMemoryStore], Any]]] = {}


    # -- hooks -------------------------------------------------------------------------

    def fail_next(
        self,
        method: str,
        code: str,
        constraint: str | None = None,
        times: int = 1,
    ) -> None:
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
    def payments(self) -> FakePaymentRepository:
        return FakePaymentRepository(self)

    def repositories(self) -> Repositories:
        return Repositories(
            devices=FakeDeviceRepository(self),
            merchants=FakeMerchantRepository(self),
            orders=FakeOrderRepository(self),
            spending=FakeSpendingRepository(self),
            payment_methods=FakePaymentMethodRepository(self),
            conversations=FakeConversationRepository(self),
            a2a_messages=FakeInsertRepository(self, self.a2a_messages, "a2a_messages.insert"),
            audit=FakeInsertRepository(self, self.audit_logs, "audit_logs.insert"),
            approvals=FakeApprovalRepository(self),
            payments=FakePaymentRepository(self),
            webhooks=FakeWebhookRepository(self),
        )

    def user_reader_factory(self) -> Callable[[str], Any]:
        """Simulates RLS: the JWT is mapped to ``sub`` by ``jwt_subjects``."""
        store = self

        @asynccontextmanager
        async def factory(token: str):  # type: ignore[no-untyped-def]
            import jwt as pyjwt

            # The route already verified the JWT; take sub like PostgREST's auth.uid().
            sub = pyjwt.decode(token, options={"verify_signature": False})["sub"]
            yield FakeUserReader(store, sub)

        return factory
