"""Best-effort audit writer for events raised in Python (design §1.6).

Events inside RPCs are written by the RPC in the same transaction. Here a
failure is logged at ERROR and never propagates. ``details`` must never hold
tokens, signatures, canonical bytes, provider payloads or prompts.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from app.core.metrics import METRICS
from app.repositories.ports.events import AuditRepository


logger = logging.getLogger(__name__)


class AuditService:
    def __init__(self, repo: AuditRepository) -> None:
        self._repo = repo

    async def record(
        self,
        event_type: str,
        *,
        actor_type: str,
        actor_id: str | None = None,
        user_id: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        row = {
            "event_type": event_type,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "user_id": user_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "details": dict(details or {}),
        }
        METRICS.audit_events.inc(event_type)
        try:
            await self._repo.insert(row)
        except Exception as exc:
            logger.error("Audit write failed for %s: %s", event_type, type(exc).__name__)
