"""Service-role Supabase adapter for approvals."""
from __future__ import annotations

from datetime import datetime

from app.repositories.errors import RepositoryError
from app.repositories.ports.approvals import ApprovalChallengeRecord
from app.repositories.ports.common import RpcResult
from app.repositories.supabase.base import _SupabaseBase, parse_ts


class SupabaseApprovalRepository(_SupabaseBase):
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
        params = {
            "p_order_id": order_id,
            "p_device_id": device_id,
            "p_user_id": user_id,
            "p_key_version": key_version,
            "p_nonce": nonce,
            "p_canonical": canonical,
            "p_payload_sha256": payload_sha256,
            "p_amount_minor": amount_minor,
            "p_currency": currency,
            "p_expires_at": expires_at.isoformat(),
        }
        try:
            row = await self._rpc_one("issue_approval_challenge", params)
        except RepositoryError as exc:
            if exc.code == "23505":
                # Race on open challenge: retry once
                row = await self._rpc_one("issue_approval_challenge", params)
            else:
                raise
        return RpcResult(
            ok=bool(row["ok"]),
            reason=str(row["reason"]),
            data=row,
        )

    async def get_challenge(self, challenge_id: str) -> ApprovalChallengeRecord | None:
        response = await self._run(
            lambda c: c.table("approval_challenges")
            .select(
                "id,order_id,device_id,user_id,nonce,canonical_payload,payload_sha256,"
                "amount_minor,currency,key_version,expires_at,consumed_at,invalidated_at,created_at"
            )
            .eq("id", challenge_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        if not rows:
            return None
        r = rows[0]
        return ApprovalChallengeRecord(
            id=r["id"],
            order_id=r["order_id"],
            device_id=r["device_id"],
            user_id=r["user_id"],
            nonce=r["nonce"],
            canonical_payload=r["canonical_payload"],
            payload_sha256=r["payload_sha256"],
            amount_minor=int(r["amount_minor"]),
            currency=r["currency"],
            key_version=int(r["key_version"]),
            expires_at=parse_ts(r["expires_at"]),  # type: ignore[arg-type]
            consumed_at=parse_ts(r.get("consumed_at")),
            invalidated_at=parse_ts(r.get("invalidated_at")),
            created_at=parse_ts(r.get("created_at")),
        )

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
        row = await self._rpc_one(
            "record_gadget_denial",
            {
                "p_challenge_id": challenge_id,
                "p_device_id": device_id,
                "p_user_id": user_id,
                "p_nonce": nonce,
                "p_payload_sha256": payload_sha256,
                "p_key_version": key_version,
                "p_actor_type": actor_type,
                "p_actor_id": actor_id,
            },
        )
        return RpcResult(ok=bool(row["ok"]), reason=str(row["reason"]), data=row)

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
        row = await self._rpc_one(
            "accept_gadget_approval",
            {
                "p_challenge_id": challenge_id,
                "p_device_id": device_id,
                "p_user_id": user_id,
                "p_nonce": nonce,
                "p_payload_sha256": payload_sha256,
                "p_policy_currency": policy_currency,
                "p_daily_limit_minor": daily_limit_minor,
                "p_timezone": timezone,
                "p_phone_threshold_minor": phone_threshold_minor,
            },
        )
        return RpcResult(ok=bool(row["ok"]), reason=str(row["reason"]), data=row)
