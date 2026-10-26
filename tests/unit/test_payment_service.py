"""Unit tests for PaymentService.execute and atomic state effects (§3.8, AC3.5, AC3.6, AC3.8)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.core.config import Settings
from app.integrations.paypal.client import PayPalClient
from app.integrations.paypal.errors import PaymentClassification
from app.integrations.paypal import PayPalService
from app.repositories.protocols import PaymentAttemptRecord
from app.services.payment_service import PaymentService
from tests.fakes.clock import FakeClock
from tests.fakes.paypal import FakePayPalTransport
from tests.fakes.store import InMemoryStore


def _setup_service(
    paypal_transport: FakePayPalTransport | None = None,
    clock: FakeClock | None = None,
) -> tuple[PaymentService, InMemoryStore, FakePayPalTransport, FakeClock]:
    clock = clock or FakeClock()
    store = InMemoryStore(clock)
    transport = paypal_transport or FakePayPalTransport()
    settings = Settings(
        app_env="test",
        supabase_url="http://supabase.test",
        supabase_service_role_key="fake-key",
        device_token_pepper="test-pepper",
        internal_service_token="test-internal-service-token-at-least-32-chars",
        paypal_client_id="fake-client-id",
        paypal_client_secret="fake-client-secret",
    )
    client = PayPalClient(settings, transport=transport)
    paypal_svc = PayPalService(client)
    repos = store.repositories()
    payment_service = PaymentService(
        settings=settings,
        clock=clock,
        payment_repo=repos.payments,
        order_repo=repos.orders,
        paypal=paypal_svc,
    )
    return payment_service, store, transport, clock


def _seed_order_and_attempt(
    store: InMemoryStore,
    *,
    amount_minor: int = 2500,
    currency: str = "MYR",
    status: str = "initiated",
    first_sent_at: dt.datetime | None = None,
    capture_sent_at: dt.datetime | None = None,
    capture_send_count: int = 0,
) -> tuple[str, PaymentAttemptRecord]:
    user_id = store.add_user()
    store.add_payment_method(user_id, vault_token="vault-tok-123")
    order_id = store.create_order(
        user_id=user_id,
        merchant_id=store.add_merchant(),
        device_id=store.add_device(user_id),
        total_minor=amount_minor,
        currency=currency,
        items=[{"name": "Item 1", "qty": 1, "line_total": "RM25.00"}],
        quote_expires_at=store.now() + dt.timedelta(minutes=15),
    )
    # Reserve spending
    store.spending_records.append({
        "id": "spend-1",
        "user_id": user_id,
        "order_id": order_id,
        "amount_minor": amount_minor,
        "currency": currency,
        "status": "reserved",
        "spending_day": store.now().date().isoformat(),
        "created_at": store.now(),
        "updated_at": store.now(),
    })
    # Transition order to PAYMENT_PENDING
    order = store.orders[order_id]
    order["status"] = "PAYMENT_PENDING"

    attempt_id = "attempt-1"
    store.payment_attempts[attempt_id] = {
        "id": attempt_id,
        "order_id": order_id,
        "payment_method_id": "pm-1",
        "provider": "paypal",
        "status": status,
        "idempotency_key": f"clawpay-{order_id}-1",
        "amount_minor": amount_minor,
        "currency": currency,
        "claimed_until": store.now() + dt.timedelta(seconds=30),
        "first_sent_at": first_sent_at,
        "capture_sent_at": capture_sent_at,
        "capture_send_count": capture_send_count,
        "completed_at": None,
        "exhausted_at": None,
        "provider_order_id": None,
        "provider_capture_id": None,
        "provider_status": None,
        "failure_code": None,
        "vault_token_reference": "vault-tok-123",
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    att_rec = PaymentAttemptRecord(
        id=attempt_id,
        order_id=order_id,
        user_id=user_id,
        payment_method_id="pm-1",
        provider="paypal",
        status=status,
        idempotency_key=f"clawpay-{order_id}-1",
        amount_minor=amount_minor,
        currency=currency,
        claimed_until=store.now() + dt.timedelta(seconds=30),
        first_sent_at=first_sent_at,
        capture_sent_at=capture_sent_at,
        capture_send_count=capture_send_count,
        completed_at=None,
        exhausted_at=None,
        provider_order_id=None,
        provider_capture_id=None,
        provider_status=None,
        failure_code=None,
        vault_token_reference="vault-tok-123",
        created_at=store.now(),
    )
    return order_id, att_rec


@pytest.mark.anyio
async def test_execute_refuses_non_initiated_attempt() -> None:
    svc, store, transport, _ = _setup_service()
    _, attempt = _seed_order_and_attempt(store, status="completed")
    res = await svc.execute(attempt)
    assert res.status == "completed"
    assert len(transport.calls) == 0


@pytest.mark.anyio
async def test_execute_oauth_preflight_failure_leaves_attempt_initiated() -> None:
    transport = FakePayPalTransport()
    transport.oauth_status = 401
    svc, store, _, _ = _setup_service(paypal_transport=transport)
    order_id, attempt = _seed_order_and_attempt(store)

    res = await svc.execute(attempt)
    assert res.status == "pending"

    # Attempt remains initiated, first_sent_at is null
    raw_attempt = store.payment_attempts[attempt.id]
    assert raw_attempt["status"] == "initiated"
    assert raw_attempt["first_sent_at"] is None

    # Order remains PAYMENT_PENDING
    assert store.orders[order_id]["status"] == "PAYMENT_PENDING"

    # No order create call was made
    create_calls = [c for c in transport.calls if c.path == "/v2/checkout/orders"]
    assert len(create_calls) == 0


@pytest.mark.anyio
async def test_execute_first_sent_marker_set_before_create() -> None:
    transport = FakePayPalTransport()
    transport.orders_mode = "completed"
    svc, store, _, _ = _setup_service(paypal_transport=transport)
    order_id, attempt = _seed_order_and_attempt(store)

    res = await svc.execute(attempt)
    assert res.status == "completed"

    raw_attempt = store.payment_attempts[attempt.id]
    assert raw_attempt["first_sent_at"] is not None
    assert raw_attempt["status"] == "completed"

    # Check headers on orders call
    create_call = next(c for c in transport.calls if c.path == "/v2/checkout/orders")
    assert create_call.headers.get("paypal-request-id") == attempt.idempotency_key
    assert create_call.body["payment_source"]["paypal"]["vault_id"] == "vault-tok-123"


@pytest.mark.anyio
async def test_execute_replay_preserves_first_sent_at() -> None:
    clock = FakeClock()
    transport = FakePayPalTransport()
    transport.orders_mode = "completed"
    svc, store, _, _ = _setup_service(paypal_transport=transport, clock=clock)

    original_sent = clock.utc_now() - dt.timedelta(seconds=10)
    order_id, attempt = _seed_order_and_attempt(store, first_sent_at=original_sent)

    res = await svc.execute(attempt)
    assert res.status == "completed"

    raw_attempt = store.payment_attempts[attempt.id]
    # Marker was not bumped
    assert raw_attempt["first_sent_at"] == original_sent


@pytest.mark.anyio
async def test_execute_completed_transitions_order_and_commits_spending() -> None:
    transport = FakePayPalTransport()
    transport.orders_mode = "completed"
    svc, store, _, clock = _setup_service(paypal_transport=transport)
    order_id, attempt = _seed_order_and_attempt(store)

    res = await svc.execute(attempt)
    assert res.status == "completed"

    # Order is PAID
    assert store.orders[order_id]["status"] == "PAID"

    # Attempt is completed
    raw_attempt = store.payment_attempts[attempt.id]
    assert raw_attempt["status"] == "completed"
    assert raw_attempt["completed_at"] is not None
    assert raw_attempt["provider_status"] == "COMPLETED"

    # Spending committed
    sp = store.spending_records[0]
    assert sp["status"] == "committed"

    # Audits
    events = [a["event"] for a in store.audit_logs]
    assert "payment.completed" in events
    assert "spending.committed" in events


@pytest.mark.anyio
async def test_execute_declined_transitions_order_and_releases_spending() -> None:
    transport = FakePayPalTransport()
    transport.orders_mode = "declined"
    svc, store, _, _ = _setup_service(paypal_transport=transport)
    order_id, attempt = _seed_order_and_attempt(store)

    res = await svc.execute(attempt)
    assert res.status == "failed"

    # Order is PAYMENT_FAILED
    assert store.orders[order_id]["status"] == "PAYMENT_FAILED"

    # Attempt is failed
    raw_attempt = store.payment_attempts[attempt.id]
    assert raw_attempt["status"] == "failed"
    assert raw_attempt["failure_code"] is not None

    # Spending released
    sp = store.spending_records[0]
    assert sp["status"] == "released"

    # Audits
    events = [a["event"] for a in store.audit_logs]
    assert "payment.failed" in events
    assert "spending.released" in events


@pytest.mark.anyio
async def test_execute_pending_preserves_reservation() -> None:
    transport = FakePayPalTransport()
    transport.orders_mode = "pending"
    svc, store, _, _ = _setup_service(paypal_transport=transport)
    order_id, attempt = _seed_order_and_attempt(store)

    res = await svc.execute(attempt)
    assert res.status == "pending"

    # Order is PAYMENT_PENDING
    assert store.orders[order_id]["status"] == "PAYMENT_PENDING"

    # Attempt is pending
    raw_attempt = store.payment_attempts[attempt.id]
    assert raw_attempt["status"] == "pending"

    # Spending remains reserved
    sp = store.spending_records[0]
    assert sp["status"] == "reserved"


@pytest.mark.anyio
async def test_execute_capture_needed_flow() -> None:
    transport = FakePayPalTransport()
    transport.orders_mode = "approved_no_capture"
    transport.capture_mode = "completed"
    svc, store, _, _ = _setup_service(paypal_transport=transport)
    order_id, attempt = _seed_order_and_attempt(store)

    res = await svc.execute(attempt)
    assert res.status == "completed"

    # Both create and capture calls were made
    create_call = next(c for c in transport.calls if c.path == "/v2/checkout/orders")
    cap_call = next(c for c in transport.calls if "/capture" in c.path)
    assert cap_call.headers.get("paypal-request-id") == f"{attempt.idempotency_key}-capture"

    raw_attempt = store.payment_attempts[attempt.id]
    assert raw_attempt["capture_sent_at"] is not None
    assert raw_attempt["capture_send_count"] == 1
    assert raw_attempt["status"] == "completed"
    assert store.orders[order_id]["status"] == "PAID"


@pytest.mark.anyio
async def test_execute_uncertain_capture_exhaustion_on_third_try() -> None:
    transport = FakePayPalTransport()
    transport.orders_mode = "approved_no_capture"
    transport.capture_mode = "server_error"
    svc, store, _, _ = _setup_service(paypal_transport=transport)
    order_id, attempt = _seed_order_and_attempt(
        store,
        capture_sent_at=store.now() - dt.timedelta(seconds=60),
        capture_send_count=2,
    )

    res = await svc.execute(attempt)
    assert res.status == "uncertain"

    raw_attempt = store.payment_attempts[attempt.id]
    assert raw_attempt["capture_send_count"] == 3
    assert raw_attempt["exhausted_at"] is not None

    events = [a["event"] for a in store.audit_logs]
    assert "payment.reconcile_exhausted" in events


@pytest.mark.anyio
async def test_late_completion_after_payment_failed() -> None:
    """Late completion: payment completes after order was marked PAYMENT_FAILED."""
    svc, store, _, clock = _setup_service()
    order_id, attempt = _seed_order_and_attempt(store)

    # First mark as failed
    await store.payments.apply_payment_result(
        attempt_id=attempt.id,
        classification="failed",
        failure_code="INSTRUMENT_DECLINED",
        source="capture_response",
    )
    assert store.orders[order_id]["status"] == "PAYMENT_FAILED"
    assert store.spending_records[0]["status"] == "released"
    original_day = store.spending_records[0]["spending_day"]

    # Advance clock to next day
    clock.advance(86400)

    # PayPal later confirms payment
    res = await store.payments.apply_payment_result(
        attempt_id=attempt.id,
        classification="completed",
        provider_order_id="ORD-LATE",
        provider_capture_id="CAP-LATE",
        provider_status="COMPLETED",
        source="webhook",
    )
    assert res.ok
    assert store.orders[order_id]["status"] == "PAID"
    assert store.payment_attempts[attempt.id]["status"] == "completed"

    # New committed spending row on original spending day
    committed = [s for s in store.spending_records if s["status"] == "committed"]
    assert len(committed) == 1
    assert committed[0]["spending_day"] == original_day
    assert committed[0]["order_id"] == order_id


@pytest.mark.anyio
async def test_conflicting_failure_after_completed_ignored() -> None:
    svc, store, _, _ = _setup_service()
    order_id, attempt = _seed_order_and_attempt(store)

    # Complete payment
    await store.payments.apply_payment_result(
        attempt_id=attempt.id,
        classification="completed",
        provider_status="COMPLETED",
        source="capture_response",
    )
    assert store.orders[order_id]["status"] == "PAID"

    # Now a conflicting failure arrives
    res = await store.payments.apply_payment_result(
        attempt_id=attempt.id,
        classification="failed",
        failure_code="PAYER_ACTION_REQUIRED",
        source="webhook",
    )
    assert res.ok
    assert res.reason == "already_completed"

    # Order remains PAID, attempt remains completed
    assert store.orders[order_id]["status"] == "PAID"
    assert store.payment_attempts[attempt.id]["status"] == "completed"

    events = [a["event"] for a in store.audit_logs]
    assert "payment.conflicting_result" in events
