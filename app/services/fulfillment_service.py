"""Merchant fulfillment handoff: reserve before payment, confirm or release after.

Payment integrity comes first:

- Before an order is first sent to PayPal, its merchant agent must hold the
  cart (``reserve``). If it refuses or does not answer, the never-sent payment
  attempt is failed and no money moves. ``send_guard`` enforces the same rule
  for the reconciler.
- After capture, ClawPay tells the merchant (``confirm``). A confirm failure
  never undoes the payment: the order stays paid with fulfillment_pending and
  the confirm is retried when the order status is read.
- When the payment fails, ClawPay releases the reservation (``cancel``).

Fulfillment state lives in ``orders.fulfillment_status`` (compare-and-set via
``set_fulfillment``), separate from the payment status machine. A2A exchanges
are logged to ``a2a_messages`` against the order.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.config import Settings
from app.repositories.ports.merchants import MerchantRepository
from app.repositories.ports.orders import OrderRecord, OrderRepository
from app.repositories.ports.payments import PaymentAttemptRecord
from app.services.merchant_service import A2ALog, MerchantFailure, MerchantService


logger = logging.getLogger(__name__)

NONE = "none"
RESERVED = "reserved"
RESERVATION_FAILED = "reservation_failed"
CONFIRMED = "confirmed"
PENDING = "fulfillment_pending"
RELEASED = "released"

# DB order statuses after which a held reservation is no longer needed.
_ENDED_UNPAID = frozenset({"PAYMENT_FAILED", "CANCELLED", "EXPIRED", "DENIED"})

# What the device is told when the merchant will not hold the cart (fixed texts).
RESERVE_FAILURE_MESSAGES: dict[str, str] = {
    "out_of_stock": "The shop is out of stock for an item in this order. No money was taken.",
    "price_changed": "The shop changed a price since the quote. No money was taken. Create the order again.",
    "unknown_sku": "The shop no longer sells an item in this order. No money was taken.",
    "merchant_unavailable": "The shop did not respond, so the order was not placed. No money was taken.",
}
DEFAULT_RESERVE_FAILURE = "The shop could not accept this order. No money was taken."


@dataclass(frozen=True)
class ReserveOutcome:
    ok: bool
    failure_code: str | None = None  # stored as payment_attempts.failure_code
    message: str | None = None


@dataclass(frozen=True)
class FulfillmentView:
    status: str
    merchant_reference: str | None


class FulfillmentService:
    def __init__(
        self,
        settings: Settings,
        orders: OrderRepository,
        merchants: MerchantRepository,
        merchant_service: MerchantService,
    ) -> None:
        self._settings = settings
        self._orders = orders
        self._merchants = merchants
        self._merchant_service = merchant_service

    @property
    def enabled(self) -> bool:
        return self._settings.merchant_fulfillment_enabled

    # -- helpers ------------------------------------------------------------------------

    async def _merchant_code(self, order: OrderRecord) -> str:
        merchant = await self._merchants.get_by_id(order.merchant_id)
        if merchant is None:
            raise MerchantFailure("merchant_unavailable", "merchant_not_found")
        return merchant.merchant_code

    async def _lines(self, order_id: str) -> list[dict[str, int | str]]:
        return [
            {"sku": i.product_reference, "qty": i.quantity, "unit_price_minor": i.unit_price_minor}
            for i in await self._orders.list_items(order_id)
        ]

    async def _set(
        self,
        order_id: str,
        expected: Sequence[str],
        status: str,
        *,
        reservation_id: str | None = None,
        fulfillment_id: str | None = None,
    ) -> str | None:
        """Compare-and-set; returns the status now stored (None if the order is gone)."""
        result = await self._orders.set_fulfillment(
            order_id=order_id, expected=tuple(expected), status=status,
            reservation_id=reservation_id, fulfillment_id=fulfillment_id,
        )
        if not result.ok:
            logger.info("Fulfillment update to %s skipped: %s", status, result.reason, extra={"order_id": order_id})
        return result.data.get("fulfillment_status")

    async def _reserve_with(self, code: str, order: OrderRecord, log: A2ALog) -> str:
        reservation = await self._merchant_service.reserve(
            code, order.id, await self._lines(order.id), order.currency, order.total_minor, log
        )
        return reservation.reservation_id

    # -- before payment -------------------------------------------------------------------

    async def reserve(self, order_id: str, user_id: str) -> ReserveOutcome:
        """Hold the order's cart with its merchant agent. Idempotent for an already held order."""
        if not self.enabled:
            return ReserveOutcome(True)
        order = await self._orders.get_for_user(order_id, user_id)
        if order is None:
            return ReserveOutcome(False, "merchant_reservation_failed", DEFAULT_RESERVE_FAILURE)
        if order.fulfillment_status in (RESERVED, CONFIRMED):
            return ReserveOutcome(True)
        log = A2ALog(user_id=user_id)
        code = None
        try:
            code = await self._merchant_code(order)
            reservation_id = await self._reserve_with(code, order, log)
        except MerchantFailure as failure:
            await self._set(order_id, (order.fulfillment_status,), RESERVATION_FAILED)
            if code is not None and failure.code in ("merchant_unavailable", "fulfillment_invalid"):
                # The merchant may hold stock we could not read about: release it.
                try:
                    await self._merchant_service.cancel(code, order_id, None, log)
                except MerchantFailure:
                    pass
            message = RESERVE_FAILURE_MESSAGES.get(failure.code, DEFAULT_RESERVE_FAILURE)
            prefixed = failure.code if failure.code.startswith("merchant_") else f"merchant_{failure.code}"
            return ReserveOutcome(False, prefixed[:40], message)
        finally:
            await self._merchant_service.write_log(log, order_id)
        await self._set(order_id, (order.fulfillment_status,), RESERVED, reservation_id=reservation_id)
        return ReserveOutcome(True)

    async def send_guard(self, attempt: PaymentAttemptRecord) -> str | None:
        """PaymentService hook: refuse a first PayPal send unless the merchant holds the cart."""
        if not self.enabled:
            return None
        order = await self._orders.get_for_user(attempt.order_id, attempt.user_id)
        if order is None or order.fulfillment_status in (RESERVED, CONFIRMED):
            return None
        return "merchant_not_reserved"

    # -- after payment ----------------------------------------------------------------------

    async def _confirm(self, order: OrderRecord) -> str:
        log = A2ALog(user_id=order.user_id)
        reservation_id = order.merchant_reservation_id
        try:
            code = await self._merchant_code(order)
            fulfillment = None
            if reservation_id:
                try:
                    fulfillment = await self._merchant_service.confirm(code, order.id, reservation_id, log)
                except MerchantFailure as failure:
                    if failure.code != "reservation_lost":
                        raise
            if fulfillment is None:
                # Never held, or the hold expired / was forgotten: hold again, then confirm.
                reservation_id = await self._reserve_with(code, order, log)
                fulfillment = await self._merchant_service.confirm(code, order.id, reservation_id, log)
        except MerchantFailure as failure:
            logger.warning("Merchant confirm failed: %s/%s", failure.code, failure.reason,
                           extra={"order_id": order.id})
            if order.fulfillment_status == PENDING:
                return PENDING
            return await self._set(order.id, (order.fulfillment_status,), PENDING,
                                   reservation_id=reservation_id) or PENDING
        finally:
            await self._merchant_service.write_log(log, order.id)
        stored = await self._set(order.id, (order.fulfillment_status,), CONFIRMED,
                                 reservation_id=reservation_id, fulfillment_id=fulfillment.fulfillment_id)
        return stored or CONFIRMED

    async def _release(self, order: OrderRecord) -> str:
        log = A2ALog(user_id=order.user_id)
        try:
            code = await self._merchant_code(order)
            await self._merchant_service.cancel(code, order.id, order.merchant_reservation_id, log)
        except MerchantFailure as failure:
            logger.warning("Merchant cancel failed: %s/%s", failure.code, failure.reason,
                           extra={"order_id": order.id})
            return order.fulfillment_status  # retried on a later status read
        finally:
            await self._merchant_service.write_log(log, order.id)
        return await self._set(order.id, (order.fulfillment_status,), RELEASED) or RELEASED

    async def after_payment(self, order_id: str, user_id: str, payment_status: str) -> FulfillmentView | None:
        """Inline follow-up once the payment result is known (completed / failed)."""
        if not self.enabled or payment_status not in ("completed", "failed"):
            return None
        order = await self._orders.get_for_user(order_id, user_id)
        if order is None:
            return None
        if payment_status == "completed" and order.fulfillment_status in (RESERVED, PENDING):
            status = await self._confirm(order)
        elif payment_status == "failed" and order.fulfillment_status == RESERVED:
            status = await self._release(order)
        else:
            status = order.fulfillment_status
        return await self.view(order_id, user_id, fallback=status)

    async def resolve(self, order: OrderRecord, db_status: str) -> FulfillmentView:
        """Finish a handoff the approval request could not (called on status reads)."""
        status = order.fulfillment_status
        if self.enabled:
            if db_status == "PAID" and status in (RESERVED, PENDING):
                status = await self._confirm(order)
            elif db_status in _ENDED_UNPAID and status == RESERVED:
                status = await self._release(order)
            else:
                return FulfillmentView(status, order.merchant_fulfillment_id)
            return await self.view(order.id, order.user_id, fallback=status)
        return FulfillmentView(status, order.merchant_fulfillment_id)

    async def view(self, order_id: str, user_id: str, *, fallback: str) -> FulfillmentView:
        order = await self._orders.get_for_user(order_id, user_id)
        if order is None:
            return FulfillmentView(fallback, None)
        return FulfillmentView(order.fulfillment_status, order.merchant_fulfillment_id)
