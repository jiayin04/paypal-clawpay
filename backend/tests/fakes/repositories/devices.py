"""In-memory devices adapter sharing the store transaction lock."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, TYPE_CHECKING

from app.repositories.errors import RepositoryError
from app.repositories.ports.common import RpcResult
from app.repositories.ports.devices import DeviceRecord


if TYPE_CHECKING:
    from tests.fakes.store import InMemoryStore


def _device_record(row: Mapping[str, Any]) -> DeviceRecord:
    return DeviceRecord(row["id"], row["user_id"], row["status"], row["key_version"], row["public_key"],
                        row["last_seen_at"])


class FakeDeviceRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def find_by_token_hash(self, token_hash: str) -> DeviceRecord | None:
        await self.s._enter("devices.find_by_token_hash")
        for row in self.s.devices.values():
            if row["token_hash"] == token_hash:
                return _device_record(row)
        return None

    async def find_by_llm_token_hash(self, token_hash: str) -> DeviceRecord | None:
        await self.s._enter("devices.find_by_llm_token_hash")
        for row in self.s.devices.values():
            if row["llm_token_hash"] == token_hash:
                return _device_record(row)
        return None

    async def touch_last_seen(self, device_id: str, at: datetime) -> None:
        await self.s._enter("devices.touch_last_seen")
        if device_id in self.s.devices:
            self.s.devices[device_id]["last_seen_at"] = at

    async def register(self, *, user_id: str, device_name: str, public_key: str, key_version: int,
                       token_hash: str, llm_token_hash: str, actor_type: str,
                       actor_id: str | None) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("register_device")
            if user_id not in s.users:
                return RpcResult(False, "user_not_found", {"device_id": None})
            if any(d["public_key"] == public_key and d["status"] != "revoked" for d in s.devices.values()):
                return RpcResult(False, "public_key_already_registered", {"device_id": None})
            if any(d["token_hash"] == token_hash for d in s.devices.values()):
                raise RepositoryError("23505", "devices_token_hash_key")
            device_id = s.add_device(user_id, token_hash=token_hash, llm_token_hash=llm_token_hash,
                                     public_key=public_key, key_version=key_version)
            s.devices[device_id]["device_name"] = device_name
            s._audit(user_id, actor_type, actor_id, "device.registered", "device", device_id,
                     {"key_version": key_version})
            s._audit(user_id, actor_type, actor_id, "device.token_issued", "device", device_id,
                     {"kinds": ["device", "llm"]})
            return RpcResult(True, "registered", {"device_id": device_id})

    async def revoke(self, *, device_id: str, user_id: str | None, actor_type: str,
                     actor_id: str | None) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("revoke_device")
            device = s.devices.get(device_id)
            if device is None or (user_id is not None and device["user_id"] != user_id):
                return RpcResult(False, "not_found", {"device_id": None, "cancelled_orders": 0})
            reason = "already_revoked"
            if device["status"] != "revoked":
                device["status"] = "revoked"
                device["revoked_at"] = s.now()
                reason = "revoked"
                s._audit(device["user_id"], actor_type, actor_id, "device.revoked", "device", device_id)
            cancelled = 0
            for order in sorted(s.orders.values(), key=lambda o: o["id"]):
                if order["device_id"] == device_id and order["status"] in ("CREATED", "QUOTED", "AWAITING_APPROVAL"):
                    s._transition(order, "CANCELLED", actor_type, actor_id, "device_revoked")
                    cancelled += 1
            return RpcResult(True, reason, {"device_id": device_id, "cancelled_orders": cancelled})
