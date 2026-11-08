"""Channel3 merchant agent: the ClawPay A2A merchant protocol backed by the
Channel3 product API (https://api.trychannel3.com, ``x-api-key``).

It speaks exactly the same ``clawpay.search_request.v1`` / ``quote_request.v1``
messages as the mock merchants, so ClawPay validates its answers with the same
untrusted-response rules (merchant id, exact skus/qty, integer prices, totals,
expiry) and recomputes every total itself.

Mapping:
- search -> ``POST /v1/search`` ``{"query", "limit", "config": {"country", "currency"}}``
- quote  -> ``GET /v1/products/{id}`` per line (live price at quote time)
- each product is priced from its cheapest ``InStock`` offer in the requested
  currency (ties broken by retailer domain), so search and quote pick the same
  offer while prices are unchanged.
- sku = ``C3-`` + base32(Channel3 product id), which fits ClawPay's
  ``^[A-Z0-9-]{1,64}$`` sku rule and decodes back to the case-sensitive id.

What this does NOT do: place the order with the retailer. ClawPay charges the
user's vaulted PayPal wallet to the ClawPay Sandbox merchant; fulfilment at the
offer's retailer is out of scope (demo marketplace).

By default ClawPay runs this agent in-process (no public route) when
CHANNEL3_API_KEY is set. Standalone (no authentication; bind to 127.0.0.1, every
call spends Channel3 credits):

    uv run uvicorn --factory app.merchant_agents.channel3:channel3_app --host 127.0.0.1 --port 8103
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.observability import RequestContextMiddleware
from app.domain.money import EXPONENTS


logger = logging.getLogger(__name__)

MERCHANT_CODE = "channel3"
DISPLAY_NAME = "Channel3 Marketplace"
SKU_PREFIX = "C3-"
QUOTE_TTL = timedelta(minutes=5)
MAX_SEARCH_LIMIT = 30  # Channel3 /v1/search maximum
MAX_NAME_LENGTH = 80
MAX_UNIT_PRICE_MINOR = 10_000_000
_PRODUCT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,38}$")


class Channel3Unavailable(Exception):
    """Channel3 did not answer usefully (timeout, 5xx, 401/429, malformed JSON)."""


# -- sku encoding --------------------------------------------------------------------


def product_id_to_sku(product_id: str) -> str | None:
    if not _PRODUCT_ID_RE.match(product_id):
        return None
    return SKU_PREFIX + base64.b32encode(product_id.encode("ascii")).decode("ascii").rstrip("=")


def sku_to_product_id(sku: str) -> str | None:
    if not isinstance(sku, str) or not sku.startswith(SKU_PREFIX):
        return None
    body = sku[len(SKU_PREFIX):]
    try:
        decoded = base64.b32decode(body + "=" * (-len(body) % 8)).decode("ascii")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return decoded if product_id_to_sku(decoded) == sku else None


# -- offer selection -----------------------------------------------------------------


def _price_minor(value: Any, currency: str) -> int | None:
    """Decimal major units -> integer minor units; anything inexact or out of range is None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    minor = amount * (10 ** EXPONENTS[currency])
    if not minor.is_finite() or minor != minor.to_integral_value():
        return None
    result = int(minor)
    return result if 1 <= result <= MAX_UNIT_PRICE_MINOR else None


def _offer_price(offer: dict[str, Any]) -> tuple[Any, str | None]:
    """Channel3 documents ``price`` as ``{"price", "currency"}`` (API reference) and
    ``{"amount", "currency"}`` (product guide); accept both."""
    price = offer.get("price")
    if isinstance(price, dict):
        value = price.get("price", price.get("amount"))
        return value, price.get("currency")
    return None, None


def choose_offer(product: dict[str, Any], currency: str) -> tuple[int, str] | None:
    """Cheapest in-stock offer in ``currency`` -> (unit_price_minor, retailer domain)."""
    candidates: list[tuple[int, str]] = []
    for offer in product.get("offers") or []:
        if not isinstance(offer, dict) or offer.get("availability") != "InStock":
            continue
        value, offer_currency = _offer_price(offer)
        if offer_currency != currency:
            continue
        minor = _price_minor(value, currency)
        domain = offer.get("domain")
        if minor is None or not isinstance(domain, str):
            continue
        candidates.append((minor, domain[:60]))
    return min(candidates) if candidates else None


def product_name(product: dict[str, Any], domain: str) -> str | None:
    title = product.get("title")
    if not isinstance(title, str):
        return None
    title = "".join(ch for ch in title if ch.isprintable()).strip()
    suffix = f" ({domain})"
    if not title:
        return None
    return title[: MAX_NAME_LENGTH - len(suffix)].rstrip() + suffix


# -- Channel3 HTTP ---------------------------------------------------------------------


class Channel3Client:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if settings.channel3_api_key is None:
            raise RuntimeError("CHANNEL3_API_KEY must be set for the Channel3 merchant agent")
        self._key = settings.channel3_api_key.get_secret_value()
        self._base = settings.channel3_base_url
        self._timeout = settings.channel3_timeout_seconds
        self._transport = transport

    async def _request(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> Any | None:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
                resp = await client.request(method, f"{self._base}{path}", json=json_body,
                                            headers={"x-api-key": self._key, "Accept": "application/json"})
        except httpx.HTTPError as exc:
            logger.warning("Channel3 %s failed: %s", path.split("/")[2], type(exc).__name__)
            raise Channel3Unavailable() from None
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.warning("Channel3 %s returned %d", path.split("/")[2], resp.status_code)
            raise Channel3Unavailable()
        try:
            return resp.json()
        except ValueError:
            raise Channel3Unavailable() from None

    async def search(self, query: str, limit: int, country: str, currency: str) -> list[dict[str, Any]]:
        data = await self._request("POST", "/v1/search", {
            "query": query, "limit": limit, "config": {"country": country, "currency": currency},
        })
        products = data.get("products") if isinstance(data, dict) else None
        if not isinstance(products, list):
            raise Channel3Unavailable()
        return [p for p in products if isinstance(p, dict)]

    async def product(self, product_id: str) -> dict[str, Any] | None:
        data = await self._request("GET", f"/v1/products/{product_id}")
        if data is None:
            return None
        if not isinstance(data, dict):
            raise Channel3Unavailable()
        return data


# -- A2A app ----------------------------------------------------------------------------


def _rpc_error(rpc_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})


def _agent_message(rpc_id: Any, data: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": {
        "kind": "message", "role": "agent", "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "data", "data": data}],
    }})


def create_channel3_app(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    clock = clock or SystemClock()
    client = Channel3Client(settings, transport)
    country = settings.channel3_country
    currency = settings.default_currency
    app = FastAPI(title=f"{DISPLAY_NAME} (A2A agent)", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestContextMiddleware, record_metrics=False)

    def _error(correlation_id: Any, code: str, sku: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {"type": "clawpay.error.v1", "correlation_id": correlation_id, "code": code}
        if sku is not None:
            data["sku"] = sku
        return data

    async def _search(data: dict[str, Any]) -> dict[str, Any]:
        query, max_results = data.get("query"), data.get("max_results", 5)
        if not isinstance(query, str) or not query.strip() or isinstance(max_results, bool) \
                or not isinstance(max_results, int):
            return _error(data.get("correlation_id"), "bad_request")
        limit = max(1, min(max_results, MAX_SEARCH_LIMIT))
        products = []
        for product in await client.search(query.strip()[:100], limit, country, currency):
            sku = product_id_to_sku(str(product.get("id", "")))
            offer = choose_offer(product, currency)
            name = product_name(product, offer[1]) if offer else None
            if sku and offer and name:
                products.append({"sku": sku, "name": name, "unit_price_minor": offer[0], "currency": currency})
        return {"type": "clawpay.search_response.v1", "correlation_id": data.get("correlation_id"),
                "merchant_id": MERCHANT_CODE, "products": products[:limit]}

    async def _quote(data: dict[str, Any]) -> dict[str, Any]:
        correlation_id, items = data.get("correlation_id"), data.get("items")
        if not isinstance(items, list) or not items or len(items) > 10:
            return _error(correlation_id, "bad_request")
        if data.get("currency") != currency:
            return _error(correlation_id, "currency_not_supported")
        ids: list[tuple[str, str, int]] = []
        for item in items:
            qty = item.get("qty") if isinstance(item, dict) else None
            if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 20:
                return _error(correlation_id, "bad_request")
            sku = item.get("sku")
            product_id = sku_to_product_id(sku) if isinstance(sku, str) else None
            if product_id is None:
                return _error(correlation_id, "unknown_sku", str(sku)[:64])
            ids.append((sku, product_id, qty))
        products = await asyncio.gather(*(client.product(pid) for _, pid, _ in ids))
        lines = []
        for (sku, _, qty), product in zip(ids, products, strict=True):
            offer = choose_offer(product, currency) if product else None
            name = product_name(product, offer[1]) if product and offer else None
            if offer is None or name is None:
                return _error(correlation_id, "unknown_sku", sku)
            lines.append({"sku": sku, "name": name, "qty": qty, "unit_price_minor": offer[0],
                          "line_total_minor": qty * offer[0]})
        total = sum(line["line_total_minor"] for line in lines)
        expires = (clock.utc_now() + QUOTE_TTL).replace(microsecond=0)
        return {"type": "clawpay.quote_response.v1", "correlation_id": correlation_id,
                "merchant_id": MERCHANT_CODE, "merchant_name": DISPLAY_NAME,
                "quote_id": "c3q_" + uuid.uuid4().hex, "currency": currency, "items": lines,
                "subtotal_minor": total, "total_minor": total,
                "expires_at": expires.isoformat().replace("+00:00", "Z")}

    @app.get("/.well-known/agent-card.json")
    async def agent_card(request: Request) -> dict[str, Any]:
        return {
            "name": DISPLAY_NAME,
            "description": "Products and live prices from the Channel3 catalog for ClawPay orders.",
            "url": str(request.base_url).rstrip("/") + "/a2a",
            "version": "0.1.0",
            "capabilities": {},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {"id": "search", "name": "search", "description": "Search the Channel3 catalog."},
                {"id": "quote", "name": "quote", "description": "Quote a cart at live Channel3 prices."},
            ],
        }

    @app.post("/a2a")
    async def a2a(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return _rpc_error(None, -32700, "Parse error")
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid Request")
        rpc_id = body.get("id")
        if body.get("method") != "message/send":
            return _rpc_error(rpc_id, -32601, "Method not found")
        try:
            parts = body["params"]["message"]["parts"]
            data = next(p["data"] for p in parts if p.get("kind") == "data")
        except (KeyError, TypeError, StopIteration, AttributeError):
            return _rpc_error(rpc_id, -32602, "Invalid params")
        if not isinstance(data, dict):
            return _rpc_error(rpc_id, -32602, "Invalid params")
        try:
            kind = data.get("type")
            if kind == "clawpay.search_request.v1":
                result = await _search(data)
            elif kind == "clawpay.quote_request.v1":
                result = await _quote(data)
            else:
                result = _error(data.get("correlation_id"), "bad_request")
        except Channel3Unavailable:
            # ClawPay maps a JSON-RPC error to merchant_unavailable.
            return _rpc_error(rpc_id, -32000, "Channel3 unavailable")
        return _agent_message(rpc_id, result)

    return app


def channel3_app() -> FastAPI:
    """``uvicorn --factory`` entry point (reads env / .env)."""
    from app.core.logging import configure_logging

    settings = get_settings()
    configure_logging(settings)
    return create_channel3_app(settings)
