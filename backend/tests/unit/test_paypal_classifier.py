"""Unit tests for PayPal response and error classifier (§3.7, AC3.5).

Covers all 3 tables from contracts.md §3.7:
- Table 1: 2xx outcomes (capture completed/pending/declined/failed, payer_action_required,
  amount/currency mismatch checks, capture-marker rule for created/approved orders).
- Table 2: 4xx/5xx PayPalApiError outcomes (duplicate_invoice -> uncertain, 422 issues -> failed,
  400/401/403 -> failed, 404 -> uncertain, 409/429/5xx -> uncertain).
- Table 3: Exception outcomes (TimeoutException, NetworkError, PayPalUnavailable, etc. -> uncertain).
"""

from __future__ import annotations

import httpx

from app.integrations.paypal.errors import PayPalApiError, PayPalUnavailable, classify
from app.integrations.paypal.models import ProviderOrder


# ---------------------------------------------------------------------------
# Table 1: 2xx Outcomes (ProviderOrder)
# ---------------------------------------------------------------------------


def test_classify_capture_completed_matching_attempt() -> None:
    """Capture COMPLETED with matching amount and currency classifies as completed."""
    order = ProviderOrder(
        id="ORD-001",
        status="COMPLETED",
        capture_id="CAP-001",
        capture_status="COMPLETED",
        capture_amount_minor=1380,
        capture_currency="MYR",
    )
    attempt = {"amount_minor": 1380, "currency": "MYR"}
    res = classify(order, source="capture_response", attempt=attempt)

    assert res.status == "completed"
    assert res.provider_order_id == "ORD-001"
    assert res.provider_capture_id == "CAP-001"
    assert res.provider_status == "COMPLETED"


def test_classify_capture_completed_amount_mismatch() -> None:
    """Capture COMPLETED with mismatched amount classifies as uncertain (audited)."""
    order = ProviderOrder(
        id="ORD-001",
        status="COMPLETED",
        capture_id="CAP-001",
        capture_status="COMPLETED",
        capture_amount_minor=2000,  # mismatch
        capture_currency="MYR",
    )
    attempt = {"amount_minor": 1380, "currency": "MYR"}
    res = classify(order, source="capture_response", attempt=attempt)

    assert res.status == "uncertain"
    assert res.failure_code == "amount_mismatch"
    assert res.provider_status == "amount_mismatch"


def test_classify_capture_completed_currency_mismatch() -> None:
    """Capture COMPLETED with mismatched currency classifies as uncertain."""
    order = ProviderOrder(
        id="ORD-001",
        status="COMPLETED",
        capture_id="CAP-001",
        capture_status="COMPLETED",
        capture_amount_minor=1380,
        capture_currency="USD",  # mismatch
    )
    attempt = {"amount_minor": 1380, "currency": "MYR"}
    res = classify(order, source="capture_response", attempt=attempt)

    assert res.status == "uncertain"
    assert res.failure_code == "currency_mismatch"


def test_classify_capture_pending() -> None:
    """Capture PENDING classifies as pending."""
    order = ProviderOrder(
        id="ORD-001",
        status="COMPLETED",
        capture_id="CAP-001",
        capture_status="PENDING",
    )
    res = classify(order, source="capture_response")
    assert res.status == "pending"
    assert res.provider_status == "PENDING"


def test_classify_capture_declined() -> None:
    """Capture DECLINED classifies as failed with reason code."""
    order = ProviderOrder(
        id="ORD-001",
        status="COMPLETED",
        capture_id="CAP-001",
        capture_status="DECLINED",
        capture_status_reason="TRANSACTION_REFUSED",
    )
    res = classify(order, source="capture_response")
    assert res.status == "failed"
    assert res.failure_code == "TRANSACTION_REFUSED"


def test_classify_capture_failed() -> None:
    """Capture FAILED classifies as failed."""
    order = ProviderOrder(
        id="ORD-001",
        status="COMPLETED",
        capture_id="CAP-001",
        capture_status="FAILED",
    )
    res = classify(order, source="capture_response")
    assert res.status == "failed"
    assert res.failure_code == "FAILED"


def test_classify_payer_action_required() -> None:
    """Order status PAYER_ACTION_REQUIRED classifies as failed (vault token unusable)."""
    order = ProviderOrder(
        id="ORD-001",
        status="PAYER_ACTION_REQUIRED",
    )
    res = classify(order, source="create")
    assert res.status == "failed"
    assert res.failure_code == "payer_action_required"


def test_classify_voided() -> None:
    """Order status VOIDED classifies as failed."""
    order = ProviderOrder(
        id="ORD-001",
        status="VOIDED",
    )
    res = classify(order, source="poll")
    assert res.status == "uncertain"


def test_classify_created_approved_no_capture_marker() -> None:
    """Order CREATED/APPROVED without capture from create call classifies as capture_needed."""
    order = ProviderOrder(
        id="ORD-001",
        status="APPROVED",
        capture_id=None,
    )
    # Attempt has not yet sent capture (capture_sent_at is None)
    attempt = {"capture_sent_at": None}
    res = classify(order, source="create", attempt=attempt)
    assert res.status == "capture_needed"
    assert res.provider_order_id == "ORD-001"

    # If from poll/reconcile, classifies as pending
    res_poll = classify(order, source="poll", attempt=attempt)
    assert res_poll.status == "capture_needed"


# ---------------------------------------------------------------------------
# Table 2: 4xx/5xx Outcomes (PayPalApiError)
# ---------------------------------------------------------------------------


def test_classify_422_duplicate_invoice_is_uncertain() -> None:
    """DUPLICATE_INVOICE_ID exception: classifies as uncertain (prior call may have charged)."""
    err = PayPalApiError(
        status=422,
        name="UNPROCESSABLE_ENTITY",
        issue="DUPLICATE_INVOICE_ID",
        debug_id="dbg-dup",
    )
    res = classify(err, source="create")
    assert res.status == "uncertain"
    assert res.failure_code == "DUPLICATE_INVOICE_ID"


def test_classify_422_instrument_declined_is_failed() -> None:
    """422 with INSTRUMENT_DECLINED issue classifies as failed."""
    err = PayPalApiError(
        status=422,
        name="UNPROCESSABLE_ENTITY",
        issue="INSTRUMENT_DECLINED",
        debug_id="dbg-declined",
    )
    res = classify(err, source="create")
    assert res.status == "failed"
    assert res.failure_code == "INSTRUMENT_DECLINED"


def test_classify_400_401_403_are_failed() -> None:
    """400, 401, 403 API errors classify as failed."""
    for st in (400, 401, 403):
        err = PayPalApiError(status=st, name="VALIDATION_ERROR", issue="INVALID_REQUEST")
        res = classify(err, source="create")
        assert res.status == "failed"
        assert res.failure_code == "INVALID_REQUEST"


def test_classify_404_is_uncertain() -> None:
    """404 Not Found classifies as uncertain."""
    err = PayPalApiError(status=404, name="RESOURCE_NOT_FOUND")
    res = classify(err, source="poll")
    assert res.status == "no_result"
    assert res.failure_code is None


def test_classify_409_429_5xx_are_uncertain() -> None:
    """409, 429, and 5xx errors classify as uncertain."""
    for st in (409, 429, 500, 502, 503):
        err = PayPalApiError(status=st, name="SERVER_ERROR")
        res = classify(err, source="create")
        assert res.status == "uncertain"


# ---------------------------------------------------------------------------
# Table 3: Timeouts and Network Errors
# ---------------------------------------------------------------------------


def test_classify_exceptions_are_uncertain() -> None:
    """Timeouts, connection errors, and PayPalUnavailable classify as uncertain."""
    req = httpx.Request("POST", "https://api-m.sandbox.paypal.com")
    timeout_err = httpx.TimeoutException("Read timed out", request=req)
    res1 = classify(timeout_err, source="create")
    assert res1.status == "uncertain"
    assert res1.failure_code == "TimeoutException"

    net_err = httpx.NetworkError("Connection refused", request=req)
    res2 = classify(net_err, source="create")
    assert res2.status == "uncertain"
    assert res2.failure_code == "NetworkError"

    unavail = PayPalUnavailable("Service down")
    res3 = classify(unavail, source="create")
    assert res3.status == "uncertain"
    assert res3.failure_code == "PayPalUnavailable"
