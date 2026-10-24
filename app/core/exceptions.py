"""Application error types and FastAPI exception handlers."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    code: str = "app_error"
    status_code: int = 500

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ConfigurationError(AppError):
    code = "configuration_error"
    status_code = 503


class ExternalServiceError(AppError):
    code = "external_service_error"
    status_code = 502


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404


class ApiError(AppError):
    """REST error with an explicit status, code and optional response headers."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.status_code = status_code
        self.headers = headers or {}


class Unauthorized(ApiError):
    def __init__(self, message: str = "Authentication required.", code: str = "unauthorized") -> None:
        super().__init__(401, code, message, {"WWW-Authenticate": "Bearer"})


class Forbidden(ApiError):
    def __init__(self, message: str = "Forbidden.", code: str = "forbidden") -> None:
        super().__init__(403, code, message)


class Conflict(ApiError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


class Gone(ApiError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(410, code, message)


class RateLimited(ApiError):
    def __init__(self, retry_after: int, message: str = "Too many requests.") -> None:
        super().__init__(429, "rate_limited", message, {"Retry-After": str(max(1, retry_after))})
        self.retry_after = max(1, retry_after)


class PayloadTooLarge(ApiError):
    def __init__(self, message: str = "Request body too large.") -> None:
        super().__init__(413, "payload_too_large", message)


class ServiceUnavailable(ApiError):
    def __init__(
        self, message: str = "Service temporarily unavailable.", code: str = "service_unavailable"
    ) -> None:
        super().__init__(503, code, message)


def _error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    headers = getattr(exc, "headers", None) or None
    return JSONResponse(
        status_code=exc.status_code, content=_error_body(exc.code, exc.message), headers=headers
    )


async def _validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Field locations only; the rejected input is never echoed back.
    fields = sorted({".".join(str(p) for p in err.get("loc", ())[1:]) or "body" for err in exc.errors()})
    message = "Invalid request: " + ", ".join(fields[:5])
    return JSONResponse(status_code=422, content=_error_body("validation_error", message))


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Logged through the redacting filter; the exception text is never returned.
    logger.error(
        "Unhandled error on %s %s", request.method, request.url.path, exc_info=exc
    )
    return JSONResponse(
        status_code=500, content=_error_body("internal_error", "Internal server error")
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_error_handler)
