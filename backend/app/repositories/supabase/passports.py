"""Service-role Supabase adapter for agent passports (grants and receipts)."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from postgrest.types import ReturnMethod

from app.repositories.ports.common import RpcResult
from app.repositories.supabase.base import _SupabaseBase, parse_ts


DELEGATION_COLUMNS = (
    "id,user_id,device_id,merchants,max_order_minor,currency,expires_at,revoked_at,"
    "key_id,document,signature,created_at"
)
RECEIPT_COLUMNS = (
    "id,delegation_id,device_id,order_id,decision,reason,merchant_code,amount_minor,currency,"
    "key_id,document,signature,created_at"
)


def _times(row: dict[str, Any], *names: str) -> dict[str, Any]:
    for name in names:
        row[name] = parse_ts(row.get(name))
    return row


class SupabasePassportRepository(_SupabaseBase):
    async def create_delegation(
        self,
        *,
        delegation_id: str,
        user_id: str,
        device_id: str,
        merchants: list[str],
        max_order_minor: int,
        currency: str,
        expires_at: datetime,
        key_id: str,
        document: str,
        signature: str,
    ) -> RpcResult:
        row = await self._rpc_one("create_agent_delegation", {
            "p_id": delegation_id, "p_user_id": user_id, "p_device_id": device_id,
            "p_merchants": merchants, "p_max_order_minor": max_order_minor, "p_currency": currency,
            "p_expires_at": expires_at.isoformat(), "p_key_id": key_id, "p_document": document,
            "p_signature": signature,
        })
        return RpcResult(bool(row["ok"]), row["reason"], {"delegation_id": row.get("delegation_id")})

    async def revoke_delegation(self, *, delegation_id: str, user_id: str) -> RpcResult:
        row = await self._rpc_one("revoke_agent_delegation", {"p_id": delegation_id, "p_user_id": user_id})
        return RpcResult(bool(row["ok"]), row["reason"])

    async def latest_for_device(self, device_id: str) -> dict[str, Any] | None:
        # The live (unrevoked) grant wins; there is at most one. Otherwise the newest revoked one.
        live = await self._run(
            lambda c: c.table("agent_delegations").select(DELEGATION_COLUMNS)
            .eq("device_id", device_id).is_("revoked_at", "null").limit(1).execute()
        )
        rows = live.data or []
        if not rows:
            latest = await self._run(
                lambda c: c.table("agent_delegations").select(DELEGATION_COLUMNS)
                .eq("device_id", device_id).order("created_at", desc=True).limit(1).execute()
            )
            rows = latest.data or []
        return _times(dict(rows[0]), "expires_at", "revoked_at", "created_at") if rows else None

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        response = await self._run(
            lambda c: c.table("agent_delegations").select(DELEGATION_COLUMNS)
            .eq("user_id", user_id).order("created_at", desc=True).limit(100).execute()
        )
        return [_times(dict(r), "expires_at", "revoked_at", "created_at") for r in response.data or []]

    async def insert_receipt(self, row: Mapping[str, Any]) -> None:
        await self._run(
            lambda c: c.table("agent_receipts").insert(dict(row), returning=ReturnMethod.minimal).execute()
        )

    async def list_receipts(self, user_id: str, *, limit: int) -> list[dict[str, Any]]:
        response = await self._run(
            lambda c: c.table("agent_receipts").select(RECEIPT_COLUMNS)
            .eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute()
        )
        return [_times(dict(r), "created_at") for r in response.data or []]
