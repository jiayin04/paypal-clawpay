"""Webhooks persistence contracts; no driver dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from app.repositories.ports.payments import PaymentAttemptRecord


class WebhookRepository(Protocol):
    async def persist_event(self, event_id: str, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...
    async def mark_processed(self, row_id: str, at: datetime) -> None: ...
    async def find_attempt(self, field: str, value: str) -> PaymentAttemptRecord | None: ...
