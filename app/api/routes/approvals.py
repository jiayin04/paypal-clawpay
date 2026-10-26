"""Approval challenge and gadget-approval endpoints (design §3.2, §3.3, §3.4)."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from app.api.body import read_limited_body
from app.api.dependencies import ServicesDep, require_device
from app.core.exceptions import ApiError
from app.schemas.approvals import (
    ApprovalChallengeResponse,
    GadgetApprovalRequest,
    GadgetApprovalResponse,
)
from app.schemas.devices import UUID_RE
from app.services.context import DeviceContext


router = APIRouter(prefix="/api/v1/orders", tags=["approvals"])

_UUID = re.compile(UUID_RE)


def _order_id(value: str) -> str:
    if not _UUID.match(value):
        raise ApiError(404, "order_not_found", "Order not found.")
    return value.lower()


def _device_id(value: str) -> str:
    if not _UUID.match(value):
        raise ApiError(404, "not_found", "Device not found.")
    return value.lower()


@router.get("/{order_id}/approval-challenge", response_model=ApprovalChallengeResponse)
async def get_approval_challenge(
    order_id: str,
    device: Annotated[DeviceContext, Depends(require_device)],
    services: ServicesDep,
) -> Any:
    """§3.2: Issue (or return existing open) approval challenge for the order."""
    order_id = _order_id(order_id)

    # Fetch the order to get merchant info needed to build the canonical payload.
    order = await services.repos.orders.get_for_user(order_id, device.user_id)
    if order is None:
        raise ApiError(404, "order_not_found", "Order not found.")

    # Merchant display name and code for the canonical payload.
    merchant = await services.repos.merchants.get_by_id(order.merchant_id)
    merchant_code = merchant.merchant_code if merchant else order.merchant_id
    merchant_name = merchant.display_name if merchant else merchant_code

    merchant_info = {
        "total_minor": order.total_minor,
        "cart_sha256": order.cart_sha256 or "",
        "currency": order.currency,
        "merchant_code": merchant_code,
        "merchant_name": merchant_name,
        "quote_expires_at": order.quote_expires_at,
    }

    result = await services.approvals.issue_challenge(device, order_id, merchant_info)
    return result


@router.post("/{order_id}/gadget-approval", response_model=GadgetApprovalResponse)
async def submit_gadget_approval(
    order_id: str,
    request: Request,
    device: Annotated[DeviceContext, Depends(require_device)],
    services: ServicesDep,
) -> Any:
    """§3.3: Verify gadget signature and record approval/denial."""
    order_id = _order_id(order_id)
    raw = await read_limited_body(request,16*1024)
    try:
        body = GadgetApprovalRequest.model_validate_json(raw)
    except ValidationError:
        raise ApiError(422,"validation_error","Invalid approval request.") from None
    return await services.approvals.submit_gadget_approval(
        device, order_id, body.model_dump()
    )
