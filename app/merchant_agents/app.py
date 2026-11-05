"""Mock merchant agents (A2A JSON-RPC subset: ``message/send`` with a data part).

One factory serves every catalog in ``catalogs.py`` (grocer, electronics,
shopee, foodpanda, grab). Each agent answers search and quote, and the
fulfillment messages reserve / confirm / cancel (see reservations.py) against
its in-memory stock.

Not mounted on the main app. By default ClawPay calls them in-process through
``httpx.ASGITransport``. To run one standalone (no authentication; bind to
127.0.0.1 only):

    uv run uvicorn --factory app.merchant_agents.app:grocer_app --host 127.0.0.1 --port 8101
    uv run uvicorn --factory app.merchant_agents.app:shopee_app --host 127.0.0.1 --port 8104

then point ClawPay at it, e.g. MERCHANT_AGENT_ENDPOINTS=shopee=http://127.0.0.1:8104/a2a
(GROCER_AGENT_ENDPOINT / ELECTRONICS_AGENT_ENDPOINT still work for those two).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.clock import Clock, SystemClock
from app.merchant_agents import catalogs, reservations


QUOTE_TTL = timedelta(minutes=5)
SUPPORTED_CURRENCIES = frozenset({catalogs.CATALOG_CURRENCY})
MAX_SEARCH_RESULTS = 10


def _rpc_error(rpc_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})


def _agent_message(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "message",
        "role": "agent",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "data", "data": data}],
    }


def create_merchant_app(merchant_code: str, clock: Clock | None = None) -> FastAPI:
    if merchant_code not in catalogs.CATALOGS:
        raise ValueError(f"unknown merchant: {merchant_code}")
    clock = clock or SystemClock()
    meta = catalogs.CATALOGS[merchant_code]
    by_sku = {p.sku: p for p in catalogs.products(merchant_code)}
    book = reservations.ReservationBook(clock, {p.sku: p.stock for p in by_sku.values()}, f"{merchant_code[:3]}_")
    app = FastAPI(title=f"{meta['name']} (mock A2A agent)", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.reservations = book

    def _error(correlation_id: Any, code: str, sku: str | None = None) -> dict[str, Any]:
        return reservations.error(correlation_id, code, sku)

    def _search(data: dict[str, Any]) -> dict[str, Any]:
        query = data.get("query")
        max_results = data.get("max_results", 5)
        if not isinstance(query, str) or not isinstance(max_results, int):
            return _error(data.get("correlation_id"), "bad_request")
        hits = catalogs.search(merchant_code, query, max(1, min(max_results, MAX_SEARCH_RESULTS)))
        return {
            "type": "clawpay.search_response.v1",
            "correlation_id": data.get("correlation_id"),
            "merchant_id": merchant_code,
            "products": [
                {
                    "sku": p.sku,
                    "name": p.name,
                    "unit_price_minor": p.unit_price_minor,
                    "currency": catalogs.CATALOG_CURRENCY,
                }
                for p in hits
            ],
        }

    def _quote(data: dict[str, Any]) -> dict[str, Any]:
        correlation_id = data.get("correlation_id")
        currency = data.get("currency")
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return _error(correlation_id, "bad_request")
        if currency not in SUPPORTED_CURRENCIES:
            return _error(correlation_id, "currency_not_supported")
        lines = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("qty"), int):
                return _error(correlation_id, "bad_request")
            product = by_sku.get(item.get("sku"))  # type: ignore[arg-type]
            if product is None:
                return _error(correlation_id, "unknown_sku", str(item.get("sku")))
            qty = item["qty"]
            lines.append(
                {
                    "sku": product.sku,
                    "name": product.name,
                    "qty": qty,
                    "unit_price_minor": product.unit_price_minor,
                    "line_total_minor": qty * product.unit_price_minor,
                }
            )
        subtotal = sum(line["line_total_minor"] for line in lines)
        expires = (clock.utc_now() + QUOTE_TTL).replace(microsecond=0)
        return {
            "type": "clawpay.quote_response.v1",
            "correlation_id": correlation_id,
            "merchant_id": merchant_code,
            "merchant_name": meta["name"],
            "quote_id": "q_" + uuid.uuid4().hex,
            "currency": currency,
            "items": lines,
            "subtotal_minor": subtotal,
            "total_minor": subtotal,
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
        }

    def _reserve(data: dict[str, Any]) -> dict[str, Any]:
        request = reservations.parse_reserve(data)
        if isinstance(request, dict):
            return request
        if request.currency not in SUPPORTED_CURRENCIES:
            return _error(request.correlation_id, "currency_not_supported")
        for sku, _, price in request.lines:
            product = by_sku.get(sku)
            if product is None:
                return _error(request.correlation_id, "unknown_sku", sku)
            if product.unit_price_minor != price:
                return _error(request.correlation_id, "price_changed", sku)
        try:
            res = book.reserve(request)
        except reservations.ReservationError as exc:
            return _error(request.correlation_id, exc.code, exc.sku)
        return reservations.reserve_response(merchant_code, request, res)

    @app.get("/.well-known/agent-card.json")
    async def agent_card(request: Request) -> dict[str, Any]:
        return {
            "name": meta["name"],
            "description": meta["description"],
            "url": str(request.base_url).rstrip("/") + "/a2a",
            "version": "0.1.0",
            "capabilities": {},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {"id": "search", "name": "search", "description": "Search the catalog."},
                {"id": "quote", "name": "quote", "description": "Quote a cart."},
                *reservations.SKILLS,
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
        except (KeyError, TypeError, StopIteration):
            return _rpc_error(rpc_id, -32602, "Invalid params")
        if not isinstance(data, dict):
            return _rpc_error(rpc_id, -32602, "Invalid params")
        kind = data.get("type")
        if kind == "clawpay.search_request.v1":
            result = _search(data)
        elif kind == "clawpay.quote_request.v1":
            result = _quote(data)
        elif kind == reservations.RESERVE_REQUEST:
            result = _reserve(data)
        elif kind == reservations.CONFIRM_REQUEST:
            result = reservations.handle_confirm(book, merchant_code, data)
        elif kind == reservations.CANCEL_REQUEST:
            result = reservations.handle_cancel(book, merchant_code, data)
        else:
            result = _error(data.get("correlation_id"), "bad_request")
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": _agent_message(result)})

    return app


def grocer_app() -> FastAPI:
    return create_merchant_app("grocer")


def electronics_app() -> FastAPI:
    return create_merchant_app("electronics")


def shopee_app() -> FastAPI:
    return create_merchant_app("shopee")


def foodpanda_app() -> FastAPI:
    return create_merchant_app("foodpanda")


def grab_app() -> FastAPI:
    return create_merchant_app("grab")
