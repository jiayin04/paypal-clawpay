"""Text-in / text-out agent for watch and voice clients (server-side tool loop).

Mounted with MCP (MCP_ENABLED=true); auth is the device's ``cpd_`` token, so
orders belong to that device and are approved on it with the button hold.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

import anyio

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.api.body import DEVICE_REST_BODY_LIMIT, read_limited_body
from app.api.dependencies import ServicesDep, authenticate_device, bearer_token, require_device
from app.core.exceptions import ApiError
from app.integrations.mcp.assistant import Assistant, ConversationMemory
from app.schemas.assistant import AssistantMessageRequest, SpeechRequest
from app.services.context import DeviceContext
from app.services.stt_service import MAX_RECORDING_BYTES
from app.services.tts_service import PCM_SAMPLE_RATE


router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

_memory = ConversationMemory()
logger = logging.getLogger(__name__)


@router.post("/transcriptions")
async def transcribe(request: Request, background: BackgroundTasks, services: ServicesDep) -> JSONResponse:
    """Audio -> text only; this endpoint never runs the agent or creates an order."""
    device = await authenticate_device(request, bearer_token(request), services, background)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "audio/wav":
        raise ApiError(415, "unsupported_audio", "Unsupported recording. Try recording again.")
    audio = await read_limited_body(request, MAX_RECORDING_BYTES)
    text = await services.stt.transcribe(device.device_id, audio)
    return JSONResponse({"text": text}, headers={"Cache-Control": "no-store"})


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


@router.post("/messages/stream", response_class=StreamingResponse)
async def stream_message(
    request: Request,
    background: BackgroundTasks,
    device: Annotated[DeviceContext, Depends(require_device)],
    services: ServicesDep,
) -> StreamingResponse:
    """Live action summaries and one final result; never exposes model reasoning tokens."""
    raw = await read_limited_body(request, DEVICE_REST_BODY_LIMIT)
    try:
        body = AssistantMessageRequest.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "invalid_request", "Send a request with 1-500 characters.") from None

    async def events():
        sender, receiver = anyio.create_memory_object_stream[dict[str, Any]](16)

        async def run():
            async with sender:
                try:
                    reply = await Assistant(services, _memory).handle(
                        device, body.text, background, reset=body.reset, approver=body.approver,
                        progress=sender.send,
                    )
                    await sender.send({"type": "result", "data": reply})
                except ApiError as exc:
                    await sender.send({"type": "error", "error": {"code": exc.code, "message": exc.message}})
                except Exception:
                    logger.exception("Assistant stream failed")
                    await sender.send({"type": "error", "error": {
                        "code": "assistant_interrupted", "message": "Request interrupted. Check your current order before retrying.",
                    }})

        async with anyio.create_task_group() as group:
            group.start_soon(run)
            try:
                async with receiver:
                    async for event in receiver:
                        yield json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            finally:
                group.cancel_scope.cancel()

    return StreamingResponse(events(), media_type="application/x-ndjson", background=background, headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })


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


@router.post("/speech/stream", response_class=StreamingResponse)
async def stream_speech(request: Request, background: BackgroundTasks, services: ServicesDep) -> StreamingResponse:
    """Authenticated low-latency PCM, bounded and proxied without whole-response buffering."""
    device = await authenticate_device(request, bearer_token(request), services, background)
    raw = await read_limited_body(request, DEVICE_REST_BODY_LIMIT)
    try:
        body = SpeechRequest.model_validate_json(raw)
    except ValidationError:
        raise ApiError(400, "invalid_request", "Send {\"text\": \"...\"} with 1-500 characters.") from None
    stream = services.tts.audio(device.device_id, body.text, pcm=True)
    # Surface provider/auth/rate-limit errors as JSON before committing audio headers.
    first = await anext(stream)

    async def chunks():
        try:
            yield first
            async for chunk in stream:
                yield chunk
        finally:
            with anyio.CancelScope(shield=True):
                await stream.aclose()  # close the provider even during disconnect cancellation

    return StreamingResponse(chunks(), media_type="application/octet-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        "X-Audio-Format": "pcm-s16le", "X-Audio-Sample-Rate": str(PCM_SAMPLE_RATE), "X-Audio-Channels": "1",
    })
