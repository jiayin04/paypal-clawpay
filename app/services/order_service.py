"""create_order, get_order_status, cancel and lazy expiry (design §2.2, §2.6, §2.7).

Totals always come from the validated merchant quote, recomputed here and
again inside the create RPC. Orders above PHONE_APPROVAL_THRESHOLD_MINOR are
refused because phone approval does not exist yet.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from starlette.background import BackgroundTasks

from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import ApiError, Conflict
from app.domain.canonical import cart_sha256
from app.domain.money import format_display
from app.domain.order_state import to_api_status
from app.repositories.errors import RepositoryError, call_rpc_with_retry
from app.repositories.protocols import (
    MerchantRepository,
    OrderRecord,
    OrderRepository,
    PaymentMethodRepository,
)
from app.services.context import DeviceContext
from app.services.errors import ToolError
from app.services.merchant_service import A2ALog, MerchantFailure, MerchantService
from app.services.spending_service import SpendingService

logger = logging.getLogger(__name__)

MAX_LINE_QTY = 20
EXPIRABLE = frozenset({"CREATED", "QUOTED", "AWAITING_APPROVAL"})

STATUS_MESSAGES: dict[str, str] = {
    "created": "The order is being prepared.",
    "quoted": "The order is being prepared.",
    "awaiting_device_approval": "Waiting for approval on the device.",
    "approved": "Approved. Payment is starting.",
    "payment_pending": "Payment is still processing. Check again shortly.",
    "requires_reconciliation": "Payment is still processing. Check again shortly.",
    "paid": "Payment completed.",
    "payment_failed": (
        "Payment failed. No money was taken. If PayPal later confirms the payment, "
        "this status changes to paid."
    ),
    "denied": "The order was declined on the device.",
    "expired": "The order expired before it was approved.",
    "cancelled": "The order was cancelled.",
}


def iso_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def merge_items(items: Sequence[Any]) -> list[dict[str, Any]]:
    """Merge duplicate skus by summing qty; merged qty > 20 is invalid."""
    merged: dict[str, int] = {}
    for item in items:
        merged[item.sku] = merged.get(item.sku, 0) + item.qty
    for sku, qty in merged.items():
        if qty > MAX_LINE_QTY:
            raise ToolError("invalid_arguments", f"items: total qty for {sku[:64]} exceeds {MAX_LINE_QTY}")
    return [{"sku": sku, "qty": qty} for sku, qty in merged.items()]


class OrderService:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        orders: OrderRepository,
        merchants: MerchantRepository,
        payment_methods: PaymentMethodRepository,
        spending: SpendingService,
        merchant_service: MerchantService,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._orders = orders
        self._merchants = merchants
        self._payment_methods = payment_methods
        self._spending = spending
        self._merchant_service = merchant_service

    # -- create_order ----------------------------------------------------------------

    async def create_order(
        self,
        device: DeviceContext,
        *,
        merchant_id: str,
        items: Sequence[Any],
        currency: str | None,
        background: BackgroundTasks,
    ) -> dict[str, Any]:
        settings = self._settings
        lines = merge_items(items)
        currency = currency or settings.default_currency

        if currency != settings.default_currency:
            raise ToolError("currency_not_supported")

        merchant = await self._merchants.get_active_by_code(merchant_id)
        if merchant is None:
            raise ToolError("merchant_unavailable")

        if settings.order_require_payment_method and not await self._payment_methods.has_active(
            device.user_id
        ):
            raise ToolError("no_payment_method")

        log = A2ALog(user_id=device.user_id)
        try:
            quote = await self._merchant_service.request_quote(merchant_id, lines, currency, log)
        except MerchantFailure as failure:
            background.add_task(self._merchant_service.write_log, log, None)
            if failure.code == "unknown_sku":
                sku = failure.sku
                raise ToolError(
                    "unknown_sku",
                    f"The shop does not sell {sku}. Search again for the right item."
                    if sku
                    else None,
                ) from None
            raise ToolError(failure.code) from None

        try:
            total = quote.total_minor
            if total > settings.phone_approval_threshold_minor:
                raise ToolError("phone_approval_unavailable")

            summary = await self._spending.summary(device.user_id)
            used = summary.committed_minor + summary.reserved_minor
            if used + total > settings.daily_spending_limit_minor:
                remaining = self._spending.remaining_minor(summary)
                raise ToolError(
                    "daily_limit_exceeded",
                    "This order would go over today's spending limit. "
                    f"Remaining today: {format_display(remaining, currency)}.",
                )

            rpc_items = [
                {
                    "sku": line.sku,
                    "name": line.name,
                    "qty": line.qty,
                    "unit_price_minor": line.unit_price_minor,
                }
                for line in quote.lines
            ]
            cart = cart_sha256(rpc_items)
            result = await call_rpc_with_retry(
                "create_order_from_quote",
                lambda: self._orders.create_from_quote(
                    user_id=device.user_id,
                    device_id=device.device_id,
                    merchant_code=merchant_id,
                    currency=currency,
                    quote_expires_at=quote.expires_at,
                    quote_reference=quote.quote_id,
                    cart_sha256=cart,
                    items=rpc_items,
                    total_minor=total,
                ),
            )
        except ToolError:
            background.add_task(self._merchant_service.write_log, log, None)
            raise
        except (RepositoryError, ApiError) as exc:
            logger.error("create_order DB failure: %s", type(exc).__name__)
            background.add_task(self._merchant_service.write_log, log, None)
            raise ToolError("internal_error") from None

        if not result.ok:
            background.add_task(self._merchant_service.write_log, log, None)
            if result.reason == "merchant_unavailable":
                raise ToolError("merchant_unavailable")
            # device_revoked: the next request fails auth with 401 anyway.
            logger.warning("create_order refused by RPC: %s", result.reason)
            raise ToolError("internal_error")

        order_id = str(result.data["order_id"])
        background.add_task(self._merchant_service.write_log, log, order_id)

        expires_at = quote.expires_at
        out_total = total
        out_lines = quote.lines
        if result.data.get("deduplicated"):
            existing = await self._orders.get_for_user(order_id, device.user_id)
            if existing is not None and existing.quote_expires_at is not None:
                expires_at = existing.quote_expires_at
                out_total = existing.total_minor

        return {
            "order_id": order_id,
            "merchant": merchant.display_name,
            "items": [
                {"name": line.name, "qty": line.qty, "line_total": format_display(line.line_total_minor, currency)}
                for line in out_lines
            ],
            "total_minor": out_total,
            "total": format_display(out_total, currency),
            "currency": currency,
            "expires_at": iso_z(expires_at),
            "status": "awaiting_device_approval",
            "next_step": "call clawpay_request_approval",
        }

    # -- get_order_status --------------------------------------------------------------

    async def _expire_if_needed(self, order: OrderRecord) -> str:
        status = order.status
        if (
            status in EXPIRABLE
            and order.quote_expires_at is not None
            and order.quote_expires_at <= self._clock.utc_now()
        ):
            result = await call_rpc_with_retry("expire_order", lambda: self._orders.expire(order.id))
            if result.ok:
                return "EXPIRED"
            refreshed = result.data.get("status")
            if isinstance(refreshed, str) and refreshed:
                return refreshed
        return status

    async def get_order_status(self, device: DeviceContext, order_id: str) -> dict[str, Any]:
        try:
            order = await self._orders.get_for_user(order_id, device.user_id)
            if order is None:
                raise ToolError("not_found")
            status = await self._expire_if_needed(order)
        except (RepositoryError, ApiError) as exc:
            logger.error("get_order_status DB failure: %s", type(exc).__name__)
            raise ToolError("internal_error") from None
        api_status = to_api_status(status)
        return {
            "order_id": order.id,
            "status": api_status,
            # Payment attempts arrive in Phase 3; until then there is no provider status.
            "paypal_status": None,
            "total": format_display(order.total_minor, order.currency),
            "currency": order.currency,
            "approved_at": None,
            "paid_at": None,
            "message": STATUS_MESSAGES.get(api_status, ""),
        }

    # -- cancel (dashboard) ----------------------------------------------------------------

    async def cancel(self, order_id: str, user_id: str) -> dict[str, Any]:
        result = await call_rpc_with_retry(
            "cancel_order",
            lambda: self._orders.cancel(
                order_id=order_id, user_id=user_id, actor_type="user", actor_id=user_id
            ),
        )
        if result.ok:
            return {"order_id": order_id, "status": "cancelled"}
        if result.reason == "invalid_order_status":
            raise Conflict("invalid_order_status", "The order can no longer be cancelled.")
        raise ApiError(404, "order_not_found", "Order not found.")
