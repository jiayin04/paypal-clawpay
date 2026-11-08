"""PayPal integration errors and response classifier (§3.7, AC3.5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

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


ClassificationStatus = Literal["completed", "pending", "failed", "uncertain", "capture_needed", "no_result"]


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
    # Legacy aliases are retained for callers outside the payment executor.
    source = {"create": "first_create", "capture_response": "first_create"}.get(source, source)
    def value(name):
        return attempt.get(name) if isinstance(attempt, dict) else getattr(attempt, name, None)
    if isinstance(outcome, ProviderOrder):
        common = dict(provider_order_id=outcome.id, provider_capture_id=outcome.capture_id,
                      provider_status=outcome.capture_status or outcome.status)
        if outcome.capture_status in ("COMPLETED", "REFUNDED", "PARTIALLY_REFUNDED"):
            if attempt is not None:
                from app.domain.money import to_paypal_value
                raw_mismatch = outcome.capture_amount_value is not None and outcome.capture_amount_value != to_paypal_value(value("amount_minor"),value("currency"))
                if raw_mismatch or outcome.capture_amount_minor is None or outcome.capture_amount_minor != value("amount_minor"):
                    return PaymentClassification("uncertain", **{**common, "provider_status": "amount_mismatch"}, failure_code="amount_mismatch")
                if outcome.capture_currency is None or outcome.capture_currency != value("currency"):
                    return PaymentClassification("uncertain", **{**common, "provider_status": "currency_mismatch"}, failure_code="currency_mismatch")
            return PaymentClassification("completed", **common)
        if outcome.capture_status == "PENDING":
            return PaymentClassification("pending", **common)
        if outcome.capture_status in ("DECLINED", "FAILED", "DENIED"):
            return PaymentClassification("failed", **common, failure_code=outcome.capture_status_reason or outcome.capture_status)
        if outcome.status == "PAYER_ACTION_REQUIRED":
            return PaymentClassification("failed", **common, failure_code="payer_action_required")
        if outcome.status == "APPROVED" and outcome.capture_id is None:
            return PaymentClassification("capture_needed", **common)
        return PaymentClassification("uncertain", **common)
    if isinstance(outcome, PayPalApiError):
        code = outcome.issue or outcome.name
        if source == "poll":
            return PaymentClassification("no_result")
        if source == "first_create" and outcome.status in (400,401,403,422) and code not in ("DUPLICATE_INVOICE_ID", "ORDER_ALREADY_CAPTURED"):
            return PaymentClassification("failed", provider_status=outcome.name, failure_code=code)
        return PaymentClassification("uncertain", provider_status=outcome.name, failure_code=code)
    return PaymentClassification("no_result" if source == "poll" else "uncertain", failure_code=type(outcome).__name__)
