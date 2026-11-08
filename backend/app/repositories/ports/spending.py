"""Spending persistence contracts; no driver dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class SpendingSummaryRow:
    day: date
    committed_minor: int
    reserved_minor: int


class SpendingRepository(Protocol):
    async def summary(self, user_id: str, timezone: str) -> SpendingSummaryRow: ...
