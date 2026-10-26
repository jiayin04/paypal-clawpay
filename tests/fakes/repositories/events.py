"""In-memory events adapter sharing the store transaction lock."""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, TYPE_CHECKING


if TYPE_CHECKING:
    from tests.fakes.store import InMemoryStore


class FakeConversationRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def upsert_conversation(
        self,
        *,
        user_id: str,
        device_id: str,
        external_session_id: str,
    ) -> str:
        await self.s._enter("conversations.upsert")
        for conv in self.s.conversations.values():
            if conv["external_session_id"] == external_session_id:
                return conv["id"]
        conv_id = str(uuid.uuid4())
        self.s.conversations[conv_id] = {"id": conv_id, "user_id": user_id, "device_id": device_id,
                                         "source": "esp_claw", "external_session_id": external_session_id}
        return conv_id

    async def add_event(self, *, conversation_id: str, event_type: str, summary: Mapping[str, Any],
                        order_id: str | None = None, correlation_id: str | None = None) -> None:
        await self.s._enter("conversation_events.insert")
        self.s.conversation_events.append({
            "conversation_id": conversation_id, "event_type": event_type, "summary": dict(summary),
            "payload": {}, "order_id": order_id, "correlation_id": correlation_id,
        })


class FakeInsertRepository:
    def __init__(self, store: InMemoryStore, table: list[dict[str, Any]], name: str) -> None:
        self.s = store
        self.table = table
        self.name = name

    async def insert(self, row: Mapping[str, Any]) -> None:
        await self.s._enter(self.name)
        self.table.append(dict(row))
