"""Request correlation: request ids, log context and the HTTP metrics middleware.

Every request gets a request id (a caller-supplied ``X-Request-ID`` is reused
only when it matches a strict pattern). The id, and the device / user / order
ids once known, live in contextvars, so every log line written while handling
the request (background tasks included) carries them, and outbound merchant
agent calls forward the id. Nothing here logs bodies or headers.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.metrics import METRICS

REQUEST_ID_HEADER = "X-Request-ID"
_INCOMING_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_NO_ECHO_PREFIXES = ("/llm/",)
CONTEXT_FIELDS = ("request_id", "device_id", "user_id", "order_id")

_context: dict[str, contextvars.ContextVar[str | None]] = {
    name: contextvars.ContextVar(f"clawpay_{name}", default=None) for name in CONTEXT_FIELDS
}

logger = logging.getLogger("app.access")


def new_request_id() -> str:
    return uuid.uuid4().hex


def current(name: str) -> str | None:
    return _context[name].get()


def current_request_id() -> str | None:
    return _context["request_id"].get()


def bind(**values: str | None) -> None:
    """Attach ids to the current request context (unknown names are ignored)."""
    for name, value in values.items():
        var = _context.get(name)
        if var is not None and value is not None:
            var.set(str(value))


@contextmanager
def bound(**values: str | None) -> Iterator[None]:
    """Bind ids for a block of work outside a request (e.g. one reconciler tick)."""
    tokens = [(_context[n], _context[n].set(str(v))) for n, v in values.items() if n in _context and v is not None]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def outbound_headers() -> dict[str, str]:
    request_id = current_request_id()
    return {REQUEST_ID_HEADER: request_id} if request_id else {}


class ContextFilter(logging.Filter):
    """Copies the request context onto each record (``-`` when unset)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name in CONTEXT_FIELDS:
            if not hasattr(record, name):
                setattr(record, name, current(name) or "-")
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. ``message`` is the already-redacted text."""

    _EXTRA_KEYS = ("event", "method", "route", "status", "duration_ms")

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in CONTEXT_FIELDS:
            value = getattr(record, name, None)
            if value and value != "-":
                out[name] = value
        for key in self._EXTRA_KEYS:
            if hasattr(record, key):
                out[key] = getattr(record, key)
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class RequestContextMiddleware:
    """Pure ASGI middleware (keeps SSE streaming intact).

    Sets the request id, echoes it as ``X-Request-ID``, counts requests and
    latency by route template (never the raw path, which can hold a token),
    and writes one access log line.
    """

    def __init__(self, app: ASGIApp, *, record_metrics: bool = True) -> None:
        self.app = app
        self.record_metrics = record_metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = None
        for key, value in scope.get("headers", []):
            if key == b"x-request-id":
                incoming = value.decode("latin-1")
                break
        request_id = incoming if incoming and _INCOMING_ID_RE.match(incoming) else new_request_id()
        # Fresh values for this request. They are deliberately not reset on exit:
        # each request runs in its own task context, and keeping them lets the
        # outer error handler log an unhandled exception with the request id.
        for var in _context.values():
            var.set(None)
        _context["request_id"].set(request_id)
        status = 500
        started = time.perf_counter()
        # The LLM proxy contract allows only Content-Type and Cache-Control.
        echo_header = not scope.get("path", "").startswith(_NO_ECHO_PREFIXES)

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                if echo_header:
                    headers = list(message.get("headers", []))
                    headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            route = _route_template(scope)
            method = scope.get("method", "-")
            if self.record_metrics:
                METRICS.http_requests.inc(method, route, str(status))
                METRICS.http_duration.observe(elapsed, method, route)
            logger.info(
                "%s %s %s %dms", method, route, status, int(elapsed * 1000),
                extra={"event": "http_request", "method": method, "route": route, "status": status,
                       "duration_ms": int(elapsed * 1000)},
            )
