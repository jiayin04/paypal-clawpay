"""Reusable seeded payment state and a gateway wired to mocked HTTP."""
from __future__ import annotations

import datetime as dt

from app.core.config import Settings
from app.integrations.paypal import PayPalClient, PayPalService
from app.repositories.ports.payments import PaymentAttemptRecord
from app.services.payment_service import PaymentService
from app.services.reconciliation_service import ReconciliationService
from tests.fakes.clock import FakeClock
from tests.fakes.paypal import FakePayPalTransport
from tests.fakes.store import InMemoryStore


def make_payment_service(
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


def seed_order_and_attempt(
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


def make_reconciler(service: PaymentService, store: InMemoryStore) -> ReconciliationService:
    return ReconciliationService(store.clock, store.payments, store.repositories().orders, service._paypal, service)
