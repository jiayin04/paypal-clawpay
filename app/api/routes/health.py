"""Liveness and readiness endpoints. Responses never include URLs, keys, or error text."""

from __future__ import annotations

import logging
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.dependencies import get_app_settings, get_supabase_client
from app.core.config import Settings
from app.integrations.supabase import check_supabase


logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

SERVICE_NAME = "clawpay-middleware"
SERVICE_VERSION = "0.1.0"

SupabaseCheck = Literal["ok", "not_configured", "error"]

# /health/ready is unauthenticated; cache the Supabase probe result briefly.
READINESS_CACHE_SECONDS = 5.0


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    environment: str


class ReadinessChecks(BaseModel):
    supabase: SupabaseCheck


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks


async def _probe_supabase(request: Request) -> SupabaseCheck:
    try:
        client = get_supabase_client(request)
        if client is None:
            return "not_configured"
        await run_in_threadpool(check_supabase, client)
        return "ok"
    except Exception as exc:
        # Type name only: exception text may contain URLs or keys.
        logger.warning("Supabase readiness check failed: %s", type(exc).__name__)
        return "error"


async def _cached_supabase_check(request: Request) -> SupabaseCheck:
    """Probe at most once per READINESS_CACHE_SECONDS so the public endpoint
    cannot be used to drive database load."""
    state = request.app.state
    if not state.settings.supabase_configured:
        return "not_configured"
    async with state.readiness_lock:
        cached = state.readiness_cache
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        check = await _probe_supabase(request)
        state.readiness_cache = (time.monotonic() + READINESS_CACHE_SECONDS, check)
        return check


@router.get("/health", response_model=HealthResponse)
async def health(settings: Annotated[Settings, Depends(get_app_settings)]) -> HealthResponse:
    return HealthResponse(
        status="ok", service=SERVICE_NAME, version=SERVICE_VERSION, environment=settings.app_env
    )


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def readiness(request: Request) -> JSONResponse:
    check = await _cached_supabase_check(request)
    ready = check == "ok"
    body = ReadinessResponse(
        status="ready" if ready else "not_ready", checks=ReadinessChecks(supabase=check)
    )
    return JSONResponse(status_code=200 if ready else 503, content=body.model_dump())
