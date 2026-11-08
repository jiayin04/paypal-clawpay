"""Service container. ``build_services(settings)`` wires the Supabase-backed
repositories lazily (no client is created until the first DB call); tests pass
in-memory repositories instead.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from supabase import Client

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.exceptions import ServiceUnavailable
from app.core.security import SlidingWindowLimiter
from app.integrations.paypal import PayPalClient, PayPalGateway, PayPalService
from app.integrations.paypal.agent_gateway import A2APayPalGateway
from app.integrations.supabase import client as supabase_client
from app.repositories.bundle import Repositories
from app.repositories.supabase.approvals import SupabaseApprovalRepository
from app.repositories.supabase.conversations import SupabaseConversationRepository
from app.repositories.supabase.devices import SupabaseDeviceRepository
from app.repositories.supabase.events import SupabaseInsertRepository
from app.repositories.supabase.merchants import SupabaseMerchantRepository
from app.repositories.supabase.orders import SupabaseOrderRepository
from app.repositories.supabase.payment_methods import SupabasePaymentMethodRepository
from app.repositories.supabase.payments import SupabasePaymentRepository
from app.repositories.supabase.spending import SupabaseSpendingRepository
from app.repositories.supabase.webhooks import SupabaseWebhookRepository
from app.repositories.user_scoped import make_user_reader_factory
from app.services.approval_service import ApprovalService
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService, KeyResolver
from app.services.conversation_service import ConversationService
from app.services.device_service import DeviceService
from app.services.llm_proxy_service import LLMProxyService
from app.services.merchant_service import MerchantService
from app.services.order_service import OrderService
from app.services.payment_method_service import PaymentMethodService
from app.services.payment_service import PaymentService
from app.services.reconciliation_service import ReconciliationService
from app.services.spending_service import SpendingService
from app.services.tts_service import TTSService
from app.services.stt_service import STTService
from app.services.webhook_service import WebhookService


REGISTER_FAILURES_PER_MINUTE = 10


@dataclass
class Limiters:
    auth_failures: SlidingWindowLimiter
    register_failures: SlidingWindowLimiter
    mcp_requests: SlidingWindowLimiter
    mcp_tool_calls: SlidingWindowLimiter
    device_requests: SlidingWindowLimiter
    revoked_token_audit: SlidingWindowLimiter


@dataclass
class Services:
    settings: Settings
    clock: Clock
    repos: Repositories
    user_reader: Callable[[str], Any]
    auth: AuthService
    audit: AuditService
    devices: DeviceService
    merchants: MerchantService
    orders: OrderService
    spending: SpendingService
    conversations: ConversationService
    approvals: ApprovalService
    payments: PaymentService
    payment_methods: PaymentMethodService
    reconciliation: ReconciliationService
    paypal: PayPalGateway
    limiters: Limiters
    webhooks: WebhookService
    llm: LLMProxyService
    tts: TTSService
    stt: STTService


def _lazy_client_provider(settings: Settings) -> Callable[[], Client]:
    lock = threading.Lock()
    holder: dict[str, Client] = {}

    def provider() -> Client:
        if not settings.supabase_configured:
            raise ServiceUnavailable("The database is not configured.")
        with lock:
            if "client" not in holder:
                holder["client"] = supabase_client.create_service_client(settings)
            return holder["client"]

    return provider


def supabase_repositories(settings: Settings) -> Repositories:
    provider = _lazy_client_provider(settings)
    return Repositories(
        devices=SupabaseDeviceRepository(provider),
        merchants=SupabaseMerchantRepository(provider),
        orders=SupabaseOrderRepository(provider),
        spending=SupabaseSpendingRepository(provider),
        payment_methods=SupabasePaymentMethodRepository(provider),
        conversations=SupabaseConversationRepository(provider),
        a2a_messages=SupabaseInsertRepository(provider, "a2a_messages"),
        audit=SupabaseInsertRepository(provider, "audit_logs"),
        approvals=SupabaseApprovalRepository(provider),
        payments=SupabasePaymentRepository(provider),
        webhooks=SupabaseWebhookRepository(provider),
    )


def build_services(
    settings: Settings,
    *,
    clock: Clock | None = None,
    repos: Repositories | None = None,
    user_reader: Callable[[str], Any] | None = None,
    key_resolver: KeyResolver | None = None,
    merchant_transports: Mapping[str, httpx.AsyncBaseTransport] | None = None,
    paypal_gateway: PayPalGateway | None = None,
    paypal_transport: httpx.AsyncBaseTransport | None = None,
    paypal_agent_transport: httpx.AsyncBaseTransport | None = None,
    llm_client: httpx.AsyncClient | None = None,
    tts_client: httpx.AsyncClient | None = None,
    stt_client: httpx.AsyncClient | None = None,
) -> Services:
    clock = clock or SystemClock()
    repos = repos or supabase_repositories(settings)
    audit = AuditService(repos.audit)
    spending = SpendingService(settings, repos.spending)
    merchants = MerchantService(settings, clock, audit, repos.a2a_messages, merchant_transports)
    if paypal_gateway is None:
        if settings.paypal_agent_enabled:
            # Multi-agent mode: the PayPal agent holds the credentials; ClawPay keeps
            # policy, idempotency keys and classification.
            paypal_gateway = A2APayPalGateway(settings, transport=paypal_agent_transport)
        else:
            paypal_client = PayPalClient(settings, transport=paypal_transport)
            paypal_gateway = PayPalService(paypal_client)
    payments = PaymentService(settings, clock, repos.payments, repos.orders, paypal_gateway, audit=audit)
    mono = clock.monotonic
    limiters = Limiters(
        auth_failures=SlidingWindowLimiter(settings.auth_failures_per_minute, 60, monotonic=mono),
        register_failures=SlidingWindowLimiter(REGISTER_FAILURES_PER_MINUTE, 60, monotonic=mono),
        mcp_requests=SlidingWindowLimiter(settings.mcp_requests_per_minute, 60, monotonic=mono),
        mcp_tool_calls=SlidingWindowLimiter(settings.mcp_tool_calls_per_minute, 60, monotonic=mono),
        device_requests=SlidingWindowLimiter(settings.device_requests_per_minute, 60, monotonic=mono),
        revoked_token_audit=SlidingWindowLimiter(1, 60, monotonic=mono),
    )
    reconciliation = ReconciliationService(
        clock=clock,
        payment_repo=repos.payments,
        order_repo=repos.orders,
        paypal=paypal_gateway,
        payment_service=payments,
    )
    payment_method_svc = PaymentMethodService(
        settings=settings,
        clock=clock,
        payment_methods=repos.payment_methods,
        paypal=paypal_gateway,
        audit=audit,
    )
    return Services(
        settings=settings,
        clock=clock,
        repos=repos,
        user_reader=user_reader or make_user_reader_factory(settings),
        auth=AuthService(settings, key_resolver),
        audit=audit,
        devices=DeviceService(settings, repos.devices),
        merchants=merchants,
        orders=OrderService(
            settings, clock, repos.orders, repos.merchants, repos.payment_methods, spending, merchants,
            payment_repo=repos.payments, payment_service=payments,
        ),
        spending=spending,
        conversations=ConversationService(settings, clock, repos.conversations),
        approvals=ApprovalService(
            settings, clock, repos.approvals, repos.orders, payment_service=payments, payment_repo=repos.payments, audit=audit
        ),
        payments=payments,
        payment_methods=payment_method_svc,
        reconciliation=reconciliation,
        paypal=paypal_gateway,
        limiters=limiters,
        webhooks=WebhookService(settings,clock,repos.webhooks,repos.payments,paypal_gateway,audit),
        llm=LLMProxyService(settings,clock,llm_client or httpx.AsyncClient(timeout=settings.llm_proxy_timeout_seconds)),
        tts=TTSService(settings, clock, tts_client or httpx.AsyncClient(timeout=settings.fish_audio_timeout_seconds)),
        stt=STTService(settings, clock, stt_client or httpx.AsyncClient(timeout=settings.fish_audio_timeout_seconds)),
    )
