"""Events persistence contracts; no driver dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class ConversationRepository(Protocol):
    async def upsert_conversation(
        self, *, user_id: str, device_id: str, external_session_id: str
    ) -> str: ...

    async def add_event(
        self,
        *,
        conversation_id: str,
        event_type: str,
        summary: Mapping[str, Any],
        order_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None: ...


class A2AMessageRepository(Protocol):
    async def insert(self, row: Mapping[str, Any]) -> None: ...


class AuditRepository(Protocol):
    async def insert(self, row: Mapping[str, Any]) -> None: ...
