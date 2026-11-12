"""Agent passports (lite): owner-issued, signed, revocable grants for agent devices.

Owner routes use the JWT ``sub`` with the service client (like cancel / revoke),
so they work without the publishable key. The issuer public key is public.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.api.dependencies import ServicesDep, require_user_service_only
from app.core.exceptions import ApiError
from app.schemas.devices import UUID_RE, UuidStr
from app.services.context import UserContext


router = APIRouter(tags=["agent-passports"])

_UUID = re.compile(UUID_RE)

MerchantCode = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,40}$")]


class CreateDelegationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    device_id: UuidStr
    merchants: Annotated[list[MerchantCode], Field(min_length=1, max_length=10)]
    max_order_minor: Annotated[int, Field(ge=1, le=10_000_000)]
    expires_in_hours: Annotated[int, Field(ge=1, le=720)] = 168


@router.get("/.well-known/agent-passport")
async def issuer_keys(services: ServicesDep) -> dict[str, Any]:
    """Public Ed25519 key(s) for verifying grant and receipt signatures."""
    return services.passports.public_keys()


@router.post("/api/v1/delegations", status_code=201)
async def create_delegation(
    body: CreateDelegationRequest,
    user: Annotated[UserContext, Depends(require_user_service_only)],
    services: ServicesDep,
) -> dict[str, Any]:
    return await services.passports.create(
        user_id=user.user_id, device_id=body.device_id, merchants=body.merchants,
        max_order_minor=body.max_order_minor, expires_in_hours=body.expires_in_hours,
    )


@router.get("/api/v1/delegations")
async def list_delegations(
    user: Annotated[UserContext, Depends(require_user_service_only)], services: ServicesDep
) -> dict[str, Any]:
    return {"delegations": await services.passports.list(user.user_id)}


@router.get("/api/v1/delegations/receipts")
async def list_receipts(
    user: Annotated[UserContext, Depends(require_user_service_only)],
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    return {"receipts": await services.passports.receipts(user.user_id, limit=limit)}


@router.delete("/api/v1/delegations/{delegation_id}")
async def revoke_delegation(
    delegation_id: str,
    user: Annotated[UserContext, Depends(require_user_service_only)],
    services: ServicesDep,
) -> dict[str, Any]:
    if not _UUID.match(delegation_id):
        raise ApiError(404, "not_found", "Passport not found.")
    return await services.passports.revoke(delegation_id=delegation_id.lower(), user_id=user.user_id)
