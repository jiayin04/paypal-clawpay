"""Text-in / text-out agent for watch and voice clients (server-side tool loop).

Mounted with MCP (MCP_ENABLED=true); auth is the device's ``cpd_`` token, so
orders belong to that device and are approved on it with the button hold.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import ValidationError

from app.api.body import DEVICE_REST_BODY_LIMIT, read_limited_body
from app.api.dependencies import ServicesDep, authenticate_device, bearer_token, require_device
from app.core.exceptions import ApiError
from app.integrations.mcp.assistant import Assistant, ConversationMemory
from app.schemas.assistant import AssistantMessageRequest, SpeechRequest
from app.services.context import DeviceContext


router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

_memory = ConversationMemory()


@router.post("/messages")
async def post_message(
    request: Request,
    background: BackgroundTasks,
    device: Annotated[DeviceContext, Depends(require_device)],
    services: ServicesDep,
) -> dict[str, Any]:
    raw = await read_limited_body(request, DEVICE_REST_BODY_LIMIT)
    try:
        body = AssistantMessageRequest.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "invalid_request", "Send {\"text\": \"...\"} with 1-500 characters.") from None
    return await Assistant(services, _memory).handle(
        device, body.text, background, reset=body.reset, approver=body.approver
    )


@router.post("/speech", response_class=Response)
async def post_speech(request: Request, background: BackgroundTasks, services: ServicesDep) -> Response:
    """Reply text -> MP3 via Fish Audio. 503 when unconfigured; the watch then uses its own voice.

    Rate-limited only by the TTS limiter, not the generic device limit, so speaking a
    reply does not halve how many messages the watch can send.
    """
    device = await authenticate_device(request, bearer_token(request), services, background)
    raw = await read_limited_body(request, DEVICE_REST_BODY_LIMIT)
    try:
        body = SpeechRequest.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "invalid_request", "Send {\"text\": \"...\"} with 1-500 characters.") from None
    audio = await services.tts.synthesize(device.device_id, body.text)
    return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
