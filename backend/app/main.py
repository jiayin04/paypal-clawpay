"""FastAPI application factory for the ClawPay middleware."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import (
    approvals,
    assistant,
    audit,
    conversations,
    devices,
    health,
    llm_proxy,
    mcp,
    metrics,
    orders,
    passports,
    payment_methods,
    spending,
    webhooks,
)
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.observability import RequestContextMiddleware
from app.repositories.errors import RepositoryError
from app.services.container import Services, build_services


logger = logging.getLogger(__name__)


async def _repository_error_handler(_: Request, exc: RepositoryError) -> JSONResponse:
    logger.error("Database error: %s", exc.code or type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"error": {"code": "service_unavailable", "message": "Service temporarily unavailable."}},
    )


def _add_bearer_to_openapi(app: FastAPI) -> None:
    """Swagger "Authorize" button. Auth is read from the Authorization header by
    the route dependencies (JWT, cpd_ device token or admin token); this only
    documents it so /docs can send it."""
    from fastapi.openapi.utils import get_openapi

    def openapi() -> dict:
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schema.setdefault("components", {})["securitySchemes"] = {
                "bearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Supabase user JWT, device token (cpd_...) or INTERNAL_SERVICE_TOKEN, "
                    "depending on the endpoint.",
                }
            }
            schema["security"] = [{"bearer": []}]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


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
        # Background reconciler (Phase 3): starts when start_background=True and PayPal configured.
        task: asyncio.Task | None = None
        if start_background and settings.paypal_configured:

            async def _run_reconciler() -> None:
                await services.reconciliation.run_forever(interval_seconds=settings.reconcile_interval_seconds)

            loop = asyncio.get_event_loop()
            task = loop.create_task(_run_reconciler())
            logger.info("Background reconciler started")
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                logger.info("Background reconciler stopped")
            await services.llm.client.aclose()
            await services.tts.client.aclose()
            await services.stt.client.aclose()

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
    _add_bearer_to_openapi(app)
    app.add_exception_handler(RepositoryError, _repository_error_handler)  # type: ignore[arg-type]
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(devices.router)
    app.include_router(orders.router)
    app.include_router(approvals.router)
    app.include_router(payment_methods.router)
    app.include_router(webhooks.router)
    app.include_router(llm_proxy.router)
    app.include_router(conversations.router)
    app.include_router(spending.router)
    app.include_router(audit.router)
    app.include_router(passports.router)
    if settings.mcp_enabled:
        app.include_router(mcp.router)
        app.include_router(assistant.router)
    return app


app = create_app()
