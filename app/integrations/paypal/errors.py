"""PayPal integration errors and response classifier (§3.7, AC3.5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.integrations.paypal.models import ProviderOrder


class PayPalError(Exception):
    """Base class for PayPal errors."""


class PayPalAuthError(PayPalError):
    """Failed to obtain or refresh OAuth token."""


class PayPalUnavailable(PayPalError):
    """PayPal service is unreachable, timed out, or connection failed."""


class PayPalApiError(PayPalError):
    """PayPal API returned an error response (4xx/5xx)."""

    def __init__(
        self,
        status: int,
        name: str,
        issue: str | None = None,
        debug_id: str | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(f"PayPal API error {status}: {name} (issue={issue}, debug_id={debug_id})")
        self.status = status
        self.name = name
        self.issue = issue
        self.debug_id = debug_id
        self.details = details or []


ClassificationStatus = Literal["completed", "pending", "failed", "uncertain", "capture_needed"]


@dataclass(frozen=True)
class PaymentClassification:
    """Classified result of a payment outcome used by payment_service."""

    status: ClassificationStatus
    provider_order_id: str | None = None
    provider_capture_id: str | None = None
    provider_status: str | None = None
    failure_code: str | None = None
    reason: str | None = None


def classify(outcome: Any, source: str, attempt: Any = None) -> PaymentClassification:
    """Classify PayPal response or exception into completed, pending, failed, uncertain, or capture_needed.

    Governed by the three tables in contracts.md §3.7:
    - Table 1: 2xx outcomes
    - Table 2: 4xx/5xx PayPalApiError outcomes
    - Table 3: Exception outcomes (timeouts, network, unavailable)
    Also validates captured amount and currency against attempt if provided.
    """
    # -----------------------------------------------------------------------
    # Table 1: 2xx HTTP response represented as ProviderOrder
    # -----------------------------------------------------------------------
    if isinstance(outcome, ProviderOrder):
        order_id = outcome.id
        cap_id = outcome.capture_id
        cap_status = outcome.capture_status

        # 1. Capture COMPLETED: check captured amount and currency
        if cap_status == "COMPLETED":
            if attempt is not None:
                exp_amount = getattr(attempt, "amount_minor", None)
                if exp_amount is None and isinstance(attempt, dict):
                    exp_amount = attempt.get("amount_minor")

                exp_curr = getattr(attempt, "currency", None)
                if exp_curr is None and isinstance(attempt, dict):
                    exp_curr = attempt.get("currency")

                if (
                    exp_amount is not None
                    and outcome.capture_amount_minor is not None
                    and exp_amount != outcome.capture_amount_minor
                ):
                    return PaymentClassification(
                        status="uncertain",
                        provider_order_id=order_id,
                        provider_capture_id=cap_id,
                        provider_status="amount_mismatch",
                        failure_code="amount_mismatch",
                        reason=f"Captured amount {outcome.capture_amount_minor} != expected {exp_amount}",
                    )

                if (
                    exp_curr is not None
                    and outcome.capture_currency is not None
                    and exp_curr != outcome.capture_currency
                ):
                    return PaymentClassification(
                        status="uncertain",
                        provider_order_id=order_id,
                        provider_capture_id=cap_id,
                        provider_status="currency_mismatch",
                        failure_code="currency_mismatch",
                        reason=f"Captured currency {outcome.capture_currency} != expected {exp_curr}",
                    )

            return PaymentClassification(
                status="completed",
                provider_order_id=order_id,
                provider_capture_id=cap_id,
                provider_status="COMPLETED",
            )

        # 2. Capture PENDING
        if cap_status == "PENDING":
            return PaymentClassification(
                status="pending",
                provider_order_id=order_id,
                provider_capture_id=cap_id,
                provider_status="PENDING",
            )

        # 3. Capture DECLINED or FAILED
        if cap_status in ("DECLINED", "FAILED"):
            failure_code = outcome.capture_status_reason or cap_status
            return PaymentClassification(
                status="failed",
                provider_order_id=order_id,
                provider_capture_id=cap_id,
                provider_status=cap_status,
                failure_code=failure_code,
            )

        # 4. Order status PAYER_ACTION_REQUIRED (vault token unusable without buyer)
        if outcome.status == "PAYER_ACTION_REQUIRED":
            return PaymentClassification(
                status="failed",
                provider_order_id=order_id,
                provider_status="PAYER_ACTION_REQUIRED",
                failure_code="payer_action_required",
            )

        # 5. Order status VOIDED
        if outcome.status == "VOIDED":
            return PaymentClassification(
                status="failed",
                provider_order_id=order_id,
                provider_status="VOIDED",
                failure_code="voided",
            )

        # 6. Order CREATED or APPROVED without a capture
        if outcome.status in ("CREATED", "APPROVED") and cap_id is None:
            if source == "create":
                return PaymentClassification(
                    status="capture_needed",
                    provider_order_id=order_id,
                    provider_status=outcome.status,
                )
            return PaymentClassification(
                status="pending",
                provider_order_id=order_id,
                provider_status=outcome.status,
            )

        # Catch-all for other 2xx order statuses
        return PaymentClassification(
            status="pending",
            provider_order_id=order_id,
            provider_status=outcome.status,
        )

    # -----------------------------------------------------------------------
    # Table 2: 4xx/5xx API Errors
    # -----------------------------------------------------------------------
    if isinstance(outcome, PayPalApiError):
        # 422 Unprocessable Entity
        if outcome.status == 422:
            # Exception: DUPLICATE_INVOICE_ID is uncertain (previous call may have charged)
            if outcome.issue == "DUPLICATE_INVOICE_ID" or outcome.name == "DUPLICATE_INVOICE_ID":
                return PaymentClassification(
                    status="uncertain",
                    provider_status="DUPLICATE_INVOICE_ID",
                    failure_code="DUPLICATE_INVOICE_ID",
                )
            code = outcome.issue or outcome.name
            return PaymentClassification(
                status="failed",
                provider_status=outcome.name,
                failure_code=code,
            )

        # 400, 401, 403: definitive client failure
        if outcome.status in (400, 401, 403):
            code = outcome.issue or outcome.name
            return PaymentClassification(
                status="failed",
                provider_status=outcome.name,
                failure_code=code,
            )

        # 409, 429, 5xx: uncertain
        if outcome.status in (409, 429) or outcome.status >= 500:
            return PaymentClassification(
                status="uncertain",
                provider_status=str(outcome.status),
                failure_code=outcome.name,
            )

        # 404: on query/get, uncertain whether order was created
        if outcome.status == 404:
            return PaymentClassification(
                status="uncertain",
                provider_status="404",
                failure_code="not_found",
            )

    # -----------------------------------------------------------------------
    # Table 3: Timeouts, Network Errors, and Unhandled Exceptions
    # -----------------------------------------------------------------------
    if isinstance(outcome, (httpx.TimeoutException, PayPalUnavailable, httpx.NetworkError, Exception)):
        return PaymentClassification(
            status="uncertain",
            failure_code=type(outcome).__name__,
        )

    return PaymentClassification(status="uncertain", failure_code="unknown_outcome")
