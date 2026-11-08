"""Repository error type and the single deadlock/serialization retry (design §1.1)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

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


class RpcRetryRepository:
    """Apply the shared single retry to selected RPC methods, including test fakes.

    Phase 2 callers already wrap their RPCs explicitly; phase 3 services use
    this adapter to keep the same policy for every state-changing operation.
    Reads are delegated without retry and no argument values are logged.
    """
    RPC_METHODS = frozenset({
        'issue_challenge', 'record_denial', 'accept_approval',
        'mark_attempt_sent', 'mark_capture_sent', 'apply_payment_result',
        'mark_attempt_exhausted', 'claim_reconcilable_attempt',
        'claim_reconcilable_attempts', 'activate_payment_method',
        'revoke_payment_method', 'find_by_state', 'expire_stale_orders',
    })

    def __init__(self, repository):
        self._repository = repository._repository if isinstance(repository,RpcRetryRepository) else repository

    def __getattr__(self, name):
        method = getattr(self._repository,name)
        if name not in self.RPC_METHODS:
            return method
        async def call(*args, **kwargs):
            return await call_rpc_with_retry(name,lambda: method(*args,**kwargs))
        return call


RepositoryT = TypeVar("RepositoryT")


def with_rpc_retry(repository: RepositoryT) -> RepositoryT:
    """Decorate RPC calls while preserving the caller's repository interface."""
    return cast(RepositoryT, RpcRetryRepository(repository))
