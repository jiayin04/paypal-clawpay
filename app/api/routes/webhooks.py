from fastapi import APIRouter, Request

from app.api.body import read_limited_body
from app.api.dependencies import ServicesDep
from app.core.security import client_ip

router = APIRouter(tags=['webhooks'])

@router.post('/api/v1/webhooks/paypal')
async def paypal_webhook(request: Request, services: ServicesDep):
    ip = client_ip(request,services.settings.client_ip_header)
    # Throttling precedes reading an attacker-controlled request body.
    from app.core.exceptions import RateLimited
    retry = await services.webhooks.failures.is_over(ip)
    if retry is not None:
        raise RateLimited(retry)
    if not services.settings.paypal_configured or not services.settings.paypal_webhook_id:
        from app.core.exceptions import ServiceUnavailable
        raise ServiceUnavailable("PayPal webhooks are not configured.", "webhooks_not_configured")
    body = await read_limited_body(request,256*1024)
    return await services.webhooks.receive(request.headers,body,ip)
