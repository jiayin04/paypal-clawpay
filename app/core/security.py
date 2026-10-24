"""Device tokens, HMAC hashing, constant-time compare, rate limiting, client IP."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import math
import re
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Literal

from starlette.requests import Request

DEVICE_TOKEN_PREFIX = "cpd_"
LLM_TOKEN_PREFIX = "cpl_"
DEVICE_TOKEN_RE = re.compile(r"^cpd_[A-Za-z0-9_-]{43}$")
LLM_TOKEN_RE = re.compile(r"^cpl_[A-Za-z0-9_-]{43}$")

ClientIpMode = Literal["none", "cf-connecting-ip", "x-forwarded-for-last"]


def _random_b64url(n_bytes: int = 32) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(n_bytes)).rstrip(b"=").decode("ascii")


def generate_device_token() -> str:
    return DEVICE_TOKEN_PREFIX + _random_b64url()


def generate_llm_token() -> str:
    return LLM_TOKEN_PREFIX + _random_b64url()


def hash_token(pepper: str, token: str) -> str:
    """HMAC-SHA256(pepper, token) hex digest; the only form stored in the DB."""
    return hmac.new(pepper.encode("utf-8"), token.encode("ascii"), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class SlidingWindowLimiter:
    """In-process sliding-window counter keyed by an arbitrary string.

    Single uvicorn worker assumed: with several workers each has its own counts.
    Bounded to ``max_keys`` keys with LRU eviction.
    """

    def __init__(
        self,
        limit: int,
        window_s: float = 60.0,
        *,
        max_keys: int = 10_000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self._monotonic = monotonic
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = asyncio.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            events = deque()
            self._events[key] = events
            while len(self._events) > self.max_keys:
                self._events.popitem(last=False)
        else:
            self._events.move_to_end(key)
        cutoff = now - self.window_s
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def _retry_after(self, events: deque[float], now: float) -> int:
        return max(1, math.ceil(events[0] + self.window_s - now))

    async def is_over(self, key: str) -> int | None:
        """Retry-After seconds if ``key`` is at/over the limit, else None. Records nothing."""
        async with self._lock:
            now = self._monotonic()
            events = self._prune(key, now)
            if len(events) >= self.limit:
                return self._retry_after(events, now)
            return None

    async def record(self, key: str) -> None:
        async with self._lock:
            now = self._monotonic()
            self._prune(key, now).append(now)

    async def acquire(self, key: str) -> int | None:
        """Take one slot. Returns Retry-After seconds when over the limit (nothing recorded)."""
        async with self._lock:
            now = self._monotonic()
            events = self._prune(key, now)
            if len(events) >= self.limit:
                return self._retry_after(events, now)
            events.append(now)
            return None


def _parse_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request: Request, mode: ClientIpMode) -> str:
    """Client IP for IP-keyed limits. Never trusts the leftmost X-Forwarded-For entry."""
    peer = request.client.host if request.client else "unknown"
    if mode == "cf-connecting-ip":
        return _parse_ip(request.headers.get("cf-connecting-ip")) or peer
    if mode == "x-forwarded-for-last":
        header = request.headers.get("x-forwarded-for")
        if header:
            return _parse_ip(header.split(",")[-1]) or peer
        return peer
    return peer
