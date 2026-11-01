"""Fish Audio text-to-speech for watch replies; the API key never leaves the server.

The watch posts the assistant's reply text and plays the MP3 it gets back. Any
failure is a 502/503 and the watch falls back to its built-in voice, so speech
is never on the critical path of an order. Text is never logged.
"""

from __future__ import annotations

import logging

import httpx

from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import ApiError, RateLimited
from app.core.security import SlidingWindowLimiter


logger = logging.getLogger(__name__)

FISH_AUDIO_TTS_URL = "https://api.fish.audio/v1/tts"
MAX_AUDIO_BYTES = 2 * 1024 * 1024  # ~4 min of 64 kbps MP3; a two-sentence reply is far smaller


class TTSService:
    def __init__(self, settings: Settings, clock: Clock, client: httpx.AsyncClient) -> None:
        self.settings, self.client = settings, client
        self.limiter = SlidingWindowLimiter(settings.fish_audio_requests_per_minute, 60, monotonic=clock.monotonic)

    async def synthesize(self, device_id: str, text: str) -> bytes:
        s = self.settings
        if s.fish_audio_api_key is None:
            raise ApiError(503, "tts_not_configured", "Text-to-speech is not configured.")
        retry = await self.limiter.acquire(device_id)
        if retry is not None:
            raise RateLimited(retry)
        body: dict[str, object] = {"text": text, "format": "mp3", "mp3_bitrate": 64, "latency": "balanced"}
        # .env values pasted from a web page often carry trailing spaces; Fish rejects them.
        voice = (s.fish_audio_voice_id or "").strip()
        if voice:
            body["reference_id"] = voice
        key = s.fish_audio_api_key.get_secret_value().strip()
        headers = {"Authorization": "Bearer " + key, "model": s.fish_audio_model}
        unavailable = ApiError(502, "tts_unavailable", "Text-to-speech is unavailable.")
        audio = bytearray()
        try:
            async with self.client.stream("POST", FISH_AUDIO_TTS_URL, json=body, headers=headers,
                                          timeout=s.fish_audio_timeout_seconds) as response:
                if response.status_code != 200:
                    # 401 bad key, 402 out of credits, 429/503 overloaded: all operator problems.
                    logger.warning("Fish Audio status=%d", response.status_code)
                    raise unavailable
                async for chunk in response.aiter_bytes():
                    audio += chunk
                    if len(audio) > MAX_AUDIO_BYTES:  # stop reading; never buffer an unbounded body
                        logger.warning("Fish Audio response over %d bytes", MAX_AUDIO_BYTES)
                        raise unavailable
        except httpx.HTTPError as exc:
            logger.warning("Fish Audio request failed: %s", type(exc).__name__)
            raise unavailable from None
        if not audio:
            logger.warning("Fish Audio returned no audio")
            raise unavailable
        return bytes(audio)
