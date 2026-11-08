"""Prometheus scrape endpoint. Disabled (404) unless METRICS_TOKEN is set.

Scrapers send ``Authorization: Bearer <METRICS_TOKEN>``. Labels hold route
templates and reason codes only, so the body contains no ids or secrets, but
it still describes traffic, so keep it off public ingress where possible.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from app.api.dependencies import ServicesDep, bearer_token
from app.core.exceptions import NotFoundError, Unauthorized
from app.core.metrics import METRICS
from app.core.security import constant_time_equals


router = APIRouter(tags=["observability"])

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, services: ServicesDep) -> PlainTextResponse:
    expected = services.settings.metrics_token
    if expected is None:
        raise NotFoundError("Not found.")
    token = bearer_token(request)
    if token is None or not constant_time_equals(token, expected.get_secret_value()):
        raise Unauthorized()
    return PlainTextResponse(METRICS.render(), media_type=CONTENT_TYPE, headers={"Cache-Control": "no-store"})
