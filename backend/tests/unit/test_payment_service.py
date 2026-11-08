"""Unit tests for PaymentService.execute and atomic state effects (§3.8, AC3.5, AC3.6, AC3.8)."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.fakes.clock import FakeClock
from tests.fakes.paypal import FakePayPalTransport
from tests.fakes.payments import make_payment_service as _setup_service, seed_order_and_attempt as _seed_order_and_attempt




@pytest.mark.anyio
async def test_execute_refuses_non_initiated_attempt() -> None:
    svc, store, transport, _ = _setup_service()
    _, attempt = _seed_order_and_attempt(store, status="completed")
    res = await svc.execute(attempt)
    assert res.status == "no_result"
    assert len(transport.calls) == 0


@pytest.mark.anyio
async def test_execute_oauth_preflight_failure_leaves_attempt_initiated() -> None:
    transport = FakePayPalTransport()
    transport.oauth_status = 401
    svc, store, _, _ = _setup_service(paypal_transport=transport)
    order_id, attempt = _seed_order_and_attempt(store)

    res = await svc.execute(attempt)
    assert res.status == "no_result"

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


@pytest.mark.anyio
async def test_payment_rpc_deadlock_retries_once():
    svc,store,transport,_ = _setup_service()
    oid,attempt = _seed_order_and_attempt(store)
    store.fail_next('mark_attempt_sent','40P01')
    result = await svc.execute(attempt)
    assert result.status == 'completed'
    assert store.calls.count('mark_attempt_sent') == 2
    assert len([c for c in transport.calls if c.path == '/v2/checkout/orders']) == 1

@pytest.mark.anyio
async def test_replayed_capture_422_never_releases_reservation():
    svc,store,transport,_ = _setup_service()
    oid,attempt = _seed_order_and_attempt(store,capture_sent_at=store.now(),capture_send_count=1)
    transport.orders_mode = 'approved_no_capture'
    transport.custom_routes['/v2/checkout/orders/ORD-APPR-001/capture'] = (422,{'name':'UNPROCESSABLE_ENTITY','details':[{'issue':'ORDER_ALREADY_CAPTURED'}]})
    result = await svc.execute(attempt)
    assert result.status == 'uncertain'
    assert store.spending_records[0]['status'] == 'reserved'
    assert store.orders[oid]['status'] == 'REQUIRES_RECONCILIATION'
