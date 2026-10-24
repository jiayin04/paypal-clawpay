"""Bounded request-body reader."""

from __future__ import annotations

from starlette.requests import Request

from app.core.exceptions import PayloadTooLarge

MCP_BODY_LIMIT = 64 * 1024
DEVICE_REST_BODY_LIMIT = 16 * 1024
WEBHOOK_BODY_LIMIT = 256 * 1024
REST_BODY_LIMIT = 32 * 1024


async def read_limited_body(request: Request, limit: int) -> bytes:
    """Read the body, rejecting anything over ``limit`` bytes with 413.

    ``Content-Length`` over the limit is rejected before reading; otherwise the
    stream is consumed and aborted at ``limit + 1`` bytes.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise PayloadTooLarge()
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)
