"""In-memory merchants adapter sharing the store transaction lock."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.repositories.ports.merchants import MerchantRecord


if TYPE_CHECKING:
    from tests.fakes.store import InMemoryStore


class FakeMerchantRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def get_active_by_code(self, merchant_code: str) -> MerchantRecord | None:
        await self.s._enter("merchants.get_active_by_code")
        for m in self.s.merchants.values():
            if m["merchant_code"] == merchant_code and m["status"] == "active":
                return MerchantRecord(m["id"], m["merchant_code"], m["display_name"], m["status"])
        return None

    async def get_by_id(self, merchant_id: str) -> MerchantRecord | None:
        m = self.s.merchants.get(merchant_id)
        return MerchantRecord(m["id"], m["merchant_code"], m["display_name"], m["status"]) if m else None
