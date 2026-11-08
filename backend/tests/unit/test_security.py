import pytest
from starlette.requests import Request

from app.core.security import (
    DEVICE_TOKEN_RE,
    LLM_TOKEN_RE,
    SlidingWindowLimiter,
    client_ip,
    constant_time_equals,
    generate_device_token,
    generate_llm_token,
    hash_token,
)


def test_token_formats() -> None:
    for _ in range(20):
        device, llm = generate_device_token(), generate_llm_token()
        assert DEVICE_TOKEN_RE.match(device) and len(device) == 47
        assert LLM_TOKEN_RE.match(llm) and len(llm) == 47
        assert not DEVICE_TOKEN_RE.match(llm)
        assert not LLM_TOKEN_RE.match(device)
    assert generate_device_token() != generate_device_token()


def test_hmac_hash_stable_and_pepper_dependent() -> None:
    token = generate_device_token()
    a = hash_token("pepper-one-" * 4, token)
    assert a == hash_token("pepper-one-" * 4, token)
    assert a != hash_token("pepper-two-" * 4, token)
    assert len(a) == 64 and token not in a


def test_constant_time_equals() -> None:
    assert constant_time_equals("abc", "abc")
    assert not constant_time_equals("abc", "abd")


class Mono:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.anyio
async def test_limiter_window_and_retry_after() -> None:
    mono = Mono()
    limiter = SlidingWindowLimiter(3, 60, monotonic=mono)
    for _ in range(3):
        assert await limiter.acquire("k") is None
    mono.t += 10
    assert await limiter.acquire("k") == 50
    assert await limiter.is_over("k") == 50
    assert await limiter.acquire("other") is None
    mono.t += 50.5
    assert await limiter.acquire("k") is None


@pytest.mark.anyio
async def test_limiter_record_and_lru_bound() -> None:
    limiter = SlidingWindowLimiter(1, 60, max_keys=3, monotonic=Mono())
    await limiter.record("a")
    assert await limiter.is_over("a") is not None
    for key in ("b", "c", "d"):
        await limiter.record(key)
    assert await limiter.is_over("a") is None  # evicted


def _request(headers: dict[str, str], peer: str = "10.0.0.5") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 1234),
        }
    )


def test_client_ip_modes() -> None:
    xff = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9", "CF-Connecting-IP": "198.51.100.7"}
    assert client_ip(_request(xff), "none") == "10.0.0.5"
    assert client_ip(_request(xff), "cf-connecting-ip") == "198.51.100.7"
    # Rightmost entry is the one the trusted proxy appended; the spoofed leftmost is ignored.
    assert client_ip(_request(xff), "x-forwarded-for-last") == "203.0.113.9"
    assert client_ip(_request({}), "cf-connecting-ip") == "10.0.0.5"
    assert client_ip(_request({"X-Forwarded-For": "garbage"}), "x-forwarded-for-last") == "10.0.0.5"
