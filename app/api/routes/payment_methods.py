"""JWT-owned payment methods and the unauthenticated PayPal return page."""
from html import escape
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import ServicesDep, require_user
from app.core.exceptions import ApiError, RateLimited
from app.core.security import client_ip
from app.services.context import UserContext


router = APIRouter(prefix="/api/v1/payment-methods", tags=["payment-methods"])

@router.post("/paypal/setup", status_code=201)
async def setup_payment_method(
    user: Annotated[UserContext, Depends(require_user)],
    services: ServicesDep,
):
    return await services.payment_methods.setup(user, request_id=f"setup-{uuid4()}")

@router.get("/paypal/return", include_in_schema=False)
async def paypal_return(
    request: Request,
    services: ServicesDep,
    state: str = "",
    cancelled: str = "",
):
    svc = services.payment_methods
    ip = client_ip(request, services.settings.client_ip_header)
    retry = await svc.return_limiter.is_over(ip)
    if retry is not None:
        raise RateLimited(retry)
    status_code = 200
    try:
        result = await svc.handle_return(state, cancelled=cancelled == "1")
        masked = escape(result.get("payer_email_masked") or "")
        message = f"PayPal {masked} linked. You can close this tab." if result['status'] == 'activated' else "Linking failed. Start again from the dashboard."
    except ApiError as exc:
        if exc.status_code == 503:
            raise
        if exc.code == "invalid_state":
            await svc.return_limiter.record(ip)
        status_code = exc.status_code
        message = "Linking failed. Start again from the dashboard."
    except Exception:
        status_code = 502
        message = "Linking failed. Start again from the dashboard."
    return HTMLResponse("<!doctype html><html><head><title>ClawPay</title></head><body><p>"+message+"</p></body></html>", status_code=status_code,
                        headers={"Cache-Control":"no-store", "Referrer-Policy":"no-referrer"})

@router.get("")
async def list_payment_methods(
    user: Annotated[UserContext, Depends(require_user)],
    services: ServicesDep,
):
    async with services.user_reader(user.jwt) as reader:
        return {"payment_methods": await reader.list_payment_methods()}

@router.delete("/{payment_method_id}")
async def revoke_payment_method(
    payment_method_id: str,
    user: Annotated[UserContext, Depends(require_user)],
    services: ServicesDep,
):
    from uuid import UUID
    try:
        payment_method_id = str(UUID(payment_method_id))
    except ValueError:
        raise ApiError(404,"not_found","Payment method not found.") from None
    return await services.payment_methods.revoke(user, payment_method_id)
