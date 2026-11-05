"""Payment execution and reconciliation using atomic repository transitions.

``send_guard`` (optional) is asked before an attempt is sent to PayPal for the
first time, by the inline path and the reconciler alike. It returns a failure
code to fail the never-sent attempt instead (no money moves), or None to send.
The merchant fulfillment check uses it so no order is charged unless its
merchant agent has reserved the cart.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace

import anyio

from app.core.clock import Clock
from app.core.config import Settings
from app.core.metrics import METRICS
from app.integrations.paypal import PaymentClassification, classify
from app.integrations.paypal.gateway import PayPalGateway
from app.repositories.errors import with_rpc_retry
from app.repositories.ports.orders import OrderRepository
from app.repositories.ports.payments import PaymentAttemptRecord, PaymentRepository
from app.services.audit_service import AuditService


logger = logging.getLogger(__name__)

SendGuard = Callable[[PaymentAttemptRecord], Awaitable[str | None]]


class PaymentService:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        payment_repo: PaymentRepository,
        order_repo: OrderRepository,
        paypal: PayPalGateway,
        audit: AuditService | None = None,
        send_guard: SendGuard | None = None,
    ) -> None:
        self._settings, self._clock = settings, clock
        self._payment_repo, self._order_repo, self._paypal = with_rpc_retry(payment_repo), order_repo, paypal
        self._reconcile_throttles: dict[str, float] = {}
        self._audit = audit
        self._send_guard = send_guard

    async def fail_unsent(self, attempt: PaymentAttemptRecord, failure_code: str) -> PaymentClassification:
        """Fail an attempt that was never sent to PayPal; releases the spending reservation."""
        if attempt.status != "initiated" or attempt.first_sent_at is not None:
            return PaymentClassification("no_result")
        result = PaymentClassification("failed", failure_code=failure_code)
        await self._apply(attempt, result, "first_create")
        return result

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
        METRICS.payment_results.inc(source, result.status)
        logger.info(
            "Payment result %s via %s (provider_status=%s failure_code=%s)",
            result.status, source, result.provider_status, result.failure_code,
            extra={"order_id": attempt.order_id},
        )
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
        if self._send_guard is not None and attempt.first_sent_at is None:
            try:
                blocked = await self._send_guard(attempt)
            except Exception as exc:
                # Unknown: do not send now; the reconciler asks again later.
                logger.warning("payment send guard failed: %s", type(exc).__name__)
                return PaymentClassification("no_result")
            if blocked:
                return await self.fail_unsent(attempt, blocked)
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
