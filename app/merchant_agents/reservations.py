"""Merchant-agent side of the fulfillment messages: reserve, confirm, cancel.

Shared by the mock agents and the Channel3 agent. A reservation is keyed by
ClawPay's ``order_ref``, so a retried reserve returns the same reservation.
Held stock counts against availability until it is confirmed (sold) or
released (cancelled, or the hold expired).

State is in memory, per agent process: a restart forgets reservations, which
ClawPay handles like an expired hold (it reserves again before confirming).

Messages (data part of an A2A ``message/send``):

- ``clawpay.reserve_request.v1`` {correlation_id, order_ref, currency,
  items: [{sku, qty, unit_price_minor}], total_minor}
  -> ``clawpay.reserve_response.v1`` {correlation_id, merchant_id, order_ref,
  reservation_id, currency, items: [{sku, qty}], total_minor, expires_at}
- ``clawpay.confirm_request.v1`` {correlation_id, order_ref, reservation_id}
  -> ``clawpay.confirm_response.v1`` {correlation_id, merchant_id, order_ref,
  reservation_id, fulfillment_id, status: "accepted"}
- ``clawpay.cancel_request.v1`` {correlation_id, order_ref, reservation_id?}
  -> ``clawpay.cancel_response.v1`` {correlation_id, merchant_id, order_ref,
  status: "released"}

Errors use ``clawpay.error.v1`` with code ``bad_request``, ``unknown_sku``,
``currency_not_supported``, ``out_of_stock``, ``price_changed``,
``unknown_reservation``, ``reservation_expired`` or ``already_confirmed``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from app.core.clock import Clock


HOLD_TTL = timedelta(minutes=15)  # covers the inline payment and the reconciler replay window
MAX_LINES = 10
MAX_QTY = 20
MAX_RESERVATIONS = 10_000
_REF_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

RESERVE_REQUEST = "clawpay.reserve_request.v1"
CONFIRM_REQUEST = "clawpay.confirm_request.v1"
CANCEL_REQUEST = "clawpay.cancel_request.v1"
REQUEST_TYPES = frozenset({RESERVE_REQUEST, CONFIRM_REQUEST, CANCEL_REQUEST})
SKILLS = (
    {"id": "reserve", "name": "reserve", "description": "Hold stock for a ClawPay order before payment."},
    {"id": "confirm", "name": "confirm", "description": "Accept a paid ClawPay order for fulfillment."},
    {"id": "cancel", "name": "cancel", "description": "Release a held reservation."},
)


class ReservationError(Exception):
    def __init__(self, code: str, sku: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.sku = sku


@dataclass
class Reservation:
    reservation_id: str
    order_ref: str
    items: dict[str, int]
    total_minor: int
    currency: str
    expires_at: datetime
    status: Literal["held", "confirmed", "released"] = "held"
    fulfillment_id: str | None = None


@dataclass(frozen=True)
class ReserveRequest:
    correlation_id: Any
    order_ref: str
    currency: str
    lines: tuple[tuple[str, int, int], ...]  # (sku, qty, unit_price_minor)
    total_minor: int


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ref(value: Any) -> str | None:
    return value if isinstance(value, str) and _REF_RE.match(value) else None


def error(correlation_id: Any, code: str, sku: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"type": "clawpay.error.v1", "correlation_id": correlation_id, "code": code}
    if sku is not None:
        data["sku"] = sku[:64]
    return data


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_reserve(data: Mapping[str, Any]) -> ReserveRequest | dict[str, Any]:
    """Structural checks only; the agent checks skus, prices and stock."""
    correlation_id = data.get("correlation_id")
    order_ref, items, total = _ref(data.get("order_ref")), data.get("items"), data.get("total_minor")
    currency = data.get("currency")
    if order_ref is None or not isinstance(currency, str) or not _is_int(total):
        return error(correlation_id, "bad_request")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_LINES:
        return error(correlation_id, "bad_request")
    lines: list[tuple[str, int, int]] = []
    for item in items:
        if not isinstance(item, dict):
            return error(correlation_id, "bad_request")
        sku, qty, price = item.get("sku"), item.get("qty"), item.get("unit_price_minor")
        if not isinstance(sku, str) or not _is_int(qty) or not 1 <= qty <= MAX_QTY or not _is_int(price):
            return error(correlation_id, "bad_request")
        lines.append((sku, qty, price))
    if len({sku for sku, _, _ in lines}) != len(lines) or sum(q * p for _, q, p in lines) != total:
        return error(correlation_id, "bad_request")
    return ReserveRequest(correlation_id, order_ref, currency, tuple(lines), total)


class ReservationBook:
    def __init__(self, clock: Clock, stock: Mapping[str, int | None], id_prefix: str) -> None:
        self._clock = clock
        self._stock = dict(stock)
        self._prefix = id_prefix
        self._by_id: dict[str, Reservation] = {}
        self._by_order: dict[str, str] = {}

    def _live(self, res: Reservation) -> bool:
        if res.status == "held" and res.expires_at <= self._clock.utc_now():
            res.status = "released"
        return res.status in ("held", "confirmed")

    def available(self, sku: str) -> int | None:
        total = self._stock.get(sku)
        if total is None:
            return None
        used = sum(res.items.get(sku, 0) for res in self._by_id.values() if self._live(res))
        return max(total - used, 0)

    def _prune(self) -> None:
        if len(self._by_id) < MAX_RESERVATIONS:
            return
        for rid in [rid for rid, res in self._by_id.items() if not self._live(res)]:
            res = self._by_id.pop(rid)
            if self._by_order.get(res.order_ref) == rid:
                del self._by_order[res.order_ref]

    def reserve(self, request: ReserveRequest) -> Reservation:
        items = {sku: qty for sku, qty, _ in request.lines}
        existing_id = self._by_order.get(request.order_ref)
        existing = self._by_id.get(existing_id) if existing_id else None
        if existing is not None and self._live(existing):
            if existing.items != items or existing.total_minor != request.total_minor:
                raise ReservationError("bad_request")  # same order, different cart
            return existing  # idempotent retry
        for sku, qty in items.items():
            free = self.available(sku)
            if free is not None and free < qty:
                raise ReservationError("out_of_stock", sku)
        self._prune()
        res = Reservation(
            reservation_id=f"{self._prefix}r_{uuid.uuid4().hex}",
            order_ref=request.order_ref,
            items=items,
            total_minor=request.total_minor,
            currency=request.currency,
            expires_at=(self._clock.utc_now() + HOLD_TTL).replace(microsecond=0),
        )
        self._by_id[res.reservation_id] = res
        self._by_order[request.order_ref] = res.reservation_id
        return res

    def confirm(self, order_ref: str, reservation_id: str) -> Reservation:
        res = self._by_id.get(reservation_id)
        if res is None or res.order_ref != order_ref:
            raise ReservationError("unknown_reservation")
        if not self._live(res):
            raise ReservationError("reservation_expired")
        if res.status == "held":
            res.status = "confirmed"
            res.fulfillment_id = f"{self._prefix}f_{uuid.uuid4().hex}"
        return res

    def cancel(self, order_ref: str, reservation_id: str | None) -> None:
        rid = reservation_id or self._by_order.get(order_ref)
        res = self._by_id.get(rid) if rid else None
        if res is None:
            return  # nothing held: cancelling is idempotent
        if res.order_ref != order_ref:
            raise ReservationError("unknown_reservation")
        if res.status == "confirmed":
            raise ReservationError("already_confirmed")
        res.status = "released"


# -- message handlers -------------------------------------------------------------------


def reserve_response(merchant_code: str, request: ReserveRequest, res: Reservation) -> dict[str, Any]:
    return {
        "type": "clawpay.reserve_response.v1",
        "correlation_id": request.correlation_id,
        "merchant_id": merchant_code,
        "order_ref": res.order_ref,
        "reservation_id": res.reservation_id,
        "currency": res.currency,
        "items": [{"sku": sku, "qty": qty} for sku, qty in sorted(res.items.items())],
        "total_minor": res.total_minor,
        "expires_at": _iso(res.expires_at),
    }


def handle_confirm(book: ReservationBook, merchant_code: str, data: Mapping[str, Any]) -> dict[str, Any]:
    correlation_id = data.get("correlation_id")
    order_ref, reservation_id = _ref(data.get("order_ref")), _ref(data.get("reservation_id"))
    if order_ref is None or reservation_id is None:
        return error(correlation_id, "bad_request")
    try:
        res = book.confirm(order_ref, reservation_id)
    except ReservationError as exc:
        return error(correlation_id, exc.code)
    return {
        "type": "clawpay.confirm_response.v1",
        "correlation_id": correlation_id,
        "merchant_id": merchant_code,
        "order_ref": order_ref,
        "reservation_id": res.reservation_id,
        "fulfillment_id": res.fulfillment_id,
        "status": "accepted",
    }


def handle_cancel(book: ReservationBook, merchant_code: str, data: Mapping[str, Any]) -> dict[str, Any]:
    correlation_id = data.get("correlation_id")
    order_ref, raw_id = _ref(data.get("order_ref")), data.get("reservation_id")
    reservation_id = _ref(raw_id) if raw_id is not None else None
    if order_ref is None or (raw_id is not None and reservation_id is None):
        return error(correlation_id, "bad_request")
    try:
        book.cancel(order_ref, reservation_id)
    except ReservationError as exc:
        return error(correlation_id, exc.code)
    return {
        "type": "clawpay.cancel_response.v1",
        "correlation_id": correlation_id,
        "merchant_id": merchant_code,
        "order_ref": order_ref,
        "status": "released",
    }
