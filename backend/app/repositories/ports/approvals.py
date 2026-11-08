"""Approvals persistence contracts; no driver dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.repositories.ports.common import RpcResult


@dataclass(frozen=True)
class ApprovalChallengeRecord:
    id: str
    order_id: str
    device_id: str
    user_id: str
    nonce: str
    canonical_payload: str
    payload_sha256: str
    amount_minor: int
    currency: str
    key_version: int
    expires_at: datetime
    consumed_at: datetime | None = None
    invalidated_at: datetime | None = None
    created_at: datetime | None = None


class ApprovalRepository(Protocol):
    async def issue_challenge(
        self,
        *,
        order_id: str,
        device_id: str,
        user_id: str,
        key_version: int,
        nonce: str,
        canonical: str,
        payload_sha256: str,
        amount_minor: int,
        currency: str,
        expires_at: datetime,
    ) -> RpcResult:
        """RPC issue_approval_challenge -> data {challenge_id, nonce, canonical_payload, expires_at, key_version}."""
        ...

    async def get_challenge(self, challenge_id: str) -> ApprovalChallengeRecord | None: ...

    async def record_denial(
        self,
        *,
        challenge_id: str,
        device_id: str,
        user_id: str,
        nonce: str,
        payload_sha256: str,
        key_version: int,
        actor_type: str,
        actor_id: str | None,
    ) -> RpcResult:
        """RPC record_gadget_denial -> ok, reason."""
        ...

    async def accept_approval(
        self,
        *,
        challenge_id: str,
        device_id: str,
        user_id: str,
        nonce: str,
        payload_sha256: str,
        policy_currency: str,
        daily_limit_minor: int,
        timezone: str,
        phone_threshold_minor: int,
    ) -> RpcResult:
        """RPC accept_gadget_approval -> data {attempt_id, idempotency_key, vault_token_reference}."""
        ...
