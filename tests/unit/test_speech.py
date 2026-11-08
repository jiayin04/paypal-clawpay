"""POST /api/v1/assistant/speech: Fish Audio TTS proxy for the watch."""

import json
import logging

import httpx
import pytest

from app.services.tts_service import MAX_AUDIO_BYTES
from tests.fakes.app import make_harness


MP3 = b"ID3\x04fake-mp3-bytes"
FISH = {"fish_audio_api_key": "fish-secret", "fish_audio_voice_id": "voice-123"}


class FakeFish:
    def __init__(self, status=200, content=MP3, fail=False):
        self.status, self.content, self.fail = status, content, fail
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectTimeout("timeout", request=request)
        return httpx.Response(self.status, content=self.content)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


def setup(fish=None, **settings):
    fish = fish or FakeFish()
    h = make_harness(tts_client=fish.client(), **{**FISH, **settings})
    token, _, _ = h.new_device()
    return h, fish, token


def speak(h, token, **body):
    return h.client.post("/api/v1/assistant/speech", json=body, headers={"Authorization": f"Bearer {token}"})


def test_returns_mp3_and_sends_key_voice_and_model_upstream(caplog):
    caplog.set_level(logging.DEBUG)
    h, fish, token = setup()
    r = speak(h, token, text="Hold the button to pay RM13.80.")
    assert r.status_code == 200 and r.content == MP3
    assert r.headers["content-type"] == "audio/mpeg" and r.headers["cache-control"] == "no-store"
    [req] = fish.requests
    assert str(req.url) == "https://api.fish.audio/v1/tts"
    assert req.headers["authorization"] == "Bearer fish-secret" and req.headers["model"] == "s2.1-pro"
    body = json.loads(req.content)
    assert body == {"text": "Hold the button to pay RM13.80.", "format": "mp3", "mp3_bitrate": 64,
                    "latency": "balanced", "reference_id": "voice-123"}
    assert "fish-secret" not in caplog.text and "RM13.80" not in caplog.text


def test_pasted_whitespace_is_stripped():
    h, fish, token = setup(fish_audio_voice_id="voice-123   ", fish_audio_api_key=" fish-secret ")
    assert speak(h, token, text="hi").status_code == 200
    assert json.loads(fish.requests[0].content)["reference_id"] == "voice-123"
    assert fish.requests[0].headers["authorization"] == "Bearer fish-secret"


def test_voice_id_is_optional():
    h, fish, token = setup(fish_audio_voice_id=None)
    assert speak(h, token, text="hi").status_code == 200
    assert "reference_id" not in json.loads(fish.requests[0].content)


@pytest.mark.parametrize("fish", [FakeFish(status=402), FakeFish(status=401), FakeFish(status=503),
                                  FakeFish(content=b""), FakeFish(content=b"x" * (MAX_AUDIO_BYTES + 1)),
                                  FakeFish(fail=True)])
def test_upstream_failures_are_502(fish):
    h, _, token = setup(fish)
    r = speak(h, token, text="hi")
    assert r.status_code == 502 and r.json()["error"]["code"] == "tts_unavailable"
    assert "fish-secret" not in r.text


def test_revoked_device_and_bad_model_setting():
    h, fish, token = setup()
    device_id = next(iter(h.store.devices))
    h.store.devices[device_id]["status"] = "revoked"
    assert speak(h, token, text="hi").status_code == 401 and fish.requests == []
    with pytest.raises(ValueError):
        make_harness(fish_audio_model="s2-pro-free")


def test_speech_does_not_use_the_generic_device_limit():
    h, fish, token = setup(device_requests_per_minute=1)
    assert [speak(h, token, text="hi").status_code for _ in range(3)] == [200, 200, 200]


def test_not_configured_auth_validation_and_rate_limit():
    h = make_harness()
    token, _, _ = h.new_device()
    r = speak(h, token, text="hi")
    assert r.status_code == 503 and r.json()["error"]["code"] == "tts_not_configured"

    h, fish, token = setup(fish_audio_requests_per_minute=2)
    assert h.client.post("/api/v1/assistant/speech", json={"text": "hi"}).status_code == 401
    assert speak(h, token, text="").status_code == 400
    assert speak(h, token, text="x" * 501).status_code == 400
    assert speak(h, token, text="hi", voice="other").status_code == 400
    assert fish.requests == []
    assert [speak(h, token, text="hi").status_code for _ in range(3)] == [200, 200, 429]


def test_stream_endpoint_uses_low_latency_bounded_pcm_and_owner_device_auth():
    pcm = b"\0\0" * 4800
    h, fish, token = setup(FakeFish(content=pcm), device_requests_per_minute=1, fish_audio_requests_per_minute=2)
    path = "/api/v1/assistant/speech/stream"
    assert h.client.post(path, json={"text": "hi"}).status_code == 401
    headers = {"Authorization": f"Bearer {token}"}
    assert h.client.post(path, json={"text": ""}, headers=headers).status_code == 400
    response = h.client.post(path, json={"text": "Ready to review."}, headers=headers)
    assert response.status_code == 200 and response.content == pcm
    assert response.headers["x-audio-format"] == "pcm-s16le"
    assert response.headers["x-audio-sample-rate"] == "24000"
    assert response.headers["x-audio-channels"] == "1"
    assert response.headers["cache-control"] == "no-store" and response.headers["x-accel-buffering"] == "no"
    assert json.loads(fish.requests[0].content) == {
        "text": "Ready to review.", "format": "pcm", "sample_rate": 24000, "latency": "low", "reference_id": "voice-123",
    }
    # Both audio routes share a TTS budget, separate from generic device requests.
    assert speak(h, token, text="hi").status_code == 200
    assert h.client.post(path, json={"text": "hi"}, headers=headers).status_code == 429
    h.store.devices[next(iter(h.store.devices))]["status"] = "revoked"
    assert h.client.post(path, json={"text": "hi"}, headers=headers).status_code == 401


@pytest.mark.parametrize("fish", [FakeFish(status=402), FakeFish(content=b""), FakeFish(fail=True),
                                  FakeFish(content=b"x" * (MAX_AUDIO_BYTES + 1))])
def test_stream_errors_before_audio_headers_are_json(fish):
    h, _, token = setup(fish)
    response = h.client.post("/api/v1/assistant/speech/stream", json={"text": "hi"},
                             headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 502 and response.json()["error"]["code"] == "tts_unavailable"
    assert "fish-secret" not in response.text


def test_pcm_first_chunk_precedes_provider_completion_and_cancel_closes_upstream():
    import asyncio

    class SlowAudio(httpx.AsyncByteStream):
        tail_requested = False
        closed = False

        async def __aiter__(self):
            yield b"\0\0" * 2400
            self.tail_requested = True
            await asyncio.Event().wait()  # never completes; the first audio must not wait for this

        async def aclose(self):
            self.closed = True

    async def check():
        provider = SlowAudio()
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=provider)))
        h = make_harness(tts_client=client, **FISH)
        audio = h.services.tts.audio("device", "Ready to review.", pcm=True)
        try:
            first = await asyncio.wait_for(anext(audio), timeout=1)
            assert len(first) == 4800 and not provider.tail_requested
            await audio.aclose()
            assert provider.closed
        finally:
            await client.aclose()

    asyncio.run(check())
