"""Service-role Supabase adapter for devices."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.repositories.ports.common import RpcResult
from app.repositories.ports.devices import DeviceRecord
from app.repositories.supabase.base import _SupabaseBase, parse_ts


_DEVICE_COLUMNS = "id,user_id,status,key_version,public_key,last_seen_at"


def _device(row: Mapping[str, Any]) -> DeviceRecord:
    return DeviceRecord(
        id=row["id"],
        user_id=row["user_id"],
        status=row["status"],
        key_version=int(row["key_version"]),
        public_key=row["public_key"],
        last_seen_at=parse_ts(row.get("last_seen_at")),
    )


class SupabaseDeviceRepository(_SupabaseBase):
    async def _find(self, column: str, token_hash: str) -> DeviceRecord | None:
        response = await self._run(
            lambda c: c.table("devices").select(_DEVICE_COLUMNS).eq(column, token_hash).limit(1).execute()
        )
        rows = response.data or []
        return _device(rows[0]) if rows else None

    async def find_by_token_hash(self, token_hash: str) -> DeviceRecord | None:
        return await self._find("token_hash", token_hash)

    async def find_by_llm_token_hash(self, token_hash: str) -> DeviceRecord | None:
        return await self._find("llm_token_hash", token_hash)

    async def touch_last_seen(self, device_id: str, at: datetime) -> None:
        await self._run(
            lambda c: c.table("devices").update({"last_seen_at": at.isoformat()}).eq("id", device_id).execute()
        )

    async def register(
        self,
        *,
        user_id: str,
        device_name: str,
        public_key: str,
        key_version: int,
        token_hash: str,
        llm_token_hash: str,
        actor_type: str,
        actor_id: str | None,
    ) -> RpcResult:
        row = await self._rpc_one(
            "register_device",
            {
                "p_user_id": user_id,
                "p_device_name": device_name,
                "p_public_key": public_key,
                "p_key_version": key_version,
                "p_token_hash": token_hash,
                "p_llm_token_hash": llm_token_hash,
                "p_actor_type": actor_type,
                "p_actor_id": actor_id,
            },
        )
        return RpcResult(bool(row["ok"]), row["reason"], {"device_id": row.get("device_id")})

    async def revoke(
        self, *, device_id: str, user_id: str | None, actor_type: str, actor_id: str | None
    ) -> RpcResult:
        row = await self._rpc_one(
            "revoke_device",
            {
                "p_device_id": device_id,
                "p_user_id": user_id,
                "p_actor_type": actor_type,
                "p_actor_id": actor_id,
            },
        )
        return RpcResult(
            bool(row["ok"]),
            row["reason"],
            {"device_id": row.get("device_id"), "cancelled_orders": row.get("cancelled_orders") or 0},
        )
