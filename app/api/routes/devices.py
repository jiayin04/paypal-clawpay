"""Device registry endpoints (contract §6.1, §6.5; design §2.3).

Registration is admin-token only unless DEVICE_OWNER_REGISTRATION_ENABLED=true.
The raw device and LLM tokens appear only in the 201 response body.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.body import DEVICE_REST_BODY_LIMIT, read_limited_body
from app.api.dependencies import (
    ServicesDep,
    bearer_token,
    require_admin_or_user,
    require_user,
)
from app.core.exceptions import ApiError, Forbidden, RateLimited, Unauthorized
from app.core.security import client_ip
from app.schemas.devices import UUID_RE, RegisterDeviceRequest, RevokeDeviceResponse
from app.services.context import AdminContext, UserContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/devices", tags=["devices"])

_UUID = re.compile(UUID_RE)


def _validation_message(exc: ValidationError) -> str:
    fields = sorted({".".join(str(p) for p in e.get("loc", ())) or "body" for e in exc.errors()})
    return "Invalid request: " + ", ".join(fields[:5])


@router.post("/register", status_code=201)
async def register_device(request: Request, services: ServicesDep) -> JSONResponse:
    settings = services.settings
    ip = client_ip(request, settings.client_ip_header)
    limiter = services.limiters.register_failures

    caller: AdminContext | UserContext
    if services.auth.is_admin_token(bearer_token(request)):
        caller = AdminContext()
    else:
        try:
            caller = await require_admin_or_user(request, services)
        except Unauthorized:
            over = await limiter.is_over(ip)
            await limiter.record(ip)
            logger.warning("Device registration auth failed from %s", ip)
            if over is not None:
                raise RateLimited(over) from None
            raise
        if not settings.device_owner_registration_enabled:
            raise Forbidden("Device registration requires the admin token.")

    raw = await read_limited_body(request, DEVICE_REST_BODY_LIMIT)
    try:
        payload = json.loads(raw)
        body = RegisterDeviceRequest.model_validate(payload)
    except ValueError as exc:
        message = _validation_message(exc) if isinstance(exc, ValidationError) else "Invalid JSON body."
        raise ApiError(422, "validation_error", message) from None

    if isinstance(caller, AdminContext):
        if body.user_id is None:
            raise ApiError(422, "validation_error", "Invalid request: user_id")
        user_id, actor_type, actor_id = body.user_id, "admin", caller.actor_id
    else:
        if body.user_id is not None and body.user_id != caller.user_id:
            raise Forbidden("You can only register devices for yourself.")
        user_id, actor_type, actor_id = caller.user_id, "user", caller.user_id

    result = await services.devices.register(
        user_id=user_id,
        device_name=body.device_name,
        public_key_pem=body.public_key_pem,
        key_version=body.key_version,
        actor_type=actor_type,
        actor_id=actor_id,
    )
    # Tokens are in this body only; never logged, never stored raw.
    return JSONResponse(status_code=201, content=result, headers={"Cache-Control": "no-store"})


@router.get("")
async def list_devices(
    user: Annotated[UserContext, Depends(require_user)], services: ServicesDep
) -> dict[str, Any]:
    async with services.user_reader(user.jwt) as reader:
        rows = await reader.list_devices()
    allowed = ("id", "device_name", "key_algorithm", "key_version", "status", "created_at", "revoked_at", "last_seen_at")
    return {"devices": [{k: row.get(k) for k in allowed} for row in rows]}


@router.delete("/{device_id}", response_model=RevokeDeviceResponse)
async def revoke_device(
    device_id: str,
    caller: Annotated[AdminContext | UserContext, Depends(require_admin_or_user)],
    services: ServicesDep,
) -> dict[str, Any]:
    if not _UUID.match(device_id):
        raise ApiError(404, "not_found", "Device not found.")
    device_id = device_id.lower()
    if isinstance(caller, AdminContext):
        return await services.devices.revoke(
            device_id, user_id=None, actor_type="admin", actor_id=caller.actor_id
        )
    return await services.devices.revoke(
        device_id, user_id=caller.user_id, actor_type="user", actor_id=caller.user_id
    )
