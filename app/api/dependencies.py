"""Shared FastAPI dependencies: settings, services, device / user / admin auth."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated

from fastapi import BackgroundTasks, Depends, Request
from supabase import Client

from app.core.config import Settings
from app.core.exceptions import RateLimited, ServiceUnavailable, Unauthorized
from app.core.security import DEVICE_TOKEN_RE, LLM_TOKEN_RE, client_ip
from app.integrations.supabase import create_service_client
from app.repositories.errors import RepositoryError
from app.services.container import Services
from app.services.context import AdminContext, DeviceContext, UserContext


logger = logging.getLogger(__name__)

LAST_SEEN_INTERVAL = timedelta(seconds=60)
_dashboard_warned = False


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_supabase_client(request: Request) -> Client | None:
    """Return the cached service-role client, or None when Supabase is not configured."""
    settings: Settings = request.app.state.settings
    if not settings.supabase_configured:
        return None
    client = getattr(request.app.state, "supabase_client", None)
    if client is None:
        client = create_service_client(settings)
        request.app.state.supabase_client = client
    return client


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", "?")


async def _touch_last_seen(services: Services, device_id: str) -> None:
    try:
        await services.repos.devices.touch_last_seen(device_id, services.clock.utc_now())
    except Exception as exc:
        logger.warning("last_seen update failed: %s", type(exc).__name__)


async def authenticate_device(
    request: Request,
    token: str | None,
    services: Services,
    background: BackgroundTasks,
    *,
    kind: str = "device",
) -> DeviceContext:
    """Device-token auth (design §1.3). No cache: revocation applies on the next request.

    Limiter rules: malformed tokens and lookup misses count as failures per
    client IP; a well-formed token is always looked up, so a valid device is
    never blocked even when its IP (e.g. a tunnel's 127.0.0.1) is over the limit.
    """
    settings = services.settings
    if settings.device_token_pepper is None:
        raise ServiceUnavailable("Device authentication is not configured.", "device_auth_not_configured")
    ip = client_ip(request, settings.client_ip_header)
    limiter = services.limiters.auth_failures
    pattern = LLM_TOKEN_RE if kind == "llm" else DEVICE_TOKEN_RE

    async def fail(reason: str) -> Unauthorized:
        over = await limiter.is_over(ip)
        await limiter.record(ip)
        logger.warning("Device auth failed (%s) on %s from %s", reason, _route_path(request), ip)
        if over is not None:
            raise RateLimited(over)
        return Unauthorized()

    if token is None or not pattern.match(token):
        raise await fail("malformed")

    try:
        device = await services.devices.lookup(token, kind)
    except RepositoryError as exc:
        logger.error("Device lookup failed: %s", type(exc).__name__)
        raise ServiceUnavailable() from None

    if device is not None and device.status == "active":
        now = services.clock.utc_now()
        if device.last_seen_at is None or now - device.last_seen_at >= LAST_SEEN_INTERVAL:
            background.add_task(_touch_last_seen, services, device.id)
        return DeviceContext(
            device_id=device.id,
            user_id=device.user_id,
            key_version=device.key_version,
            public_key=device.public_key,
        )

    if device is not None and device.status == "revoked":
        if await services.limiters.revoked_token_audit.acquire(device.id) is None:
            await services.audit.record(
                "device.revoked_token_used",
                actor_type="device",
                actor_id=device.id,
                user_id=device.user_id,
                entity_type="device",
                entity_id=device.id,
                details={"kind": kind},
            )
    raise await fail("unknown_or_inactive")


async def require_device(
    request: Request, background: BackgroundTasks, services: ServicesDep
) -> DeviceContext:
    """Device REST: ``cpd_`` bearer token + per-device request limit."""
    device = await authenticate_device(request, bearer_token(request), services, background)
    retry = await services.limiters.device_requests.acquire(device.device_id)
    if retry is not None:
        raise RateLimited(retry)
    return device


async def require_device_llm(
    request: Request, background: BackgroundTasks, services: ServicesDep
) -> DeviceContext:
    """LLM proxy: ``cpl_`` bearer token only (Phase 4)."""
    return await authenticate_device(request, bearer_token(request), services, background, kind="llm")


async def _verify_user(request: Request, services: Services) -> UserContext:
    token = bearer_token(request)
    if token is None:
        raise Unauthorized()
    user_id = await services.auth.verify_user_jwt(token)
    return UserContext(user_id=user_id, jwt=token)


async def require_user(request: Request, services: ServicesDep) -> UserContext:
    """Dashboard reads (RLS client): publishable key is checked before the JWT."""
    global _dashboard_warned
    if services.settings.supabase_publishable_key is None:
        if not _dashboard_warned:
            logger.warning("SUPABASE_PUBLISHABLE_KEY is not set; dashboard routes return 503")
            _dashboard_warned = True
        else:
            logger.debug("dashboard_not_configured")
        raise ServiceUnavailable("The dashboard is not configured.", "dashboard_not_configured")
    return await _verify_user(request, services)


async def require_user_service_only(request: Request, services: ServicesDep) -> UserContext:
    """Routes that use only the service client with the JWT ``sub``."""
    return await _verify_user(request, services)


async def require_admin_or_user(
    request: Request, services: ServicesDep
) -> AdminContext | UserContext:
    if services.auth.is_admin_token(bearer_token(request)):
        return AdminContext()
    return await _verify_user(request, services)
