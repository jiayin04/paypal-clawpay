"""Merchant agent registry: which merchant agents ClawPay can call, and how.

Every merchant agent speaks the same ClawPay A2A messages (search, quote,
reserve, confirm, cancel; docs/contracts.md section 10), and ClawPay treats
every answer as untrusted. A spec's ``kind`` decides how ClawPay reaches the
agent when no endpoint URL is configured for it:

- ``catalog``: in-process mock agent serving a deterministic catalog from
  ``catalogs.py`` (no network). Enabled when listed in ENABLED_MERCHANTS
  (default: grocer, electronics).
- ``api``: in-process agent that wraps a real merchant API (an adapter such
  as ``channel3.py``). Enabled when the adapter is configured, or when an
  endpoint URL is set.
- ``remote``: an agent hosted elsewhere, for example a partner's own A2A
  agent. Enabled only when an endpoint URL is set for it.

An endpoint URL in MERCHANT_AGENT_ENDPOINTS (``code=url,...``), or a legacy
``*_AGENT_ENDPOINT`` variable, makes ClawPay call that merchant over HTTP
instead of in-process.

Adding a merchant: add a MerchantSpec below, a ``public.merchants`` row
(migration, which also feeds seed.sql), and then a catalog (``catalog``), an
adapter (``api``) or an endpoint URL (``remote``). The ``merchant_id`` enums
in the MCP tools follow the enabled merchants automatically.

This module imports no agent code at import time, so the settings can use it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.core.clock import Clock
    from app.core.config import Settings


MerchantKind = Literal["catalog", "api", "remote"]


@dataclass(frozen=True)
class MerchantSpec:
    code: str
    kind: MerchantKind
    # Settings attribute of a pre-registry *_AGENT_ENDPOINT variable, if any.
    legacy_endpoint_field: str | None = None


@dataclass(frozen=True)
class Adapter:
    """An in-process agent backed by a real merchant API."""

    configured: Callable[[Settings], bool]
    create_app: Callable[[Settings, Clock], FastAPI]


# Order = order of the merchant_id enums in the MCP tool schemas.
MERCHANTS: tuple[MerchantSpec, ...] = (
    MerchantSpec("grocer", "catalog", "grocer_agent_endpoint"),
    MerchantSpec("electronics", "catalog", "electronics_agent_endpoint"),
    MerchantSpec("channel3", "api", "channel3_agent_endpoint"),
    MerchantSpec("shopee", "catalog"),
    MerchantSpec("foodpanda", "catalog"),
    MerchantSpec("grab", "catalog"),
)

BY_CODE: dict[str, MerchantSpec] = {spec.code: spec for spec in MERCHANTS}
CODES: tuple[str, ...] = tuple(BY_CODE)
CATALOG_CODES: tuple[str, ...] = tuple(s.code for s in MERCHANTS if s.kind == "catalog")
DEFAULT_ENABLED: tuple[str, ...] = ("grocer", "electronics")


def _channel3_app(settings: Settings, clock: Clock) -> FastAPI:
    from app.merchant_agents import channel3

    return channel3.create_channel3_app(settings, clock=clock)


ADAPTERS: dict[str, Adapter] = {
    "channel3": Adapter(configured=lambda s: s.channel3_api_key is not None, create_app=_channel3_app),
}


def endpoint(settings: Settings, code: str) -> str | None:
    """Configured URL for a merchant agent (MERCHANT_AGENT_ENDPOINTS wins over legacy vars)."""
    url = settings.merchant_agent_endpoints.get(code)
    if url:
        return url
    spec = BY_CODE.get(code)
    if spec is not None and spec.legacy_endpoint_field:
        return getattr(settings, spec.legacy_endpoint_field)
    return None


def enabled_codes(settings: Settings) -> tuple[str, ...]:
    listed = set(settings.enabled_merchants or DEFAULT_ENABLED)
    codes: list[str] = []
    for spec in MERCHANTS:
        if spec.kind == "catalog":
            enabled = spec.code in listed
        elif spec.kind == "api":
            enabled = ADAPTERS[spec.code].configured(settings) or endpoint(settings, spec.code) is not None
        else:
            enabled = endpoint(settings, spec.code) is not None
        if enabled:
            codes.append(spec.code)
    return tuple(codes)


def build_app(code: str, settings: Settings, clock: Clock) -> FastAPI:
    """The in-process agent for a merchant without an endpoint URL."""
    spec = BY_CODE.get(code)
    if spec is None:
        raise LookupError(f"unknown merchant: {code}")
    if spec.kind == "catalog":
        from app.merchant_agents.app import create_merchant_app

        return create_merchant_app(code, clock=clock)
    if spec.kind == "api":
        return ADAPTERS[code].create_app(settings, clock)
    raise LookupError(f"merchant {code} has no in-process agent; set its URL in MERCHANT_AGENT_ENDPOINTS")
