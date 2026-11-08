"""Shared service-role query execution, error mapping and timestamp parsing.

Only this adapter layer knows the synchronous Supabase driver. Calls execute
in worker threads; services depend on feature-specific persistence protocols.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, TypeVar

import anyio
from postgrest.exceptions import APIError
from supabase import Client

from app.core.exceptions import AppError
from app.repositories.errors import RepositoryError


logger = logging.getLogger(__name__)
T = TypeVar('T')
_CONSTRAINT_RE = re.compile(r'constraint "([A-Za-z0-9_]+)"')

def parse_ts(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _constraint(exc: APIError) -> str | None:
    for text in (exc.message, exc.details):
        if text:
            match = _CONSTRAINT_RE.search(str(text))
            if match:
                return match.group(1)
    return None


class _SupabaseBase:
    def __init__(self, client_provider: Callable[[], Client]) -> None:
        self._client_provider = client_provider

    async def _run(self, fn: Callable[[Client], T]) -> T:
        def work() -> T:
            return fn(self._client_provider())

        try:
            return await anyio.to_thread.run_sync(work)
        except APIError as exc:
            raise RepositoryError(exc.code, _constraint(exc)) from None
        except (AppError, RepositoryError):
            raise
        except Exception as exc:
            # Type only: driver errors can contain URLs or keys.
            logger.error("Database call failed: %s", type(exc).__name__)
            raise RepositoryError(None) from None

    async def _rpc(self, name: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        response = await self._run(lambda c: c.rpc(name, dict(params)).execute())
        data = response.data
        if data is None:
            return []
        return data if isinstance(data, list) else [data]

    async def _rpc_one(self, name: str, params: Mapping[str, Any]) -> dict[str, Any]:
        rows = await self._rpc(name, params)
        if not rows:
            raise RepositoryError(None)
        return rows[0]
