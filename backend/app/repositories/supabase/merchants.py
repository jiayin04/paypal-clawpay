"""Service-role Supabase adapter for merchants."""
from __future__ import annotations

from typing import Any

from supabase import Client

from app.repositories.ports.merchants import MerchantRecord
from app.repositories.supabase.base import _SupabaseBase


class SupabaseMerchantRepository(_SupabaseBase):
    async def _one(self, column: str, value: str, active_only: bool) -> MerchantRecord | None:
        def query(c: Client) -> Any:
            q = c.table("merchants").select("id,merchant_code,display_name,status").eq(column, value)
            if active_only:
                q = q.eq("status", "active")
            return q.limit(1).execute()

        rows = (await self._run(query)).data or []
        if not rows:
            return None
        r = rows[0]
        return MerchantRecord(r["id"], r["merchant_code"], r["display_name"], r["status"])

    async def get_active_by_code(self, merchant_code: str) -> MerchantRecord | None:
        return await self._one("merchant_code", merchant_code, True)

    async def get_by_id(self, merchant_id: str) -> MerchantRecord | None:
        return await self._one("id", merchant_id, False)
