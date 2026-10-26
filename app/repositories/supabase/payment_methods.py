"""Service-role Supabase adapter for payment methods."""
from __future__ import annotations

import hashlib

from app.repositories.ports.common import RpcResult
from app.repositories.supabase.base import _SupabaseBase


class SupabasePaymentMethodRepository(_SupabaseBase):
    async def has_active(self, user_id: str) -> bool:
        response = await self._run(
            lambda c: c.table("payment_methods")
            .select("id")
            .eq("user_id", user_id)
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        return bool(response.data)

    async def list_active(self, user_id: str) -> list[dict]:
        cols = "id,user_id,status,payer_email_masked,created_at,updated_at"
        response = await self._run(
            lambda c: c.table("payment_methods")
            .select(cols)
            .eq("user_id", user_id)
            .eq("status", "active")
            .execute()
        )
        return response.data or []

    async def get_by_id(self, payment_method_id: str, user_id: str) -> dict | None:
        response = await self._run(
            lambda c: c.table("payment_methods")
            .select("*")
            .eq("id", payment_method_id)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return response.data

    async def create_pending(
        self,
        *,
        user_id: str,
        state_token: str,
        state_expires_at: object,
        setup_token_reference: str,
    ) -> str:
        row = {
            "user_id": user_id,
            "status": "pending",
            "setup_state_hash": hashlib.sha256(state_token.encode()).hexdigest(),
            "setup_expires_at": str(state_expires_at),
            "setup_token_reference": setup_token_reference,
        }
        response = await self._run(
            lambda c: c.table("payment_methods").insert(row).execute()
        )
        return response.data[0]["id"]

    async def activate_payment_method(
        self,
        *,
        payment_method_id: str,
        user_id: str,
        vault_token_reference: str,
        payer_email_masked: str | None,
        provider_customer_reference: str | None = None,
    ) -> RpcResult:
        row = await self._rpc_one(
            "activate_payment_method",
            {
                "p_payment_method_id": payment_method_id,
                "p_provider_customer_reference": provider_customer_reference,
                "p_vault_token_reference": vault_token_reference,
                "p_payer_email_masked": payer_email_masked,
            },
        )
        return RpcResult(ok=bool(row["ok"]), reason=str(row["reason"]), data=dict(row))

    async def revoke_payment_method(
        self,
        *,
        payment_method_id: str,
        user_id: str,
    ) -> RpcResult:
        row = await self._rpc_one(
            "revoke_payment_method",
            {
                "p_id": payment_method_id,
                "p_user_id": user_id,
            },
        )
        return RpcResult(ok=bool(row["ok"]), reason=str(row["reason"]), data=dict(row))

    async def find_by_state(self, state_token: str) -> dict | None:
        rows = await self._rpc("find_payment_method_by_state", {"p_state_hash": hashlib.sha256(state_token.encode()).hexdigest()})
        return rows[0] if rows and rows[0].get("found") and not rows[0].get("expired") else None

    async def fail_pending(self, pm_id: str) -> None:
        await self._run(lambda c: c.table("payment_methods").update({"status": "failed", "setup_state_hash": None}).eq("id", pm_id).eq("status", "pending").execute())
