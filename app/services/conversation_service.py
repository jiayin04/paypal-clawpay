"""User-safe conversation events (design §2.8, review finding #3).

``summary`` is built only from per-event-type allowlists, never from raw tool
output; ``payload`` is always written as {}.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from app.core.clock import Clock
from app.core.config import Settings
from app.repositories.protocols import ConversationRepository
from app.services.context import DeviceContext

logger = logging.getLogger(__name__)

_CALL_ARG_KEYS: dict[str, tuple[str, ...]] = {
    "search_products": ("query", "merchant_id"),
    "create_order": ("merchant_id", "items"),
    "get_order_status": ("order_id",),
    "get_spending_summary": (),
}
_RESPONSE_KEYS = ("result_count", "order_id", "status", "total_minor", "currency")


def call_summary(tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
    allowed = _CALL_ARG_KEYS.get(tool, ())
    out: dict[str, Any] = {}
    for key in allowed:
        if key not in args:
            continue
        value = args[key]
        if key == "items" and isinstance(value, list):
            value = [
                {"sku": str(i.get("sku"))[:64], "qty": i.get("qty")}
                for i in value[:10]
                if isinstance(i, Mapping)
            ]
        elif isinstance(value, str):
            value = value[:100]
        elif value is not None and not isinstance(value, (int, bool)):
            continue
        out[key] = value
    return {"tool": tool, "args": out}


def response_summary(
    tool: str,
    *,
    is_error: bool,
    error_code: str | None,
    result: Mapping[str, Any] | None,
    latency_ms: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {"tool": tool, "is_error": is_error, "latency_ms": latency_ms}
    if error_code:
        out["error_code"] = error_code
    if result and not is_error:
        if tool == "search_products" and isinstance(result.get("results"), list):
            out["result_count"] = len(result["results"])
        for key in _RESPONSE_KEYS[1:]:
            value = result.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                out[key] = value
    return out


class ConversationService:
    def __init__(self, settings: Settings, clock: Clock, repo: ConversationRepository) -> None:
        self._settings = settings
        self._clock = clock
        self._repo = repo

    def session_id(self, device_id: str) -> str:
        day = self._clock.utc_now().astimezone(self._settings.spending_tz).date().isoformat()
        return f"device:{device_id}:{day}"

    async def record_tool_call(
        self,
        device: DeviceContext,
        tool: str,
        call: Mapping[str, Any],
        response: Mapping[str, Any],
        order_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Write agent_call + agent_response. Best-effort: failures are WARNING only."""
        try:
            conversation_id = await self._repo.upsert_conversation(
                user_id=device.user_id,
                device_id=device.device_id,
                external_session_id=self.session_id(device.device_id),
            )
            await self._repo.add_event(
                conversation_id=conversation_id,
                event_type="agent_call",
                summary=call,
                correlation_id=correlation_id,
            )
            await self._repo.add_event(
                conversation_id=conversation_id,
                event_type="agent_response",
                summary=response,
                order_id=order_id,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            logger.warning("Conversation event write failed: %s", type(exc).__name__)
