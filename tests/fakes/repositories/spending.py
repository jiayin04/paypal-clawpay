"""In-memory spending adapter sharing the store transaction lock."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.repositories.ports.spending import SpendingSummaryRow


if TYPE_CHECKING:
    from tests.fakes.store import InMemoryStore


class FakeSpendingRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def summary(self, user_id: str, timezone: str) -> SpendingSummaryRow:
        from zoneinfo import ZoneInfo

        await self.s._enter("spending_summary")
        day = self.s.now().astimezone(ZoneInfo(timezone)).date()
        rows = [r for r in self.s.spending_records if r["user_id"] == user_id and r["spending_day"] == day]
        return SpendingSummaryRow(
            day,
            sum(r["amount_minor"] for r in rows if r["status"] == "committed"),
            sum(r["amount_minor"] for r in rows if r["status"] == "reserved"),
        )
