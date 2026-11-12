"""In-memory agent passport adapter sharing the store transaction lock."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, TYPE_CHECKING

from app.repositories.errors import RepositoryError
from app.repositories.ports.common import RpcResult


if TYPE_CHECKING:
    from tests.fakes.store import InMemoryStore


class FakePassportRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def create_delegation(self, *, delegation_id: str, user_id: str, device_id: str, merchants: list[str],
                                max_order_minor: int, currency: str, expires_at: datetime, key_id: str,
                                document: str, signature: str) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("create_agent_delegation")
            device = s.devices.get(device_id)
            if device is None or device["user_id"] != user_id:
                return RpcResult(False, "device_not_found", {"delegation_id": None})
            if device["status"] != "active":
                return RpcResult(False, "device_revoked", {"delegation_id": None})
            now = s.now()
            for g in s.delegations:
                if g["device_id"] == device_id and g["revoked_at"] is None and g["expires_at"] <= now:
                    g["revoked_at"] = now
            if any(g["device_id"] == device_id and g["revoked_at"] is None for g in s.delegations):
                return RpcResult(False, "delegation_exists", {"delegation_id": None})
            s.delegations.append({
                "id": delegation_id, "user_id": user_id, "device_id": device_id, "merchants": list(merchants),
                "max_order_minor": max_order_minor, "currency": currency, "expires_at": expires_at,
                "revoked_at": None, "key_id": key_id, "document": document, "signature": signature,
                "created_at": now,
            })
            s._audit(user_id, "user", user_id, "delegation.created", "device", device_id,
                     {"delegation_id": delegation_id})
            return RpcResult(True, "created", {"delegation_id": delegation_id})

    async def revoke_delegation(self, *, delegation_id: str, user_id: str) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("revoke_agent_delegation")
            grant = next((g for g in s.delegations if g["id"] == delegation_id), None)
            if grant is None or grant["user_id"] != user_id:
                return RpcResult(False, "not_found")
            if grant["revoked_at"] is not None:
                return RpcResult(True, "already_revoked")
            grant["revoked_at"] = s.now()
            s._audit(user_id, "user", user_id, "delegation.revoked", "device", grant["device_id"],
                     {"delegation_id": delegation_id})
            return RpcResult(True, "revoked")

    async def latest_for_device(self, device_id: str) -> dict[str, Any] | None:
        await self.s._enter("agent_delegations.latest_for_device")
        rows = [g for g in self.s.delegations if g["device_id"] == device_id]
        live = [g for g in rows if g["revoked_at"] is None]
        if live:
            return dict(live[0])
        return dict(max(rows, key=lambda g: g["created_at"])) if rows else None

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        await self.s._enter("agent_delegations.list_for_user")
        rows = [dict(g) for g in self.s.delegations if g["user_id"] == user_id]
        return sorted(rows, key=lambda g: g["created_at"], reverse=True)

    async def insert_receipt(self, row: Mapping[str, Any]) -> None:
        await self.s._enter("agent_receipts.insert")
        if row.get("signature") is not None and len(row["signature"]) != 86:
            raise RepositoryError("23514", "agent_receipts_signature_check")
        self.s.agent_receipts.append({**row, "created_at": self.s.now()})

    async def list_receipts(self, user_id: str, *, limit: int) -> list[dict[str, Any]]:
        await self.s._enter("agent_receipts.list")
        rows = [{k: v for k, v in r.items() if k != "user_id"} for r in self.s.agent_receipts
                if r["user_id"] == user_id]
        return list(reversed(rows))[:limit]
