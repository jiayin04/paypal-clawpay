"""Payment method linking endpoints per design §3.10.

POST /api/v1/payment-methods/setup        - initiate PayPal vault linking
GET  /api/v1/payment-methods/return       - unauthenticated return callback from PayPal
GET  /api/v1/payment-methods              - list active payment methods
DELETE /api/v1/payment-methods/{id}       - revoke a payment method
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse

from app.api.dependencies import ServicesDep, require_device
from app.core.config import Settings
from app.core.exceptions import ApiError, ServiceUnavailable
from app.integrations.paypal import PayPalGateway, PayPalService
from app.repositories.protocols import PaymentMethodRepository
from app.schemas.payment_methods import (
    ListPaymentMethodsResponse,
    PaymentMethodItem,
    SetupPaymentMethodResponse,
)
from app.services.context import DeviceContext
from app.services.payment_method_service import PaymentMethodService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/payment-methods", tags=["payment-methods"])


def _get_payment_method_service(services: "Any") -> PaymentMethodService:
    """Build PaymentMethodService from services container (lazily)."""
    return PaymentMethodService(
        settings=services.settings,
        clock=services.clock,
        payment_methods=services.repos.payment_methods,
        paypal=services.paypal,
    )


@router.post("/setup", status_code=201)
async def setup_payment_method(
    device: Annotated[DeviceContext, Depends(require_device)],
    services: ServicesDep,
) -> SetupPaymentMethodResponse:
    """Initiate PayPal vault linking. Returns an approve_url for the buyer to open."""
    if not services.settings.paypal_client_id:
        raise ServiceUnavailable("paypal_not_configured", "PayPal is not configured.")
    if not services.settings.public_base_url:
        raise ServiceUnavailable("public_url_not_configured", "PUBLIC_BASE_URL is not configured.")

    svc = _get_payment_method_service(services)
    request_id = f"pm-setup-{uuid.uuid4()}"
    result = await svc.setup(device, request_id=request_id)
    return SetupPaymentMethodResponse(
        payment_method_id=result["payment_method_id"],
        approve_url=result["approve_url"],
        expires_at=result["expires_at"],
    )


@router.get("/return", include_in_schema=False)
async def paypal_return(
    request: Request,
    services: ServicesDep,
    state: Annotated[str | None, Query()] = None,
    token: Annotated[str | None, Query()] = None,       # setup_token_id from PayPal
    cancelled: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Unauthenticated return page after buyer authorises (or cancels) on PayPal.

    Security headers:
    - Cache-Control: no-store
    - Referrer-Policy: no-referrer
    No user input is reflected raw in the HTML response.
    """
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
    }

    if not state:
        return HTMLResponse("<p>Invalid request.</p>", status_code=400, headers=headers)

    svc = _get_payment_method_service(services)
    is_cancelled = cancelled == "1"

    if is_cancelled or not token:
        # Buyer cancelled
        html = _render_result_page(status="cancelled", message="Payment method linking was cancelled.")
        return HTMLResponse(html, status_code=200, headers=headers)

    try:
        result = await svc.handle_return_with_token(state, token, cancelled=False)
        if result["status"] == "activated":
            email_hint = result.get("payer_email_masked") or ""
            msg = f"Your PayPal account{(' (' + email_hint + ')') if email_hint else ''} has been linked successfully."
            html = _render_result_page(status="success", message=msg)
        else:
            html = _render_result_page(status="failed", message="PayPal linking could not be completed. Please try again.")
    except ApiError as exc:
        logger.warning("PayPal return error: %s", exc.code)
        html = _render_result_page(status="failed", message="PayPal linking could not be completed.")
    except Exception as exc:
        logger.error("PayPal return unexpected error: %s", type(exc).__name__)
        html = _render_result_page(status="failed", message="An unexpected error occurred.")

    return HTMLResponse(html, status_code=200, headers=headers)


def _render_result_page(*, status: str, message: str) -> str:
    """Return a minimal, safe static HTML page with no reflected user input."""
    icon = "✓" if status == "success" else ("✗" if status == "failed" else "–")
    title = {"success": "Linked", "failed": "Failed", "cancelled": "Cancelled"}.get(status, "Done")
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>ClawPay – {title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:system-ui;text-align:center;padding:2rem;color:#333}}
h1{{font-size:1.5rem}}.icon{{font-size:3rem}}</style>
</head>
<body>
<div class="icon">{icon}</div>
<h1>{title}</h1>
<p>{message}</p>
<p><small>You may close this window.</small></p>
</body>
</html>"""


@router.get("")
async def list_payment_methods(
    device: Annotated[DeviceContext, Depends(require_device)],
    services: ServicesDep,
) -> ListPaymentMethodsResponse:
    """List active payment methods for the authenticated user."""
    svc = _get_payment_method_service(services)
    methods = await svc.list_methods(device.user_id)
    return ListPaymentMethodsResponse(
        payment_methods=[
            PaymentMethodItem(
                id=m["id"],
                status=m.get("status", "active"),
                payer_email_masked=m.get("payer_email_masked"),
                created_at=m.get("created_at"),
            )
            for m in methods
        ]
    )


@router.delete("/{payment_method_id}", status_code=200)
async def revoke_payment_method(
    payment_method_id: str,
    device: Annotated[DeviceContext, Depends(require_device)],
    services: ServicesDep,
) -> dict[str, Any]:
    """Revoke a payment method. Returns 409 if a payment is in progress."""
    svc = _get_payment_method_service(services)
    return await svc.revoke(device, payment_method_id)
