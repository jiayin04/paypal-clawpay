"""Service-role Supabase adapter for events."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from postgrest.types import ReturnMethod
from supabase import Client

from app.repositories.supabase.base import _SupabaseBase


class SupabaseInsertRepository(_SupabaseBase):
    """Append-only insert into one table (a2a_messages, audit_logs)."""

    def __init__(self, client_provider: Callable[[], Client], table: str) -> None:
        super().__init__(client_provider)
        self._table = table

    async def insert(self, row: Mapping[str, Any]) -> None:
        await self._run(
            lambda c: c.table(self._table).insert(dict(row), returning=ReturnMethod.minimal).execute()
        )
