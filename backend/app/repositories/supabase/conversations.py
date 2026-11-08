"""Service-role Supabase adapter for conversations."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from postgrest.types import ReturnMethod

from app.repositories.errors import RepositoryError
from app.repositories.supabase.base import _SupabaseBase


class SupabaseConversationRepository(_SupabaseBase):
    async def upsert_conversation(
        self, *, user_id: str, device_id: str, external_session_id: str
    ) -> str:
        response = await self._run(
            lambda c: c.table("conversations")
            .upsert(
                {
                    "user_id": user_id,
                    "device_id": device_id,
                    "source": "esp_claw",
                    "external_session_id": external_session_id,
                },
                on_conflict="source,external_session_id",
            )
            .execute()
        )
        rows = response.data or []
        if not rows:
            raise RepositoryError(None)
        return rows[0]["id"]

    async def add_event(
        self,
        *,
        conversation_id: str,
        event_type: str,
        summary: Mapping[str, Any],
        order_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        await self._run(
            lambda c: c.table("conversation_events")
            .insert(
                {
                    "conversation_id": conversation_id,
                    "order_id": order_id,
                    "correlation_id": correlation_id,
                    "event_type": event_type,
                    "summary": dict(summary),
                    "payload": {},  # server-only column; always empty in this scope
                },
                returning=ReturnMethod.minimal,
            )
            .execute()
        )
