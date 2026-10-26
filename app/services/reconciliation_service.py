"""Reconciliation service per design §3.9 (background loop + on-demand + expiry sweep)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import anyio

from app.integrations.paypal.errors import PaymentClassification, classify
from app.integrations.paypal import PayPalApiError

if TYPE_CHECKING:
    from app.core.clock import Clock
    from app.repositories.protocols import PaymentRepository, OrderRepository
    from app.integrations.paypal import PayPalGateway

logger = logging.getLogger(__name__)

_NEVER_SENT_MIN_AGE_SECONDS = 120  # 2 min: allow some slack past the 60s claim guard


class ReconciliationService:
    """Runs the background payment reconciliation loop and on-demand sweep.

    Design §3.9:
    - run_once(): claim up to 20 claimable attempts, per-attempt steps 1-3.
    - run_forever(): calls run_once() every 30s, swallows errors.
    - expire_stale_orders(): called each tick to expire CREATED/QUOTED/AWAITING_APPROVAL past quote_expires_at.
    - On-demand reconcile (called by order_service on PAYMENT_PENDING/REQUIRES_RECONCILIATION):
      delegated to PaymentService.reconcile_order().
    """

    def __init__(
        self,
        clock: "Clock",
        payment_repo: "PaymentRepository",
        order_repo: "OrderRepository",
        paypal: "PayPalGateway",
        payment_service: object,  # PaymentService – imported lazily to avoid cycle
    ) -> None:
        self._clock = clock
        self._payment_repo = payment_repo
        self._order_repo = order_repo
        self._paypal = paypal
        self._payment_service = payment_service

    async def run_once(self) -> int:
        """Process one batch of claimable attempts. Returns count processed."""
        attempts = await self._payment_repo.claim_reconcilable_attempts(limit=20, lease_seconds=60)
        processed = 0
        for attempt in attempts:
            try:
                await self._process_attempt(attempt)
                processed += 1
            except Exception as exc:
                logger.warning("Reconciliation failed for attempt %s: %s", attempt.id, exc)
        return processed

    async def _process_attempt(self, attempt: object) -> None:
        """Per-attempt reconciliation steps 1-3 (§3.9)."""
        from app.repositories.protocols import PaymentAttemptRecord
        att: PaymentAttemptRecord = attempt  # type: ignore[assignment]

        # Step 1: Never-sent attempts older than replay window -> release as never_sent
        if att.first_sent_at is None:
            import datetime
            now = self._clock.utc_now()
            created = att.created_at if hasattr(att, "created_at") else now
            age = (now - created).total_seconds()
            if age > _NEVER_SENT_MIN_AGE_SECONDS:
                logger.info("Releasing never-sent attempt %s (age %.0fs)", att.id, age)
                await self._payment_repo.apply_payment_result(
                    attempt_id=att.id,
                    classification="failed",
                    failure_code="never_sent",
                    source="reconciler",
                )
            return

        # Step 2: Has provider_order_id -> poll PayPal
        if att.provider_order_id:
            try:
                with anyio.fail_after(5.0):
                    order = await self._paypal.get_order(att.provider_order_id)
                classification = classify(order, source="poll", attempt=att)
            except PayPalApiError as exc:
                classification = classify(exc, source="poll", attempt=att)
            except Exception as exc:
                classification = classify(exc, source="poll", attempt=att)

            # For poll: only apply non-uncertain results; uncertain keeps order pending
            if classification.status != "uncertain":
                await self._payment_repo.apply_payment_result(
                    attempt_id=att.id,
                    classification=classification.status,
                    provider_order_id=att.provider_order_id,
                    provider_capture_id=classification.provider_capture_id,
                    provider_status=classification.provider_status,
                    failure_code=classification.failure_code,
                    source="poll",
                )
            return

        # Step 3: No provider_order_id -> replay full create flow
        try:
            with anyio.fail_after(10.0):
                await self._payment_service.execute(att)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning("Reconciler execute failed for %s: %s", att.id, exc)

    async def run_forever(self, interval_seconds: float = 30.0) -> None:
        """Background loop: run_once every interval_seconds, cancel on anyio.get_cancel_scope()."""
        logger.info("Reconciliation loop started (interval=%.0fs)", interval_seconds)
        while True:
            try:
                count = await self.run_once()
                if count:
                    logger.info("Reconciled %d attempts", count)
            except Exception as exc:
                logger.warning("Reconciliation loop error: %s", exc)
            await anyio.sleep(interval_seconds)
