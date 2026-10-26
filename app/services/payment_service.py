"""Payment execution service per contracts.md §3.8."""

from __future__ import annotations

import logging
from typing import Any

from app.core.clock import Clock
from app.core.config import Settings
from app.integrations.paypal import (
    PayPalApiError,
    PayPalGateway,
    PaymentClassification,
    classify,
)
from app.repositories.protocols import (
    OrderRepository,
    PaymentAttemptRecord,
    PaymentRepository,
)

logger = logging.getLogger(__name__)


class PaymentService:
    """Orchestrates payment execution and reconciliation with PayPal Gateway (§3.8)."""

    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        payment_repo: PaymentRepository,
        order_repo: OrderRepository,
        paypal: PayPalGateway,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._payment_repo = payment_repo
        self._order_repo = order_repo
        self._paypal = paypal
        self._reconcile_throttles: dict[str, float] = {}

    async def execute(self, attempt: PaymentAttemptRecord) -> PaymentClassification:
        """Execute payment attempt using vaulted instrument (§3.8).

        1. Refuse non-initiated attempts (no-op).
        2. Ensure access token before marking attempt sent.
        3. Check first_sent_at vs replay; mark_attempt_sent if first attempt.
        4. Call create_order_with_vault with attempt.idempotency_key.
        5. If capture_needed: call mark_capture_sent, then capture_order.
           If uncertain capture and send_count >= 3: mark_attempt_exhausted.
        6. Call apply_payment_result to update atomic monotonic state.
        """
        if attempt.status != "initiated":
            logger.info("Payment attempt %s status is %s; refusing execution", attempt.id, attempt.status)
            return PaymentClassification(status=attempt.status)  # type: ignore[arg-type]

        # 2. Pre-flight OAuth check
        try:
            if hasattr(self._paypal, "ensure_access_token"):
                await self._paypal.ensure_access_token()  # type: ignore[misc]
        except Exception as exc:
            logger.warning("Pre-flight ensure_access_token failed: %s; leaving attempt initiated", exc)
            return PaymentClassification(status="pending", failure_code="oauth_failure", reason=str(exc))

        # 3. Mark attempt sent if first send
        if attempt.first_sent_at is None:
            await self._payment_repo.mark_attempt_sent(attempt.id)

        # 4. Create and charge order with vault
        vault_id = attempt.vault_token_reference or ""
        classification: PaymentClassification
        provider_order_id: str | None = None
        try:
            result_order = await self._paypal.create_order_with_vault(
                request_id=attempt.idempotency_key,
                order_ref=attempt.order_id,
                invoice_id=attempt.idempotency_key,
                description=f"ClawPay order {attempt.order_id}",
                amount_minor=attempt.amount_minor,
                currency=attempt.currency,
                vault_id=vault_id,
            )
            provider_order_id = result_order.id
            classification = classify(result_order, source="create", attempt=attempt)
        except PayPalApiError as exc:
            classification = classify(exc, source="create", attempt=attempt)
        except Exception as exc:
            classification = classify(exc, source="create", attempt=attempt)

        # 5. Capture needed flow (if 201 APPROVED/CREATED without capture)
        if classification.status == "capture_needed":
            # Pre-flight OAuth check before mark_capture_sent
            if hasattr(self._paypal, "ensure_access_token"):
                try:
                    await self._paypal.ensure_access_token()  # type: ignore[misc]
                except Exception as exc:
                    logger.warning("Capture pre-flight ensure_access_token failed: %s", exc)
                    return PaymentClassification(status="pending", failure_code="oauth_failure", reason=str(exc))

            _, send_count = await self._payment_repo.mark_capture_sent(attempt.id)
            cap_req_id = f"{attempt.idempotency_key}-capture"
            try:
                cap_order = await self._paypal.capture_order(
                    provider_order_id=provider_order_id or classification.provider_order_id or "",
                    request_id=cap_req_id,
                )
                classification = classify(cap_order, source="capture_response", attempt=attempt)
            except PayPalApiError as exc:
                classification = classify(exc, source="capture_response", attempt=attempt)
            except Exception as exc:
                classification = classify(exc, source="capture_response", attempt=attempt)

            # Uncertain capture with send_count >= 3 -> mark exhausted
            if classification.status == "uncertain" and send_count >= 3:
                logger.warning("Capture send_count reached %d and uncertain; exhausting attempt %s", send_count, attempt.id)
                await self._payment_repo.mark_attempt_exhausted(attempt.id)
                return classification

        # 6. Apply payment result atomically
        await self._payment_repo.apply_payment_result(
            attempt_id=attempt.id,
            classification=classification.status,
            provider_order_id=provider_order_id or classification.provider_order_id,
            provider_capture_id=classification.provider_capture_id,
            provider_status=classification.provider_status,
            failure_code=classification.failure_code,
            source="capture_response",
        )
        return classification

    async def reconcile_order(self, order_id: str) -> PaymentClassification | None:
        """On-demand reconciliation for a single order (§3.9)."""
        now = self._clock.monotonic()
        last_reconcile = self._reconcile_throttles.get(order_id, 0.0)
        if now - last_reconcile < 10.0:
            return None
        self._reconcile_throttles[order_id] = now

        try:
            attempt = await self._payment_repo.claim_reconcilable_attempt(order_id, lease_seconds=30)
            if attempt is None:
                return None

            if attempt.provider_order_id:
                try:
                    order = await self._paypal.get_order(attempt.provider_order_id)
                    classification = classify(order, source="poll", attempt=attempt)
                except PayPalApiError as exc:
                    classification = classify(exc, source="poll", attempt=attempt)
                except Exception as exc:
                    classification = classify(exc, source="poll", attempt=attempt)

                await self._payment_repo.apply_payment_result(
                    attempt_id=attempt.id,
                    classification=classification.status,
                    provider_order_id=attempt.provider_order_id,
                    provider_capture_id=classification.provider_capture_id,
                    provider_status=classification.provider_status,
                    failure_code=classification.failure_code,
                    source="poll",
                )
                return classification
            else:
                # Replay create_order_with_vault
                return await self.execute(attempt)
        except Exception as exc:
            logger.warning("reconcile_order failed for %s: %s", order_id, exc)
            return None
