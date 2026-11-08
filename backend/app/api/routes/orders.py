"""Dashboard order endpoints (design §2.7). Reads run with the caller's JWT (RLS)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import ServicesDep, require_user, require_user_service_only
from app.core import observability
from app.core.exceptions import ApiError
from app.domain.order_state import to_api_status
from app.schemas.devices import UUID_RE
from app.services.context import UserContext


router = APIRouter(prefix="/api/v1/orders", tags=["orders"])

_UUID = re.compile(UUID_RE)


def _not_found() -> ApiError:
    return ApiError(404, "order_not_found", "Order not found.")


def _order_id(value: str) -> str:
    if not _UUID.match(value):
        raise _not_found()
    order_id = value.lower()
    observability.bind(order_id=order_id)
    return order_id


def _with_api_status(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if isinstance(out.get("status"), str):
        out["status"] = to_api_status(out["status"])
    return out


@router.get("")
async def list_orders(
    user: Annotated[UserContext, Depends(require_user)],
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: Annotated[datetime | None, Query()] = None,
) -> dict[str, Any]:
    async with services.user_reader(user.jwt) as reader:
        rows = await reader.list_orders(limit=limit, before=before)
    return {"orders": [_with_api_status(r) for r in rows]}


@router.get("/{order_id}")
async def get_order(
    order_id: str,
    user: Annotated[UserContext, Depends(require_user)],
    services: ServicesDep,
) -> dict[str, Any]:
    order_id = _order_id(order_id)
    async with services.user_reader(user.jwt) as reader:
        order = await reader.get_order_detail(order_id)
    if order is None:
        raise _not_found()
    if order["status"] in ("PAYMENT_PENDING", "REQUIRES_RECONCILIATION"):
        await services.payments.reconcile_order(order_id)
        async with services.user_reader(user.jwt) as reader:
            order = await reader.get_order_detail(order_id)
    return _with_api_status(order)


@router.post("/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    user: Annotated[UserContext, Depends(require_user_service_only)],
    services: ServicesDep,
) -> dict[str, Any]:
    return await services.orders.cancel(_order_id(order_id), user.user_id)


@router.get('/{order_id}/timeline')
async def order_timeline(order_id: str, user: Annotated[UserContext, Depends(require_user)], services: ServicesDep,
                         limit: Annotated[int,Query(ge=1,le=100)] = 20, before: datetime | None = None):
    order_id = _order_id(order_id)
    async with services.user_reader(user.jwt) as reader:
        if not await reader.get_order_detail(order_id):
            raise _not_found()
        return {'events':await reader.timeline(order_id,limit=limit,before=before)}

@router.get('/{order_id}/a2a-messages')
async def order_a2a(order_id: str, user: Annotated[UserContext, Depends(require_user)], services: ServicesDep,
                    limit: Annotated[int,Query(ge=1,le=100)] = 20, before: datetime | None = None):
    order_id = _order_id(order_id)
    async with services.user_reader(user.jwt) as reader:
        if not await reader.get_order_detail(order_id):
            raise _not_found()
        return {'messages':await reader.a2a_messages(order_id,limit=limit,before=before)}
