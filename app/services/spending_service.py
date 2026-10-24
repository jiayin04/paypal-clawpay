"""Daily spending summary (RPC spending_summary, day derived in SQL)."""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.domain.money import format_display
from app.repositories.errors import call_rpc_with_retry
from app.repositories.protocols import SpendingRepository, SpendingSummaryRow


class SpendingService:
    def __init__(self, settings: Settings, repo: SpendingRepository) -> None:
        self._settings = settings
        self._repo = repo

    async def summary(self, user_id: str) -> SpendingSummaryRow:
        tz = self._settings.spending_day_timezone
        return await call_rpc_with_retry("spending_summary", lambda: self._repo.summary(user_id, tz))

    def remaining_minor(self, row: SpendingSummaryRow) -> int:
        limit = self._settings.daily_spending_limit_minor
        return max(limit - row.committed_minor - row.reserved_minor, 0)

    async def tool_summary(self, user_id: str) -> dict[str, Any]:
        row = await self.summary(user_id)
        remaining = self.remaining_minor(row)
        currency = self._settings.default_currency
        return {
            "period": "today",
            "day": row.day.isoformat(),
            "timezone": self._settings.spending_day_timezone,
            "currency": currency,
            "limit_minor": self._settings.daily_spending_limit_minor,
            "committed_minor": row.committed_minor,
            "reserved_minor": row.reserved_minor,
            "remaining_minor": remaining,
            "remaining": format_display(remaining, currency),
        }
