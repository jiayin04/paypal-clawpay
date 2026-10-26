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
    ApprovalChallengeRecord,
    DeviceRecord,
    MerchantRecord,
    OrderItemRecord,
    OrderRecord,
    PaymentAttemptRecord,
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

    async def list_pending_for_device(self, device_id: str) -> list[OrderRecord]:
        response = await self._run(
            lambda c: c.table("orders")
            .select(_ORDER_COLUMNS)
            .eq("device_id", device_id)
            .eq("status", "awaiting_device_approval")
            .order("created_at")
            .execute()
        )
        return [_order(r) for r in response.data or []]


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

    async def list_active(self, user_id: str) -> list[dict]:
        cols = "id,user_id,status,payer_email_masked,created_at,updated_at"
        response = await self._run(
            lambda c: c.table("payment_methods")
            .select(cols)
            .eq("user_id", user_id)
            .eq("status", "active")
            .execute()
        )
        return response.data or []

    async def get_by_id(self, payment_method_id: str, user_id: str) -> dict | None:
        response = await self._run(
            lambda c: c.table("payment_methods")
            .select("*")
            .eq("id", payment_method_id)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return response.data

    async def create_pending(
        self,
        *,
        user_id: str,
        state_token: str,
        state_expires_at: object,
    ) -> str:
        row = {
            "user_id": user_id,
            "status": "pending",
            "state_token": state_token,
            "state_expires_at": str(state_expires_at),
        }
        response = await self._run(
            lambda c: c.table("payment_methods").insert(row).execute()
        )
        return response.data[0]["id"]

    async def activate_payment_method(
        self,
        *,
        payment_method_id: str,
        user_id: str,
        vault_token_reference: str,
        payer_email_masked: str | None,
    ) -> RpcResult:
        row = await self._rpc_one(
            "activate_payment_method",
            {
                "p_payment_method_id": payment_method_id,
                "p_user_id": user_id,
                "p_vault_token_reference": vault_token_reference,
                "p_payer_email_masked": payer_email_masked,
            },
        )
        return RpcResult(ok=bool(row["ok"]), reason=str(row["reason"]), data=dict(row))

    async def revoke_payment_method(
        self,
        *,
        payment_method_id: str,
        user_id: str,
    ) -> RpcResult:
        row = await self._rpc_one(
            "revoke_payment_method",
            {
                "p_payment_method_id": payment_method_id,
                "p_user_id": user_id,
            },
        )
        return RpcResult(ok=bool(row["ok"]), reason=str(row["reason"]), data=dict(row))

    async def find_by_state(self, state_token: str) -> dict | None:
        rows = await self._rpc("find_payment_method_by_state", {"p_state_token": state_token})
        return rows[0] if rows else None



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


class SupabaseApprovalRepository(_SupabaseBase):
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
        params = {
            "p_order_id": order_id,
            "p_device_id": device_id,
            "p_user_id": user_id,
            "p_key_version": key_version,
            "p_nonce": nonce,
            "p_canonical": canonical,
            "p_payload_sha256": payload_sha256,
            "p_amount_minor": amount_minor,
            "p_currency": currency,
            "p_expires_at": expires_at.isoformat(),
        }
        try:
            row = await self._rpc_one("issue_approval_challenge", params)
        except RepositoryError as exc:
            if exc.code == "23505":
                # Race on open challenge: retry once
                row = await self._rpc_one("issue_approval_challenge", params)
            else:
                raise
        return RpcResult(
            ok=bool(row["ok"]),
            reason=str(row["reason"]),
            data=row,
        )

    async def get_challenge(self, challenge_id: str) -> ApprovalChallengeRecord | None:
        response = await self._run(
            lambda c: c.table("approval_challenges")
            .select(
                "id,order_id,device_id,user_id,nonce,canonical_payload,payload_sha256,"
                "amount_minor,currency,key_version,expires_at,consumed_at,invalidated_at,created_at"
            )
            .eq("id", challenge_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        if not rows:
            return None
        r = rows[0]
        return ApprovalChallengeRecord(
            id=r["id"],
            order_id=r["order_id"],
            device_id=r["device_id"],
            user_id=r["user_id"],
            nonce=r["nonce"],
            canonical_payload=r["canonical_payload"],
            payload_sha256=r["payload_sha256"],
            amount_minor=int(r["amount_minor"]),
            currency=r["currency"],
            key_version=int(r["key_version"]),
            expires_at=parse_ts(r["expires_at"]),  # type: ignore[arg-type]
            consumed_at=parse_ts(r.get("consumed_at")),
            invalidated_at=parse_ts(r.get("invalidated_at")),
            created_at=parse_ts(r.get("created_at")),
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
        row = await self._rpc_one(
            "record_gadget_denial",
            {
                "p_challenge_id": challenge_id,
                "p_device_id": device_id,
                "p_user_id": user_id,
                "p_nonce": nonce,
                "p_payload_sha256": payload_sha256,
                "p_key_version": key_version,
                "p_actor_type": actor_type,
                "p_actor_id": actor_id,
            },
        )
        return RpcResult(ok=bool(row["ok"]), reason=str(row["reason"]), data=row)

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
        row = await self._rpc_one(
            "accept_gadget_approval",
            {
                "p_challenge_id": challenge_id,
                "p_device_id": device_id,
                "p_user_id": user_id,
                "p_nonce": nonce,
                "p_payload_sha256": payload_sha256,
                "p_policy_currency": policy_currency,
                "p_daily_limit_minor": daily_limit_minor,
                "p_timezone": timezone,
                "p_phone_threshold_minor": phone_threshold_minor,
            },
        )
        return RpcResult(ok=bool(row["ok"]), reason=str(row["reason"]), data=row)


def _parse_attempt_record(row: Mapping[str, Any]) -> PaymentAttemptRecord:
    def _parse_dt(val: Any) -> datetime | None:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        return datetime.fromisoformat(str(val))

    return PaymentAttemptRecord(
        id=str(row.get("attempt_id") or row.get("id")),
        order_id=str(row["order_id"]),
        user_id=str(row.get("user_id", "")),
        payment_method_id=str(row["payment_method_id"]) if row.get("payment_method_id") else None,
        provider=str(row.get("provider", "paypal")),
        status=str(row.get("status", "initiated")),
        idempotency_key=str(row["idempotency_key"]),
        amount_minor=int(row["amount_minor"]),
        currency=str(row["currency"]).strip(),
        provider_order_id=str(row["provider_order_id"]) if row.get("provider_order_id") else None,
        provider_capture_id=str(row["provider_capture_id"]) if row.get("provider_capture_id") else None,
        provider_status=str(row["provider_status"]) if row.get("provider_status") else None,
        failure_code=str(row["failure_code"]) if row.get("failure_code") else None,
        claimed_until=_parse_dt(row.get("claimed_until")),
        first_sent_at=_parse_dt(row.get("first_sent_at")),
        capture_sent_at=_parse_dt(row.get("capture_sent_at")),
        capture_send_count=int(row.get("capture_send_count", 0)),
        vault_token_reference=str(row["vault_token_reference"]) if row.get("vault_token_reference") else None,
        completed_at=_parse_dt(row.get("completed_at")),
        exhausted_at=_parse_dt(row.get("exhausted_at")),
        created_at=_parse_dt(row.get("created_at")) or datetime.now(),
    )


class SupabasePaymentRepository(_SupabaseBase):
    """Payment repository wrapping SQL RPCs for payment execution and reconciliation."""

    async def get_attempt(self, attempt_id: str) -> PaymentAttemptRecord | None:
        response = await self._run(
            lambda c: c.table("payment_attempts")
            .select("*")
            .eq("id", attempt_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        if not rows:
            return None
        return _parse_attempt_record(rows[0])

    async def get_attempt_for_order(self, order_id: str) -> PaymentAttemptRecord | None:
        response = await self._run(
            lambda c: c.table("payment_attempts")
            .select("*")
            .eq("order_id", order_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        if not rows:
            return None
        return _parse_attempt_record(rows[0])

    async def mark_attempt_sent(self, attempt_id: str) -> datetime | None:
        row = await self._rpc_one("mark_attempt_sent", {"p_attempt_id": attempt_id})
        prev = row.get("previous")
        if prev is None:
            return None
        if isinstance(prev, datetime):
            return prev
        return datetime.fromisoformat(str(prev))

    async def mark_capture_sent(self, attempt_id: str) -> tuple[datetime | None, int]:
        row = await self._rpc_one("mark_capture_sent", {"p_attempt_id": attempt_id})
        prev = row.get("previous")
        prev_dt = datetime.fromisoformat(str(prev)) if prev and not isinstance(prev, datetime) else prev
        count = int(row.get("send_count", 1))
        return prev_dt, count

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
        row = await self._rpc_one(
            "apply_payment_result",
            {
                "p_attempt_id": attempt_id,
                "p_classification": classification,
                "p_provider_order_id": provider_order_id,
                "p_provider_capture_id": provider_capture_id,
                "p_provider_status": provider_status,
                "p_failure_code": failure_code,
                "p_source": source,
            },
        )
        return RpcResult(ok=bool(row["ok"]), reason=str(row["reason"]), data=row)

    async def mark_attempt_exhausted(self, attempt_id: str) -> RpcResult:
        row = await self._rpc_one("mark_attempt_exhausted", {"p_attempt_id": attempt_id})
        return RpcResult(ok=bool(row["ok"]), reason=str(row.get("order_status", "")), data=row)

    async def claim_reconcilable_attempts(
        self, limit: int = 20, lease_seconds: int = 60
    ) -> list[PaymentAttemptRecord]:
        rows = await self._rpc(
            "claim_reconcilable_attempts",
            {"p_limit": limit, "p_lease_seconds": lease_seconds},
        )
        return [_parse_attempt_record(r) for r in rows]

    async def claim_reconcilable_attempt(
        self, order_id: str, lease_seconds: int = 60
    ) -> PaymentAttemptRecord | None:
        rows = await self._rpc(
            "claim_reconcilable_attempt",
            {"p_order_id": order_id, "p_lease_seconds": lease_seconds},
        )
        if not rows:
            return None
        return _parse_attempt_record(rows[0])

