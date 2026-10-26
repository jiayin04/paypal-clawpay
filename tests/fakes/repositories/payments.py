"""In-memory payments adapter sharing the store transaction lock."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING

from app.repositories.errors import RepositoryError
from app.repositories.ports.common import RpcResult
from app.repositories.ports.payments import PaymentAttemptRecord


if TYPE_CHECKING:
    from tests.fakes.store import InMemoryStore


class FakePaymentRepository:
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
                    return RpcResult(False,"conflicting_provider_order_id",{})
                else:
                    att["provider_order_id"] = provider_order_id
            if provider_capture_id:
                if att.get("provider_capture_id") and att["provider_capture_id"] != provider_capture_id:
                    s._audit(user_id, "system", None, "payment.conflicting_result", "payment_attempt", attempt_id, {"kind": "provider_capture_id_mismatch", "source": source})
                    return RpcResult(False,"conflicting_provider_capture_id",{})
                else:
                    att["provider_capture_id"] = provider_capture_id
            if provider_status:
                att["provider_status"] = provider_status
            if failure_code:
                att["failure_code"] = failure_code

            att["claimed_until"] = None
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
                    if att_status == "pending":
                        return RpcResult(True, "pending_unchanged", {})
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
