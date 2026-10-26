"""Payment execution and reconciliation using atomic repository transitions."""
from __future__ import annotations

import logging
from dataclasses import replace

import anyio

from app.core.clock import Clock
from app.core.config import Settings
from app.integrations.paypal import PaymentClassification, classify
from app.integrations.paypal.gateway import PayPalGateway
from app.repositories.errors import with_rpc_retry
from app.repositories.ports.orders import OrderRepository
from app.repositories.ports.payments import PaymentAttemptRecord, PaymentRepository
from app.services.audit_service import AuditService


logger = logging.getLogger(__name__)

class PaymentService:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        payment_repo: PaymentRepository,
        order_repo: OrderRepository,
        paypal: PayPalGateway,
        audit: AuditService | None = None,
    ) -> None:
        self._settings, self._clock = settings, clock
        self._payment_repo, self._order_repo, self._paypal = with_rpc_retry(payment_repo), order_repo, paypal
        self._reconcile_throttles: dict[str, float] = {}
        self._audit = audit

    async def _token(self) -> bool:
        try:
            await self._paypal.ensure_access_token()
            return True
        except Exception as exc:
            logger.warning("paypal_auth_failed: %s", type(exc).__name__)
            return False

    async def _capture(
        self,
        attempt: PaymentAttemptRecord,
        provider_order_id: str,
    ) -> PaymentClassification:
        if not await self._token():
            await self._apply(attempt, PaymentClassification("uncertain",provider_order_id=provider_order_id), "poll")
            return PaymentClassification("no_result")
        previous, count = await self._payment_repo.mark_capture_sent(attempt.id)
        source = "first_create" if previous is None else "replay"
        try:
            outcome = await self._paypal.capture_order(
                provider_order_id=provider_order_id, request_id=f"{attempt.idempotency_key}-capture")
        except Exception as exc:
            outcome = exc
        result = replace(classify(outcome, source=source, attempt=attempt), provider_order_id=provider_order_id)
        await self._apply(attempt, result, source)
        if result.status == "uncertain" and count >= 3:
            await self._payment_repo.mark_attempt_exhausted(attempt.id)
        return result

    async def _apply(
        self,
        attempt: PaymentAttemptRecord,
        result: PaymentClassification,
        source: str,
    ) -> None:
        if result.status == "no_result":
            return
        if self._audit and result.failure_code in ("amount_mismatch", "currency_mismatch"):
            await self._audit.record("payment.conflicting_result",user_id=attempt.user_id,actor_type="system",entity_type="order",entity_id=attempt.order_id,details={"reason":result.failure_code})
        if self._audit and result.provider_status in ("REFUNDED", "PARTIALLY_REFUNDED"):
            await self._audit.record("payment.refund_recorded",user_id=attempt.user_id,actor_type="system",entity_type="order",entity_id=attempt.order_id)
        await self._payment_repo.apply_payment_result(
            attempt_id=attempt.id, classification=result.status,
            provider_order_id=result.provider_order_id, provider_capture_id=result.provider_capture_id,
            provider_status=result.provider_status, failure_code=result.failure_code, source=source)

    async def execute(self, attempt: PaymentAttemptRecord) -> PaymentClassification:
        if attempt.status != "initiated":
            return PaymentClassification("no_result")
        if not await self._token():
            return PaymentClassification("no_result")
        previous = await self._payment_repo.mark_attempt_sent(attempt.id)
        source = "first_create" if previous is None else "replay"
        try:
            outcome = await self._paypal.create_order_with_vault(
                request_id=attempt.idempotency_key, order_ref=attempt.order_id,
                invoice_id=f"clawpay-{attempt.order_id}", description="ClawPay order",
                amount_minor=attempt.amount_minor, currency=attempt.currency,
                vault_id=attempt.vault_token_reference or "")
        except Exception as exc:
            outcome = exc
        result = classify(outcome, source=source, attempt=attempt)
        if result.status == "capture_needed":
            return await self._capture(attempt, result.provider_order_id)
        await self._apply(attempt, result, source)
        return result

    async def process_claimed(self, attempt: PaymentAttemptRecord) -> PaymentClassification | None:
        if attempt.provider_order_id:
            try:
                outcome = await self._paypal.get_order(attempt.provider_order_id)
            except Exception as exc:
                outcome = exc
            result = classify(outcome, source="poll", attempt=attempt)
            if result.status == "capture_needed":
                return await self._capture(attempt, attempt.provider_order_id)
            await self._apply(attempt, result, "poll")
            return result
        age = (self._clock.utc_now() - attempt.created_at).total_seconds()
        if age > self._settings.reconcile_replay_window_seconds:
            if attempt.first_sent_at is None and attempt.status == "initiated":
                await self._apply(attempt, PaymentClassification("failed", failure_code="never_sent"), "poll")
            else:
                await self._payment_repo.mark_attempt_exhausted(attempt.id)
            return None
        return await self.execute(replace(attempt, status="initiated"))

    async def reconcile_order(self, order_id: str) -> PaymentClassification | None:
        now = self._clock.monotonic()
        if now - self._reconcile_throttles.get(order_id, float("-inf")) < 10:
            return None
        self._reconcile_throttles[order_id] = now
        if len(self._reconcile_throttles) > 10000:
            self._reconcile_throttles.pop(next(iter(self._reconcile_throttles)))
        try:
            with anyio.fail_after(5):
                attempt = await self._payment_repo.claim_reconcilable_attempt(order_id, lease_seconds=60)
                if attempt:
                    return await self.process_claimed(attempt)
        except Exception as exc:
            logger.warning("On-demand reconciliation failed: %s", type(exc).__name__)
        return None
