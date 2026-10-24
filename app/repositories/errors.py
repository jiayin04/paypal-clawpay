"""Repository error type and the single deadlock/serialization retry (design §1.1)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import anyio

from app.core.exceptions import ServiceUnavailable

logger = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_CODES = frozenset({"40P01", "40001"})
RETRY_DELAY_SECONDS = 0.05


class RepositoryError(Exception):
    """A database call failed. ``code`` is the Postgres SQLSTATE when known."""

    def __init__(self, code: str | None = None, constraint: str | None = None) -> None:
        super().__init__(f"repository error code={code} constraint={constraint}")
        self.code = code
        self.constraint = constraint


async def call_rpc_with_retry(name: str, call: Callable[[], Awaitable[T]]) -> T:
    """Run ``call``; retry once after 50 ms on deadlock/serialization failure.

    A second retryable failure raises ServiceUnavailable (503). Other
    RepositoryErrors propagate unchanged so callers can map constraint codes.
    """
    try:
        return await call()
    except RepositoryError as exc:
        if exc.code not in RETRYABLE_CODES:
            raise
        logger.warning("RPC %s failed with %s; retrying once", name, exc.code)
    await anyio.sleep(RETRY_DELAY_SECONDS)
    try:
        return await call()
    except RepositoryError as exc:
        if exc.code in RETRYABLE_CODES:
            logger.warning("RPC %s failed again with %s", name, exc.code)
            raise ServiceUnavailable() from exc
        raise
