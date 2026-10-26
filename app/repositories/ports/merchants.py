"""Merchants persistence contracts; no driver dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class MerchantRecord:
    id: str
    merchant_code: str
    display_name: str
    status: str


class MerchantRepository(Protocol):
    async def get_active_by_code(self, merchant_code: str) -> MerchantRecord | None: ...

    async def get_by_id(self, merchant_id: str) -> MerchantRecord | None: ...
