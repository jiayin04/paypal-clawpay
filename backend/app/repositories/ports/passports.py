"""Agent passport persistence contracts; no driver dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from app.repositories.ports.common import RpcResult


class PassportRepository(Protocol):
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
        """RPC create_agent_delegation -> created | device_not_found | device_revoked | delegation_exists."""
        ...

    async def revoke_delegation(self, *, delegation_id: str, user_id: str) -> RpcResult:
        """RPC revoke_agent_delegation -> revoked | already_revoked | not_found."""
        ...

    async def latest_for_device(self, device_id: str) -> dict[str, Any] | None:
        """The device's unrevoked grant if any, else its most recent one; None if it never had one."""
        ...

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]: ...

    async def insert_receipt(self, row: Mapping[str, Any]) -> None: ...

    async def list_receipts(self, user_id: str, *, limit: int) -> list[dict[str, Any]]: ...
