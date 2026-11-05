"""Merchant registry, search fan-out, quotes, and untrusted-response validation (§2.5).

Every merchant answer is untrusted. Quotes are rejected (never "fixed") when
any rule fails; totals are recomputed here and again inside the create RPC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from app.core.clock import Clock
from app.core.config import Settings
from app.core.metrics import METRICS
from app.integrations.a2a.client import A2AError, send_message
from app.merchant_agents import registry
from app.repositories.ports.events import A2AMessageRepository
from app.schemas.merchant import (
    CancelResponse,
    ConfirmResponse,
    MerchantErrorResponse,
    QuoteResponse,
    ReserveResponse,
    SearchProduct,
    SearchResponse,
)
from app.services.audit_service import AuditService


logger = logging.getLogger(__name__)

IN_PROCESS_URL = "http://merchant.internal/a2a"
SEARCH_TIMEOUT_CAP = 5.0
MIN_QUOTE_LIFETIME = timedelta(seconds=30)
MAX_QUOTE_LIFETIME = timedelta(minutes=30)
MAX_PAYLOAD_BYTES = 16 * 1024
MIN_RESERVATION_LIFETIME = timedelta(seconds=30)
MAX_RESERVATION_LIFETIME = timedelta(hours=24)
# Methods whose a2a_messages rows are linked to the order.
ORDER_METHODS = frozenset({"quote", "reserve", "confirm", "cancel"})
# Merchant error code -> MerchantFailure code for the fulfillment calls.
FULFILLMENT_ERRORS = {
    "out_of_stock": "out_of_stock",
    "price_changed": "price_changed",
    "unknown_sku": "unknown_sku",
    "currency_not_supported": "currency_not_supported",
    "unknown_reservation": "reservation_lost",
    "reservation_expired": "reservation_lost",
    "already_confirmed": "already_confirmed",
}


class MerchantFailure(Exception):
    """``code`` is the tool error code; ``reason`` is the internal rejection reason."""

    def __init__(self, code: str, reason: str, sku: str | None = None) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.sku = sku


@dataclass(frozen=True)
class ValidatedProduct:
    sku: str
    name: str
    merchant_id: str
    unit_price_minor: int
    currency: str


@dataclass(frozen=True)
class QuoteLineOut:
    sku: str
    name: str
    qty: int
    unit_price_minor: int
    line_total_minor: int


@dataclass(frozen=True)
class ValidatedQuote:
    merchant_code: str
    quote_id: str
    currency: str
    lines: tuple[QuoteLineOut, ...]
    total_minor: int
    expires_at: datetime  # stored value: min(merchant expiry, now + QUOTE_TTL_SECONDS)


@dataclass(frozen=True)
class ValidatedReservation:
    merchant_code: str
    reservation_id: str
    expires_at: datetime


@dataclass(frozen=True)
class ValidatedFulfillment:
    merchant_code: str
    reservation_id: str
    fulfillment_id: str


@dataclass
class A2ALog:
    """A2A rows collected during a request and written later (best-effort)."""

    user_id: str
    rows: list[dict[str, Any]] = field(default_factory=list)


def _payload(obj: Any) -> dict[str, Any]:
    if len(json.dumps(obj, separators=(",", ":"), default=str)) > MAX_PAYLOAD_BYTES:
        return {"omitted": "too_large"}
    return obj


def _parse_expiry(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class MerchantService:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        audit: AuditService,
        a2a_repo: A2AMessageRepository,
        transports: Mapping[str, httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._audit = audit
        self._a2a_repo = a2a_repo
        self._transports = dict(transports or {})
        self._apps: dict[str, FastAPI] = {}

    # -- transport -----------------------------------------------------------------

    def _client(self, code: str) -> tuple[httpx.AsyncClient, str]:
        if code in self._transports:
            return httpx.AsyncClient(transport=self._transports[code]), IN_PROCESS_URL
        endpoint = registry.endpoint(self._settings, code)
        if endpoint:
            return httpx.AsyncClient(follow_redirects=False), endpoint
        app = self._apps.get(code)
        if app is None:
            # One instance per merchant, so in-process reservations survive between calls.
            app = registry.build_app(code, self._settings, self._clock)
            self._apps[code] = app
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app)), IN_PROCESS_URL

    # -- logging -------------------------------------------------------------------

    def _log(
        self,
        log: A2ALog | None,
        *,
        code: str,
        correlation_id: str,
        direction: str,
        method: str,
        outcome: str,
        latency_ms: int | None,
        summary: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        if log is None:
            return
        log.rows.append(
            {
                "user_id": log.user_id,
                "merchant_code": code,
                "correlation_id": correlation_id,
                "direction": direction,
                "method": method,
                "outcome": outcome,
                "latency_ms": latency_ms,
                "summary": {"merchant": code, "method": method, "outcome": outcome, **summary},
                "payload": _payload(payload),
            }
        )

    async def write_log(self, log: A2ALog, order_id: str | None = None) -> None:
        """Persist collected rows; failures are WARNING only."""
        for row in log.rows:
            row = dict(row)
            row["order_id"] = order_id if row["method"] in ORDER_METHODS else None
            try:
                await self._a2a_repo.insert(row)
            except Exception as exc:
                logger.warning("a2a_messages write failed: %s", type(exc).__name__)

    async def _reject(self, code: str, reason: str, user_id: str | None) -> None:
        await self._audit.record(
            "merchant.response_rejected",
            actor_type="agent",
            actor_id=code,
            user_id=user_id,
            entity_type="merchant",
            entity_id=code,
            details={"merchant": code, "reason": reason},
        )

    async def _exchange(
        self, code: str, method: str, data: dict[str, Any], timeout: float, log: A2ALog | None
    ) -> tuple[dict[str, Any], int]:
        correlation_id = data["correlation_id"]
        self._log(
            log,
            code=code,
            correlation_id=correlation_id,
            direction="request",
            method=method,
            outcome="ok",
            latency_ms=None,
            summary={"item_count": len(data.get("items", []))} if method in ("quote", "reserve") else {},
            payload=data,
        )
        started = self._clock.monotonic()
        client, url = self._client(code)
        try:
            async with client:
                result = await send_message(client, url, correlation_id, data, timeout)
        except A2AError as exc:
            latency = int((self._clock.monotonic() - started) * 1000)
            METRICS.merchant_requests.inc(code, method, exc.outcome)
            METRICS.merchant_duration.observe(latency / 1000, code, method)
            logger.warning("Merchant %s %s failed: %s/%s", code, method, exc.outcome, exc.reason)
            self._log(
                log,
                code=code,
                correlation_id=correlation_id,
                direction="error" if exc.outcome != "rejected" else "response",
                method=method,
                outcome=exc.outcome,
                latency_ms=latency,
                summary={"reason": exc.reason, "latency_ms": latency},
                payload={},
            )
            raise
        latency = int((self._clock.monotonic() - started) * 1000)
        METRICS.merchant_requests.inc(code, method, "ok")
        METRICS.merchant_duration.observe(latency / 1000, code, method)
        return result, latency

    def _log_response(
        self,
        log: A2ALog | None,
        code: str,
        method: str,
        correlation_id: str,
        outcome: str,
        latency: int,
        summary: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        self._log(
            log,
            code=code,
            correlation_id=correlation_id,
            direction="response",
            method=method,
            outcome=outcome,
            latency_ms=latency,
            summary={**summary, "latency_ms": latency},
            payload=payload,
        )

    # -- search --------------------------------------------------------------------

    async def _search_one(
        self, code: str, query: str, max_results: int, log: A2ALog | None
    ) -> list[ValidatedProduct]:
        correlation_id = str(uuid.uuid4())
        data = {
            "type": "clawpay.search_request.v1",
            "correlation_id": correlation_id,
            "query": query,
            "max_results": max_results,
        }
        timeout = min(SEARCH_TIMEOUT_CAP, self._settings.merchant_agent_timeout_seconds)
        user_id = log.user_id if log else None
        try:
            result, latency = await self._exchange(code, "search", data, timeout, log)
        except A2AError as exc:
            if exc.outcome == "rejected":
                await self._reject(code, exc.reason, user_id)
            raise MerchantFailure("merchant_unavailable", exc.reason) from None
        try:
            response = SearchResponse.model_validate(result)
            if response.merchant_id != code:
                raise ValueError("merchant_id")
            if response.correlation_id != correlation_id:
                raise ValueError("correlation_id")
        except (ValidationError, ValueError) as exc:
            reason = "schema" if isinstance(exc, ValidationError) else str(exc)
            self._log_response(log, code, "search", correlation_id, "rejected", latency, {"reason": reason}, {})
            await self._reject(code, reason, user_id)
            raise MerchantFailure("merchant_unavailable", reason) from None
        products: list[ValidatedProduct] = []
        for raw in response.products[:50]:
            try:
                product = SearchProduct.model_validate(raw)
            except ValidationError:
                continue
            if product.currency != self._settings.default_currency:
                continue
            products.append(
                ValidatedProduct(product.sku, product.name, code, product.unit_price_minor, product.currency)
            )
        self._log_response(
            log,
            code,
            "search",
            correlation_id,
            "ok",
            latency,
            {"item_count": len(products)},
            {
                "type": response.type,
                "correlation_id": correlation_id,
                "merchant_id": code,
                "products": [
                    {"sku": p.sku, "name": p.name, "unit_price_minor": p.unit_price_minor, "currency": p.currency}
                    for p in products
                ],
            },
        )
        return products

    async def search(
        self, query: str, max_results: int, merchant_id: str | None, log: A2ALog | None = None
    ) -> list[ValidatedProduct]:
        codes = [merchant_id] if merchant_id else list(self._settings.merchant_codes)
        results = await asyncio.gather(
            *(self._search_one(code, query, max_results, log) for code in codes),
            return_exceptions=True,
        )
        merged: list[ValidatedProduct] = []
        failures = 0
        for code, result in zip(codes, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, MerchantFailure):
                    logger.error("Merchant search error for %s: %s", code, type(result).__name__)
                failures += 1
                continue
            merged.extend(result)
        if failures == len(codes):
            raise MerchantFailure("merchant_unavailable", "all_failed")
        merged.sort(key=lambda p: (p.merchant_id, p.name))
        return merged[:max_results]

    # -- quote ---------------------------------------------------------------------

    async def request_quote(
        self,
        code: str,
        items: Sequence[Mapping[str, Any]],
        currency: str,
        log: A2ALog | None = None,
    ) -> ValidatedQuote:
        correlation_id = str(uuid.uuid4())
        requested = {str(i["sku"]): int(i["qty"]) for i in items}
        data = {
            "type": "clawpay.quote_request.v1",
            "correlation_id": correlation_id,
            "items": [{"sku": sku, "qty": qty} for sku, qty in requested.items()],
            "currency": currency,
        }
        user_id = log.user_id if log else None
        try:
            result, latency = await self._exchange(
                code, "quote", data, self._settings.merchant_agent_timeout_seconds, log
            )
        except A2AError as exc:
            if exc.outcome == "rejected":
                await self._reject(code, exc.reason, user_id)
                raise MerchantFailure("quote_invalid", exc.reason) from None
            raise MerchantFailure("merchant_unavailable", exc.reason) from None

        if result.get("type") == "clawpay.error.v1":
            try:
                error = MerchantErrorResponse.model_validate(result)
                if error.correlation_id != correlation_id:
                    raise ValueError("correlation_id")
            except (ValidationError, ValueError):
                self._log_response(log, code, "quote", correlation_id, "rejected", latency, {"reason": "error_schema"}, {})
                await self._reject(code, "error_schema", user_id)
                raise MerchantFailure("quote_invalid", "error_schema") from None
            self._log_response(
                log, code, "quote", correlation_id, "ok", latency, {"error_code": error.code}, error.model_dump()
            )
            if error.code == "unknown_sku":
                sku = error.sku if error.sku in requested else None
                raise MerchantFailure("unknown_sku", "unknown_sku", sku)
            if error.code == "currency_not_supported":
                raise MerchantFailure("currency_not_supported", "currency_not_supported")
            raise MerchantFailure("quote_invalid", "bad_request")

        try:
            quote = self._validate_quote(code, correlation_id, requested, currency, result)
        except MerchantFailure as failure:
            self._log_response(
                log, code, "quote", correlation_id, "rejected", latency, {"reason": failure.reason}, {}
            )
            await self._reject(code, failure.reason, user_id)
            raise
        self._log_response(
            log,
            code,
            "quote",
            correlation_id,
            "ok",
            latency,
            {"item_count": len(quote.lines), "total_minor": quote.total_minor, "currency": quote.currency},
            {
                "type": "clawpay.quote_response.v1",
                "correlation_id": correlation_id,
                "merchant_id": code,
                "quote_id": quote.quote_id,
                "currency": quote.currency,
                "items": [line.__dict__ for line in quote.lines],
                "total_minor": quote.total_minor,
                "expires_at": quote.expires_at.isoformat(),
            },
        )
        return quote

    def _validate_quote(
        self,
        code: str,
        correlation_id: str,
        requested: Mapping[str, int],
        currency: str,
        result: Mapping[str, Any],
    ) -> ValidatedQuote:
        def fail(reason: str) -> MerchantFailure:
            return MerchantFailure("quote_invalid", reason)

        try:
            quote = QuoteResponse.model_validate(result)
        except ValidationError:
            raise fail("schema") from None
        if quote.merchant_id != code:
            raise fail("merchant_id")
        if quote.correlation_id != correlation_id:
            raise fail("correlation_id")
        if quote.currency != currency or quote.currency != self._settings.default_currency:
            raise fail("currency")
        skus = [line.sku for line in quote.items]
        if len(skus) != len(set(skus)) or set(skus) != set(requested):
            raise fail("items")
        lines: list[QuoteLineOut] = []
        for line in quote.items:
            if line.qty != requested[line.sku]:
                raise fail("qty")
            computed = line.qty * line.unit_price_minor
            if line.line_total_minor != computed:
                raise fail("line_total")
            lines.append(QuoteLineOut(line.sku, line.name, line.qty, line.unit_price_minor, computed))
        subtotal = sum(line.line_total_minor for line in lines)
        if quote.subtotal_minor != subtotal or quote.total_minor != subtotal:
            raise fail("total")
        expires = _parse_expiry(quote.expires_at)
        now = self._clock.utc_now()
        if expires is None:
            raise fail("expires_at")
        if expires <= now + MIN_QUOTE_LIFETIME or expires > now + MAX_QUOTE_LIFETIME:
            raise fail("expires_at")
        stored = min(expires, now + timedelta(seconds=self._settings.quote_ttl_seconds))
        lines.sort(key=lambda line: line.sku)
        return ValidatedQuote(code, quote.quote_id, quote.currency, tuple(lines), subtotal, stored)

    # -- fulfillment: reserve before payment, confirm / cancel after ------------------
    #
    # Same discipline as quotes: every answer is untrusted, echoed ids and totals
    # must match what ClawPay sent, and a bad answer is rejected (never "fixed").
    # Failure codes: merchant_unavailable (no usable answer), fulfillment_invalid
    # (answer broke the contract), or a FULFILLMENT_ERRORS value.

    async def _fulfillment_call(
        self, code: str, method: str, data: dict[str, Any], log: A2ALog | None
    ) -> tuple[dict[str, Any], int]:
        timeout = min(self._settings.merchant_fulfillment_timeout_seconds,
                      self._settings.merchant_agent_timeout_seconds)
        try:
            return await self._exchange(code, method, data, timeout, log)
        except A2AError as exc:
            if exc.outcome == "rejected":
                await self._reject(code, exc.reason, log.user_id if log else None)
                raise MerchantFailure("fulfillment_invalid", exc.reason) from None
            raise MerchantFailure("merchant_unavailable", exc.reason) from None

    async def _rejected(
        self, code: str, method: str, correlation_id: str, latency: int, reason: str, log: A2ALog | None
    ) -> MerchantFailure:
        self._log_response(log, code, method, correlation_id, "rejected", latency, {"reason": reason}, {})
        await self._reject(code, reason, log.user_id if log else None)
        return MerchantFailure("fulfillment_invalid", reason)

    async def _merchant_error(
        self,
        code: str,
        method: str,
        correlation_id: str,
        latency: int,
        result: Mapping[str, Any],
        skus: Sequence[str],
        log: A2ALog | None,
    ) -> MerchantFailure | None:
        """A validated ``clawpay.error.v1`` mapped to a failure, or None for a normal answer."""
        if result.get("type") != "clawpay.error.v1":
            return None
        try:
            error = MerchantErrorResponse.model_validate(result)
            if error.correlation_id != correlation_id:
                raise ValueError("correlation_id")
        except (ValidationError, ValueError):
            return await self._rejected(code, method, correlation_id, latency, "error_schema", log)
        self._log_response(
            log, code, method, correlation_id, "ok", latency, {"error_code": error.code}, error.model_dump()
        )
        failure_code = FULFILLMENT_ERRORS.get(error.code, "fulfillment_invalid")
        return MerchantFailure(failure_code, error.code, error.sku if error.sku in skus else None)

    async def reserve(
        self,
        code: str,
        order_ref: str,
        lines: Sequence[Mapping[str, Any]],
        currency: str,
        total_minor: int,
        log: A2ALog | None = None,
    ) -> ValidatedReservation:
        """Ask the merchant agent to hold ``lines`` (sku, qty, unit_price_minor) for the order."""
        correlation_id = str(uuid.uuid4())
        items = sorted(
            ({"sku": str(i["sku"]), "qty": int(i["qty"]), "unit_price_minor": int(i["unit_price_minor"])}
             for i in lines),
            key=lambda i: i["sku"],
        )
        requested = {i["sku"]: i["qty"] for i in items}
        data = {
            "type": "clawpay.reserve_request.v1",
            "correlation_id": correlation_id,
            "order_ref": order_ref,
            "currency": currency,
            "items": items,
            "total_minor": total_minor,
        }
        result, latency = await self._fulfillment_call(code, "reserve", data, log)
        failure = await self._merchant_error(code, "reserve", correlation_id, latency, result, list(requested), log)
        if failure is not None:
            raise failure
        try:
            response = ReserveResponse.model_validate(result)
        except ValidationError:
            raise await self._rejected(code, "reserve", correlation_id, latency, "schema", log) from None
        now = self._clock.utc_now()
        expires = _parse_expiry(response.expires_at)
        echoed = [line.sku for line in response.items]
        if response.merchant_id != code:
            reason = "merchant_id"
        elif response.correlation_id != correlation_id:
            reason = "correlation_id"
        elif response.order_ref != order_ref:
            reason = "order_ref"
        elif response.currency != currency:
            reason = "currency"
        elif len(echoed) != len(set(echoed)) or {line.sku: line.qty for line in response.items} != requested:
            reason = "items"
        elif response.total_minor != total_minor:
            reason = "total"
        elif expires is None or not now + MIN_RESERVATION_LIFETIME < expires <= now + MAX_RESERVATION_LIFETIME:
            reason = "expires_at"
        else:
            reason = None
        if reason is not None:
            raise await self._rejected(code, "reserve", correlation_id, latency, reason, log)
        self._log_response(
            log, code, "reserve", correlation_id, "ok", latency,
            {"item_count": len(items), "total_minor": total_minor, "currency": currency},
            response.model_dump(),
        )
        return ValidatedReservation(code, response.reservation_id, expires)  # type: ignore[arg-type]

    async def confirm(
        self, code: str, order_ref: str, reservation_id: str, log: A2ALog | None = None
    ) -> ValidatedFulfillment:
        """Tell the merchant agent the reserved order is paid; returns its fulfillment id."""
        correlation_id = str(uuid.uuid4())
        data = {
            "type": "clawpay.confirm_request.v1",
            "correlation_id": correlation_id,
            "order_ref": order_ref,
            "reservation_id": reservation_id,
        }
        result, latency = await self._fulfillment_call(code, "confirm", data, log)
        failure = await self._merchant_error(code, "confirm", correlation_id, latency, result, (), log)
        if failure is not None:
            raise failure
        try:
            response = ConfirmResponse.model_validate(result)
        except ValidationError:
            raise await self._rejected(code, "confirm", correlation_id, latency, "schema", log) from None
        for field_name, expected in (("merchant_id", code), ("correlation_id", correlation_id),
                                     ("order_ref", order_ref), ("reservation_id", reservation_id)):
            if getattr(response, field_name) != expected:
                raise await self._rejected(code, "confirm", correlation_id, latency, field_name, log)
        self._log_response(
            log, code, "confirm", correlation_id, "ok", latency,
            {"fulfillment_id": response.fulfillment_id}, response.model_dump(),
        )
        return ValidatedFulfillment(code, reservation_id, response.fulfillment_id)

    async def cancel(
        self, code: str, order_ref: str, reservation_id: str | None, log: A2ALog | None = None
    ) -> None:
        """Release the order's reservation. Without an id the merchant releases whatever it holds."""
        correlation_id = str(uuid.uuid4())
        data: dict[str, Any] = {
            "type": "clawpay.cancel_request.v1",
            "correlation_id": correlation_id,
            "order_ref": order_ref,
        }
        if reservation_id is not None:
            data["reservation_id"] = reservation_id
        result, latency = await self._fulfillment_call(code, "cancel", data, log)
        failure = await self._merchant_error(code, "cancel", correlation_id, latency, result, (), log)
        if failure is not None:
            raise failure
        try:
            response = CancelResponse.model_validate(result)
        except ValidationError:
            raise await self._rejected(code, "cancel", correlation_id, latency, "schema", log) from None
        for field_name, expected in (("merchant_id", code), ("correlation_id", correlation_id),
                                     ("order_ref", order_ref)):
            if getattr(response, field_name) != expected:
                raise await self._rejected(code, "cancel", correlation_id, latency, field_name, log)
        self._log_response(log, code, "cancel", correlation_id, "ok", latency, {}, response.model_dump())
