"""Claimed payment reconciliation and quote expiry (design section 3.9)."""
import logging

import uuid

import anyio

from app.core.clock import Clock
from app.core.metrics import METRICS
from app.core.observability import bound
from app.integrations.paypal.gateway import PayPalGateway
from app.repositories.errors import with_rpc_retry
from app.repositories.ports.orders import OrderRepository
from app.repositories.ports.payments import PaymentRepository
from app.services.payment_service import PaymentService


logger = logging.getLogger(__name__)

class ReconciliationService:
    def __init__(
        self,
        clock: Clock,
        payment_repo: PaymentRepository,
        order_repo: OrderRepository,
        paypal: PayPalGateway,
        payment_service: PaymentService,
    ) -> None:
        self._payment_repo, self._order_repo = with_rpc_retry(payment_repo), with_rpc_retry(order_repo)
        self._payment_service = payment_service

    async def run_once(self) -> int:
        attempts = await self._payment_repo.claim_reconcilable_attempts(limit=20, lease_seconds=60)
        METRICS.reconcile_claimed.inc(amount=len(attempts))
        for attempt in attempts:
            with bound(order_id=attempt.order_id):
                try:
                    with anyio.fail_after(15):
                        await self._payment_service.process_claimed(attempt)
                except Exception as exc:
                    logger.warning("Reconciliation failed: %s", type(exc).__name__)
        await self._order_repo.expire_stale_orders(limit=100)
        return len(attempts)

    async def run_forever(self, interval_seconds: float = 30) -> None:
        while True:
            # One correlation id per tick so its log lines can be grouped.
            with bound(request_id=f"reconcile-{uuid.uuid4().hex[:12]}"):
                try:
                    claimed = await self.run_once()
                    METRICS.reconcile_runs.inc("ok")
                    if claimed:
                        logger.info("Reconciler tick claimed %d attempt(s)", claimed)
                except Exception as exc:
                    METRICS.reconcile_runs.inc("error")
                    logger.error("Reconciliation iteration failed: %s", type(exc).__name__)
            await anyio.sleep(interval_seconds)
