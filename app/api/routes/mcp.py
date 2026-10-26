"""MCP Streamable HTTP endpoint, stateless (contracts.md §3, design §2.1).

Mounted only when MCP_ENABLED=true. ``POST /mcp/{device_token}`` exists only
when MCP_PATH_TOKEN_ENABLED=true (otherwise 404); GET/DELETE always 405.
Request and response bodies are never logged.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.api.body import MCP_BODY_LIMIT, read_limited_body
from app.api.dependencies import authenticate_device, bearer_token, get_services
from app.core.exceptions import PayloadTooLarge, RateLimited
from app.integrations.mcp import jsonrpc
from app.integrations.mcp.server import McpServer, SUPPORTED_VERSIONS
from app.services.container import Services


router = APIRouter(tags=["mcp"])


def _json(status_code: int, body: bytes) -> Response:
    return Response(content=body, status_code=status_code, media_type="application/json")


def _method_not_allowed() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})


async def _handle(
    request: Request, token: str | None, services: Services, background: BackgroundTasks
) -> Response:
    settings = services.settings

    # 1. DNS-rebinding guard. The device sends no Origin.
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") not in settings.mcp_allowed_origins:
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "forbidden_origin", "message": "Origin not allowed."}},
        )

    # 2. Declared size before auth.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MCP_BODY_LIMIT:
        raise PayloadTooLarge()

    # 3. Device auth (401 / 429 / 503).
    device = await authenticate_device(request, token, services, background)

    # 4. Per-device request limit.
    retry = await services.limiters.mcp_requests.acquire(device.device_id)
    if retry is not None:
        raise RateLimited(retry)

    # 5. Protocol version header.
    version = request.headers.get("mcp-protocol-version") or "2025-11-25"
    if version not in SUPPORTED_VERSIONS:
        return _json(
            400,
            jsonrpc.dumps(
                jsonrpc.error(None, jsonrpc.INVALID_REQUEST, "Unsupported MCP-Protocol-Version")
            ),
        )

    # 6-7. Bounded body, JSON parse.
    raw = await read_limited_body(request, MCP_BODY_LIMIT)
    try:
        message = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return _json(400, jsonrpc.dumps(jsonrpc.error(None, jsonrpc.PARSE_ERROR, "Parse error")))

    # 8. Shape (batches are rejected).
    shape_error = jsonrpc.check_shape(message)
    if shape_error is not None:
        return _json(200, jsonrpc.dumps(shape_error))

    # 9. Dispatch.
    status, body = await McpServer(services).handle(message, device, background)
    if body is None:
        return Response(status_code=status)
    return _json(status, body)


@router.post("/mcp")
async def mcp_post(
    request: Request,
    background: BackgroundTasks,
    services: Annotated[Services, Depends(get_services)],
) -> Response:
    return await _handle(request, bearer_token(request), services, background)


@router.post("/mcp/{device_token}")
async def mcp_post_path_token(
    device_token: str,
    request: Request,
    background: BackgroundTasks,
    services: Annotated[Services, Depends(get_services)],
) -> Response:
    if not services.settings.mcp_path_token_enabled:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return await _handle(request, device_token, services, background)


@router.api_route("/mcp", methods=["GET", "DELETE"], include_in_schema=False)
async def mcp_not_allowed() -> Response:
    return _method_not_allowed()


@router.api_route("/mcp/{device_token}", methods=["GET", "DELETE"], include_in_schema=False)
async def mcp_path_not_allowed(device_token: str) -> Response:
    return _method_not_allowed()
