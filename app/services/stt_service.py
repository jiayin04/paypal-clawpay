"""Bounded WAV -> Fish Audio transcript. Audio and transcripts are never logged or stored."""

from __future__ import annotations

import io
import json
import logging
import wave

import httpx

from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import ApiError, RateLimited
from app.core.security import SlidingWindowLimiter


logger = logging.getLogger(__name__)
MAX_RECORDING_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
FISH_AUDIO_ASR_URL = "https://api.fish.audio/v1/asr"


class STTService:
    def __init__(self, settings: Settings, clock: Clock, client: httpx.AsyncClient) -> None:
        self.settings, self.client = settings, client
        self.limiter = SlidingWindowLimiter(settings.fish_audio_requests_per_minute, 60, monotonic=clock.monotonic)

    async def transcribe(self, device_id: str, audio: bytes) -> str:
        try:
            with wave.open(io.BytesIO(audio), "rb") as recording:
                frames = recording.getnframes()
                if (recording.getnchannels() != 1 or recording.getsampwidth() != 2
                        or recording.getframerate() != 16000 or not 0 < frames <= 30 * 16000
                        or len(recording.readframes(frames)) != frames * 2):
                    raise ValueError("format")
        except (wave.Error, EOFError, ValueError):
            raise ApiError(400, "invalid_audio", "Couldn't read the recording. Record again, up to 30 seconds.") from None
        key = self.settings.fish_audio_api_key
        if key is None:
            raise ApiError(503, "stt_not_configured", "Speech recognition is not configured. Try typing instead.")
        retry = await self.limiter.acquire(device_id)
        if retry is not None:
            raise RateLimited(retry)
        unavailable = ApiError(502, "stt_unavailable", "Speech recognition is unavailable. Try again or type instead.")
        raw = bytearray()
        try:
            async with self.client.stream(
                "POST", FISH_AUDIO_ASR_URL,
                headers={"Authorization": "Bearer " + key.get_secret_value().strip(), "model": "transcribe-1-pro"},
                files={"audio": ("request.wav", audio, "audio/wav")},
                data={"ignore_timestamps": "true", "diarize": "false", "tag_audio_events": "false"},
                timeout=self.settings.fish_audio_timeout_seconds,
            ) as response:
                if response.status_code != 200:
                    logger.warning("Fish Audio ASR status=%d", response.status_code)
                    raise unavailable
                async for chunk in response.aiter_bytes():
                    raw += chunk
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise unavailable
            payload = json.loads(raw)
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str):
                raise unavailable
        except (httpx.HTTPError, ValueError, RecursionError):
            raise unavailable from None
        text = text.strip()
        if not text:
            raise ApiError(422, "no_speech", "No speech detected. Try speaking again.")
        if len(text) > 500:
            raise ApiError(422, "transcript_too_long", "That request is too long. Say a shorter request.")
        return text
