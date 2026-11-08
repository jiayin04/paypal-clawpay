"""Actual WAV validation and ASR wire contract; transcription never invokes an agent."""

import io
import logging
import wave

import httpx
import pytest

from app.services.stt_service import MAX_RECORDING_BYTES, MAX_RESPONSE_BYTES
from tests.fakes.app import make_harness


def wav(seconds=0.1, rate=16000, channels=1, width=2):
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        audio.writeframes(bytes(int(rate * seconds) * channels * width))
    return out.getvalue()


class ASR:
    def __init__(self, status=200, content=b'{"text":"  Find socks.  ","duration":0.1}', fail=False):
        self.status, self.content, self.fail = status, content, fail
        self.calls = []

    def handle(self, request):
        self.calls.append(request)
        if self.fail:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(self.status, content=self.content)


def setup(asr=None, **settings):
    asr = asr or ASR()
    h = make_harness(stt_client=httpx.AsyncClient(transport=httpx.MockTransport(asr.handle)),
                     **{"fish_audio_api_key": "fish-stt-secret", **settings})
    token, device, _ = h.new_device()
    return h, asr, token, device


def transcribe(h, token, audio=None, content_type="audio/wav"):
    headers = {"Content-Type": content_type}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return h.client.post("/api/v1/assistant/transcriptions", content=wav() if audio is None else audio, headers=headers)


def test_wav_becomes_reviewable_text_without_creating_order(caplog):
    caplog.set_level(logging.DEBUG)
    h, asr, token, _ = setup()
    response = transcribe(h, token)
    assert response.status_code == 200 and response.json() == {"text": "Find socks."}
    assert response.headers["cache-control"] == "no-store"
    [request] = asr.calls
    assert str(request.url) == "https://api.fish.audio/v1/asr"
    assert request.headers["model"] == "transcribe-1-pro"
    assert request.headers["authorization"] == "Bearer fish-stt-secret"
    assert "multipart/form-data" in request.headers["content-type"] and wav() in request.content
    for field in [b'ignore_timestamps', b'diarize', b'tag_audio_events']:
        assert field in request.content
    assert not h.store.orders and not h.services.llm._usage
    assert "fish-stt-secret" not in caplog.text and "Find socks." not in caplog.text


@pytest.mark.parametrize("audio", [b"", b"not wave", wav(seconds=0), wav(seconds=31),
                                   wav(rate=8000), wav(channels=2), wav(width=1), wav()[:-100]])
def test_rejects_invalid_or_unbounded_recordings_before_provider(audio):
    h, asr, token, _ = setup()
    assert transcribe(h, token, audio).status_code == 400
    assert not asr.calls


def test_auth_size_type_and_revocation_guards():
    h, asr, token, device = setup()
    assert transcribe(h, None).status_code == 401
    assert transcribe(h, token, content_type="application/json").status_code == 415
    assert transcribe(h, token, b"x" * (MAX_RECORDING_BYTES + 1)).status_code == 413
    h.store.devices[device]["status"] = "revoked"
    assert transcribe(h, token).status_code == 401
    assert not asr.calls


@pytest.mark.parametrize("asr", [ASR(status=401), ASR(status=402), ASR(status=503), ASR(fail=True),
                                 ASR(content=b"bad JSON"), ASR(content=b"[]"),
                                 ASR(content=b"x" * (MAX_RESPONSE_BYTES + 1))])
def test_provider_errors_do_not_expose_payloads(asr):
    h, _, token, _ = setup(asr)
    response = transcribe(h, token)
    assert response.status_code == 502 and response.json()["error"]["code"] == "stt_unavailable"
    assert "fish-stt-secret" not in response.text


@pytest.mark.parametrize(("content", "code"), [(b'{"text":"  "}', "no_speech"),
                                              (b'{"text":"' + b"a" * 501 + b'"}', "transcript_too_long")])
def test_empty_or_overlong_transcript_requires_retry(content, code):
    h, _, token, _ = setup(ASR(content=content))
    response = transcribe(h, token)
    assert response.status_code == 422 and response.json()["error"]["code"] == code


def test_no_key_and_separate_transcription_rate_limit():
    h, asr, token, _ = setup(fish_audio_api_key=None)
    assert transcribe(h, token).status_code == 503 and not asr.calls
    h, asr, token, _ = setup(fish_audio_requests_per_minute=2, device_requests_per_minute=1)
    assert [transcribe(h, token).status_code for _ in range(3)] == [200, 200, 429]
    assert len(asr.calls) == 2
