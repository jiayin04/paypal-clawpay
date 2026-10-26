import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.body import read_limited_body
from app.api.dependencies import ServicesDep, require_device_llm
from app.core.exceptions import ApiError
from app.services.context import DeviceContext


router = APIRouter(tags=['llm-proxy'])

@router.post('/llm/v1/chat/completions')
async def completions(
    request: Request,
    services: ServicesDep,
    device: Annotated[DeviceContext,Depends(require_device_llm)],
):
    raw = await read_limited_body(request,services.settings.llm_proxy_max_body_bytes)
    try:
        body = json.loads(raw)
    except (ValueError,UnicodeError):
        raise ApiError(400,'invalid_request_error','Invalid chat completion request.') from None
    return await services.llm.complete(device.device_id,body)
