"""Devices persistence contracts; no driver dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.repositories.ports.common import RpcResult


@dataclass(frozen=True)
class DeviceRecord:
    id: str
    user_id: str
    status: str
    key_version: int
    public_key: str
    last_seen_at: datetime | None = None


class DeviceRepository(Protocol):
    async def find_by_token_hash(self, token_hash: str) -> DeviceRecord | None: ...

    async def find_by_llm_token_hash(self, token_hash: str) -> DeviceRecord | None: ...

    async def touch_last_seen(self, device_id: str, at: datetime) -> None: ...

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
        """RPC register_device -> data {device_id}. Raises RepositoryError on 23505."""
        ...

    async def revoke(
        self, *, device_id: str, user_id: str | None, actor_type: str, actor_id: str | None
    ) -> RpcResult:
        """RPC revoke_device -> data {device_id, cancelled_orders}."""
        ...
