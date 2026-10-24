"""FastAPI application factory for the ClawPay middleware."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import devices, health, mcp, orders
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.repositories.errors import RepositoryError
from app.services.container import Services, build_services

logger = logging.getLogger(__name__)


async def _repository_error_handler(_: Request, exc: RepositoryError) -> JSONResponse:
    logger.error("Database error: %s", exc.code or type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"error": {"code": "service_unavailable", "message": "Service temporarily unavailable."}},
    )


def create_app(
    settings: Settings | None = None,
    *,
    services: Services | None = None,
    start_background: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    services = services or build_services(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Background reconciler (Phase 3) starts here when start_background and
        # PayPal is configured. Phase 2 has no background work.
        yield

    is_production = settings.app_env == "production"
    app = FastAPI(
        title="ClawPay Middleware",
        version=health.SERVICE_VERSION,
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.services = services
    app.state.start_background = start_background
    app.state.supabase_client = None
    app.state.readiness_cache = None  # (expires_at_monotonic, check)
    app.state.readiness_lock = asyncio.Lock()

    register_exception_handlers(app)
    app.add_exception_handler(RepositoryError, _repository_error_handler)  # type: ignore[arg-type]
    app.include_router(health.router)
    app.include_router(devices.router)
    app.include_router(orders.router)
    if settings.mcp_enabled:
        app.include_router(mcp.router)
    return app


app = create_app()
