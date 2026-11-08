"""Bounded Fish Audio TTS. PCM chunks can reach the watch before synthesis ends.

Keys stay server-side; the existing MP3 endpoint remains for older clients.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import anyio
import httpx

from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import ApiError, RateLimited
from app.core.security import SlidingWindowLimiter

logger = logging.getLogger(__name__)
FISH_AUDIO_TTS_URL = "https://api.fish.audio/v1/tts"
MAX_AUDIO_BYTES = 2 * 1024 * 1024
PCM_SAMPLE_RATE = 24000


class TTSService:
    def __init__(self, settings: Settings, clock: Clock, client: httpx.AsyncClient) -> None:
        self.settings, self.client = settings, client
        self.limiter = SlidingWindowLimiter(settings.fish_audio_requests_per_minute, 60, monotonic=clock.monotonic)

    async def synthesize(self, device_id: str, text: str) -> bytes:
        return b"".join([chunk async for chunk in self.audio(device_id, text)])

    async def audio(self, device_id: str, text: str, *, pcm: bool = False) -> AsyncIterator[bytes]:
        s = self.settings
        if s.fish_audio_api_key is None:
            raise ApiError(503, "tts_not_configured", "Text-to-speech is not configured.")
        retry = await self.limiter.acquire(device_id)
        if retry is not None:
            raise RateLimited(retry)
        body: dict[str, object] = {"text": text, "format": "mp3", "mp3_bitrate": 64, "latency": "balanced"}
        if pcm:
            body = {"text": text, "format": "pcm", "sample_rate": PCM_SAMPLE_RATE, "latency": "low"}
        voice = (s.fish_audio_voice_id or "").strip()
        if voice:
            body["reference_id"] = voice
        headers = {"Authorization": "Bearer " + s.fish_audio_api_key.get_secret_value().strip(), "model": s.fish_audio_model}
        unavailable = ApiError(502, "tts_unavailable", "Text-to-speech is unavailable.")
        size = 0
        try:
            async with self.client.stream("POST", FISH_AUDIO_TTS_URL, json=body, headers=headers,
                                          timeout=s.fish_audio_timeout_seconds) as response:
                try:
                    if response.status_code != 200:
                        logger.warning("Fish Audio status=%d", response.status_code)
                        raise unavailable
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_AUDIO_BYTES:
                            logger.warning("Fish Audio response over %d bytes", MAX_AUDIO_BYTES)
                            raise unavailable
                        if chunk:
                            yield chunk
                finally:
                    with anyio.CancelScope(shield=True):
                        await response.aclose()
        except httpx.HTTPError as exc:
            logger.warning("Fish Audio request failed: %s", type(exc).__name__)
            raise unavailable from None
        if not size or (pcm and size % 2):
            raise unavailable
