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
from app.integrations.supabase import client as supabase_client
from app.repositories import supabase_repos as repos_mod
from app.repositories.protocols import (
    A2AMessageRepository,
    AuditRepository,
    ConversationRepository,
    DeviceRepository,
    MerchantRepository,
    OrderRepository,
    PaymentMethodRepository,
    SpendingRepository,
)
from app.repositories.user_scoped import make_user_reader_factory
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService, KeyResolver
from app.services.conversation_service import ConversationService
from app.services.device_service import DeviceService
from app.services.merchant_service import MerchantService
from app.services.order_service import OrderService
from app.services.spending_service import SpendingService

REGISTER_FAILURES_PER_MINUTE = 10


@dataclass
class Repositories:
    devices: DeviceRepository
    merchants: MerchantRepository
    orders: OrderRepository
    spending: SpendingRepository
    payment_methods: PaymentMethodRepository
    conversations: ConversationRepository
    a2a_messages: A2AMessageRepository
    audit: AuditRepository


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
    limiters: Limiters


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
        devices=repos_mod.SupabaseDeviceRepository(provider),
        merchants=repos_mod.SupabaseMerchantRepository(provider),
        orders=repos_mod.SupabaseOrderRepository(provider),
        spending=repos_mod.SupabaseSpendingRepository(provider),
        payment_methods=repos_mod.SupabasePaymentMethodRepository(provider),
        conversations=repos_mod.SupabaseConversationRepository(provider),
        a2a_messages=repos_mod.SupabaseInsertRepository(provider, "a2a_messages"),
        audit=repos_mod.SupabaseInsertRepository(provider, "audit_logs"),
    )


def build_services(
    settings: Settings,
    *,
    clock: Clock | None = None,
    repos: Repositories | None = None,
    user_reader: Callable[[str], Any] | None = None,
    key_resolver: KeyResolver | None = None,
    merchant_transports: Mapping[str, httpx.AsyncBaseTransport] | None = None,
) -> Services:
    clock = clock or SystemClock()
    repos = repos or supabase_repositories(settings)
    audit = AuditService(repos.audit)
    spending = SpendingService(settings, repos.spending)
    merchants = MerchantService(settings, clock, audit, repos.a2a_messages, merchant_transports)
    mono = clock.monotonic
    limiters = Limiters(
        auth_failures=SlidingWindowLimiter(settings.auth_failures_per_minute, 60, monotonic=mono),
        register_failures=SlidingWindowLimiter(REGISTER_FAILURES_PER_MINUTE, 60, monotonic=mono),
        mcp_requests=SlidingWindowLimiter(settings.mcp_requests_per_minute, 60, monotonic=mono),
        mcp_tool_calls=SlidingWindowLimiter(settings.mcp_tool_calls_per_minute, 60, monotonic=mono),
        device_requests=SlidingWindowLimiter(settings.device_requests_per_minute, 60, monotonic=mono),
        revoked_token_audit=SlidingWindowLimiter(1, 60, monotonic=mono),
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
            settings, clock, repos.orders, repos.merchants, repos.payment_methods, spending, merchants
        ),
        spending=spending,
        conversations=ConversationService(settings, clock, repos.conversations),
        limiters=limiters,
    )
