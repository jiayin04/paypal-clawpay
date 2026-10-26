"""Service-role Supabase adapter for spending."""
from __future__ import annotations

from datetime import date

from app.repositories.ports.spending import SpendingSummaryRow
from app.repositories.supabase.base import _SupabaseBase


class SupabaseSpendingRepository(_SupabaseBase):
    async def summary(self, user_id: str, timezone: str) -> SpendingSummaryRow:
        row = await self._rpc_one("spending_summary", {"p_user_id": user_id, "p_timezone": timezone})
        day = row["day"]
        return SpendingSummaryRow(
            day=day if isinstance(day, date) else date.fromisoformat(str(day)),
            committed_minor=int(row["committed_minor"]),
            reserved_minor=int(row["reserved_minor"]),
        )
