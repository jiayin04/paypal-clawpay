"""Service-role Supabase adapter for payments."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.repositories.ports.common import RpcResult
from app.repositories.ports.payments import PaymentAttemptRecord
from app.repositories.supabase.base import _SupabaseBase


def parse_attempt_record(row: Mapping[str, Any]) -> PaymentAttemptRecord:
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

    async def _load_attempt(self, column: str, value: str) -> PaymentAttemptRecord | None:
        response = await self._run(
            lambda client: client.table("payment_attempts")
            .select("*")
            .eq(column, value)
            .limit(1)
            .execute()
        )
        if not response.data:
            return None

        row = dict(response.data[0])
        owner = await self._run(
            lambda client: client.table("orders")
            .select("user_id")
            .eq("id", row["order_id"])
            .limit(1)
            .execute()
        )
        row["user_id"] = owner.data[0]["user_id"] if owner.data else ""
        if row.get("payment_method_id"):
            payment_method = await self._run(
                lambda client: client.table("payment_methods")
                .select("vault_token_reference")
                .eq("id", row["payment_method_id"])
                .limit(1)
                .execute()
            )
            row["vault_token_reference"] = (
                payment_method.data[0]["vault_token_reference"] if payment_method.data else None
            )
        return parse_attempt_record(row)

    async def get_attempt(self, attempt_id: str) -> PaymentAttemptRecord | None:
        return await self._load_attempt("id", attempt_id)

    async def get_attempt_for_order(self, order_id: str) -> PaymentAttemptRecord | None:
        return await self._load_attempt("order_id", order_id)

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
        return [parse_attempt_record(r) for r in rows]

    async def claim_reconcilable_attempt(
        self, order_id: str, lease_seconds: int = 60
    ) -> PaymentAttemptRecord | None:
        rows = await self._rpc(
            "claim_reconcilable_attempt",
            {"p_order_id": order_id, "p_lease_seconds": lease_seconds},
        )
        if not rows:
            return None
        row = dict(rows[0])
        owner = await self._run(lambda c: c.table("orders").select("user_id").eq("id",row["order_id"]).limit(1).execute())
        row["user_id"] = owner.data[0]["user_id"] if owner.data else ""
        if row.get("payment_method_id"):
            pm = await self._run(lambda c: c.table("payment_methods").select("vault_token_reference").eq("id", row["payment_method_id"]).limit(1).execute())
            row["vault_token_reference"] = pm.data[0]["vault_token_reference"] if pm.data else None
        return parse_attempt_record(row)
