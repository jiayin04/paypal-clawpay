"""Service-role Supabase implementations of the repository protocols.

SECURITY: these use the service-role client (bypasses RLS). Every query filters
by the ids the caller was authenticated for; RPCs re-check invariants under lock.
The sync supabase client runs in a worker thread (anyio.to_thread).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any, TypeVar

import anyio
from postgrest.exceptions import APIError
from postgrest.types import ReturnMethod
from supabase import Client

from app.core.exceptions import AppError
from app.repositories.errors import RepositoryError
from app.repositories.protocols import (
    DeviceRecord,
    MerchantRecord,
    OrderItemRecord,
    OrderRecord,
    RpcResult,
    SpendingSummaryRow,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

_CONSTRAINT_RE = re.compile(r'constraint "([A-Za-z0-9_]+)"')

_DEVICE_COLUMNS = "id,user_id,status,key_version,public_key,last_seen_at"
_ORDER_COLUMNS = (
    "id,user_id,device_id,merchant_id,currency,subtotal_minor,total_minor,status,"
    "quote_expires_at,created_at,updated_at,cart_sha256"
)


def parse_ts(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _constraint(exc: APIError) -> str | None:
    for text in (exc.message, exc.details):
        if text:
            match = _CONSTRAINT_RE.search(str(text))
            if match:
                return match.group(1)
    return None


class _SupabaseBase:
    def __init__(self, client_provider: Callable[[], Client]) -> None:
        self._client_provider = client_provider

    async def _run(self, fn: Callable[[Client], T]) -> T:
        def work() -> T:
            return fn(self._client_provider())

        try:
            return await anyio.to_thread.run_sync(work)
        except APIError as exc:
            raise RepositoryError(exc.code, _constraint(exc)) from None
        except (AppError, RepositoryError):
            raise
        except Exception as exc:
            # Type only: driver errors can contain URLs or keys.
            logger.error("Database call failed: %s", type(exc).__name__)
            raise RepositoryError(None) from None

    async def _rpc(self, name: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        response = await self._run(lambda c: c.rpc(name, dict(params)).execute())
        data = response.data
        if data is None:
            return []
        return data if isinstance(data, list) else [data]

    async def _rpc_one(self, name: str, params: Mapping[str, Any]) -> dict[str, Any]:
        rows = await self._rpc(name, params)
        if not rows:
            raise RepositoryError(None)
        return rows[0]


def _device(row: Mapping[str, Any]) -> DeviceRecord:
    return DeviceRecord(
        id=row["id"],
        user_id=row["user_id"],
        status=row["status"],
        key_version=int(row["key_version"]),
        public_key=row["public_key"],
        last_seen_at=parse_ts(row.get("last_seen_at")),
    )


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


class SupabaseDeviceRepository(_SupabaseBase):
    async def _find(self, column: str, token_hash: str) -> DeviceRecord | None:
        response = await self._run(
            lambda c: c.table("devices").select(_DEVICE_COLUMNS).eq(column, token_hash).limit(1).execute()
        )
        rows = response.data or []
        return _device(rows[0]) if rows else None

    async def find_by_token_hash(self, token_hash: str) -> DeviceRecord | None:
        return await self._find("token_hash", token_hash)

    async def find_by_llm_token_hash(self, token_hash: str) -> DeviceRecord | None:
        return await self._find("llm_token_hash", token_hash)

    async def touch_last_seen(self, device_id: str, at: datetime) -> None:
        await self._run(
            lambda c: c.table("devices").update({"last_seen_at": at.isoformat()}).eq("id", device_id).execute()
        )

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
        row = await self._rpc_one(
            "register_device",
            {
                "p_user_id": user_id,
                "p_device_name": device_name,
                "p_public_key": public_key,
                "p_key_version": key_version,
                "p_token_hash": token_hash,
                "p_llm_token_hash": llm_token_hash,
                "p_actor_type": actor_type,
                "p_actor_id": actor_id,
            },
        )
        return RpcResult(bool(row["ok"]), row["reason"], {"device_id": row.get("device_id")})

    async def revoke(
        self, *, device_id: str, user_id: str | None, actor_type: str, actor_id: str | None
    ) -> RpcResult:
        row = await self._rpc_one(
            "revoke_device",
            {
                "p_device_id": device_id,
                "p_user_id": user_id,
                "p_actor_type": actor_type,
                "p_actor_id": actor_id,
            },
        )
        return RpcResult(
            bool(row["ok"]),
            row["reason"],
            {"device_id": row.get("device_id"), "cancelled_orders": row.get("cancelled_orders") or 0},
        )


class SupabaseMerchantRepository(_SupabaseBase):
    async def _one(self, column: str, value: str, active_only: bool) -> MerchantRecord | None:
        def query(c: Client) -> Any:
            q = c.table("merchants").select("id,merchant_code,display_name,status").eq(column, value)
            if active_only:
                q = q.eq("status", "active")
            return q.limit(1).execute()

        rows = (await self._run(query)).data or []
        if not rows:
            return None
        r = rows[0]
        return MerchantRecord(r["id"], r["merchant_code"], r["display_name"], r["status"])

    async def get_active_by_code(self, merchant_code: str) -> MerchantRecord | None:
        return await self._one("merchant_code", merchant_code, True)

    async def get_by_id(self, merchant_id: str) -> MerchantRecord | None:
        return await self._one("id", merchant_id, False)


class SupabaseOrderRepository(_SupabaseBase):
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


class SupabaseSpendingRepository(_SupabaseBase):
    async def summary(self, user_id: str, timezone: str) -> SpendingSummaryRow:
        row = await self._rpc_one("spending_summary", {"p_user_id": user_id, "p_timezone": timezone})
        day = row["day"]
        return SpendingSummaryRow(
            day=day if isinstance(day, date) else date.fromisoformat(str(day)),
            committed_minor=int(row["committed_minor"]),
            reserved_minor=int(row["reserved_minor"]),
        )


class SupabasePaymentMethodRepository(_SupabaseBase):
    async def has_active(self, user_id: str) -> bool:
        response = await self._run(
            lambda c: c.table("payment_methods")
            .select("id")
            .eq("user_id", user_id)
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        return bool(response.data)


class SupabaseConversationRepository(_SupabaseBase):
    async def upsert_conversation(
        self, *, user_id: str, device_id: str, external_session_id: str
    ) -> str:
        response = await self._run(
            lambda c: c.table("conversations")
            .upsert(
                {
                    "user_id": user_id,
                    "device_id": device_id,
                    "source": "esp_claw",
                    "external_session_id": external_session_id,
                },
                on_conflict="source,external_session_id",
            )
            .execute()
        )
        rows = response.data or []
        if not rows:
            raise RepositoryError(None)
        return rows[0]["id"]

    async def add_event(
        self,
        *,
        conversation_id: str,
        event_type: str,
        summary: Mapping[str, Any],
        order_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        await self._run(
            lambda c: c.table("conversation_events")
            .insert(
                {
                    "conversation_id": conversation_id,
                    "order_id": order_id,
                    "correlation_id": correlation_id,
                    "event_type": event_type,
                    "summary": dict(summary),
                    "payload": {},  # server-only column; always empty in this scope
                },
                returning=ReturnMethod.minimal,
            )
            .execute()
        )


class SupabaseInsertRepository(_SupabaseBase):
    """Append-only insert into one table (a2a_messages, audit_logs)."""

    def __init__(self, client_provider: Callable[[], Client], table: str) -> None:
        super().__init__(client_provider)
        self._table = table

    async def insert(self, row: Mapping[str, Any]) -> None:
        await self._run(
            lambda c: c.table(self._table).insert(dict(row), returning=ReturnMethod.minimal).execute()
        )
