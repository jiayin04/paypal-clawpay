"""In-memory payment methods adapter sharing the store transaction lock."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any, TYPE_CHECKING

from app.repositories.ports.common import RpcResult


if TYPE_CHECKING:
    from tests.fakes.store import InMemoryStore


class FakePaymentMethodRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

    async def has_active(self, user_id: str) -> bool:
        await self.s._enter("payment_methods.has_active")
        return any(p["user_id"] == user_id and p["status"] == "active" for p in self.s.payment_methods)

    async def list_active(self, user_id: str) -> list[dict[str, Any]]:
        await self.s._enter("payment_methods.list_active")
        cols = ("id", "user_id", "status", "payer_email_masked", "created_at", "updated_at")
        return [
            {k: p.get(k) for k in cols}
            for p in self.s.payment_methods
            if p["user_id"] == user_id and p["status"] == "active"
        ]

    async def get_by_id(self, payment_method_id: str, user_id: str) -> dict[str, Any] | None:
        await self.s._enter("payment_methods.get_by_id")
        for p in self.s.payment_methods:
            if p["id"] == payment_method_id and p["user_id"] == user_id:
                return dict(p)
        return None

    async def create_pending(
        self,
        *,
        user_id: str,
        state_token: str,
        state_expires_at: datetime,
        setup_token_reference: str,
    ) -> str:
        await self.s._enter("payment_methods.create_pending")
        pm_id = str(uuid.uuid4())
        self.s.payment_methods.append({
            "id": pm_id,
            "user_id": user_id,
            "status": "pending",
            "setup_state_hash": hashlib.sha256(state_token.encode()).hexdigest(),
            "setup_expires_at": state_expires_at,
            "setup_token_reference": setup_token_reference,
            "vault_token_reference": None,
            "payer_email_masked": None,
            "created_at": self.s.now(),
            "updated_at": self.s.now(),
        })
        return pm_id

    async def activate_payment_method(
        self,
        *,
        payment_method_id: str,
        user_id: str,
        vault_token_reference: str,
        payer_email_masked: str | None,
        provider_customer_reference: str | None = None,
    ) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("activate_payment_method")
            pm = next((p for p in s.payment_methods if p["id"] == payment_method_id and p["user_id"] == user_id), None)
            if pm is None:
                return RpcResult(False, "not_found", {})
            # Deactivate any existing active payment methods
            for other in s.payment_methods:
                if other["user_id"] == user_id and other["status"] == "active" and other["id"] != payment_method_id:
                    other["status"] = "revoked"
            pm["setup_state_hash"] = None
            pm["provider_customer_reference"] = provider_customer_reference
            pm["status"] = "active"
            pm["vault_token_reference"] = vault_token_reference
            pm["payer_email_masked"] = payer_email_masked
            pm["updated_at"] = s.now()
            s._audit(user_id, "user", user_id, "payment_method.linked", "payment_method", payment_method_id, {})
            return RpcResult(True, "activated", {"payment_method_id": payment_method_id})

    async def revoke_payment_method(
        self,
        *,
        payment_method_id: str,
        user_id: str,
    ) -> RpcResult:
        s = self.s
        async with s.lock:
            await s._enter("revoke_payment_method")
            pm = next((p for p in s.payment_methods if p["id"] == payment_method_id and p["user_id"] == user_id), None)
            if pm is None:
                return RpcResult(False, "not_found", {})
            if pm["status"] == "revoked":
                return RpcResult(True, "already_revoked", {})
            if pm["status"] != "active":
                return RpcResult(False, "not_found", {})
            # Check for in-progress payment attempts
            for att in s.payment_attempts.values():
                order = s.orders.get(att["order_id"])
                if order and order["user_id"] == user_id:
                    if att.get("payment_method_id") == payment_method_id and att["status"] in ("initiated", "pending", "unknown"):
                        return RpcResult(False, "payment_in_progress", {})
            vault_ref = pm.get("vault_token_reference")
            pm["status"] = "revoked"
            pm["updated_at"] = s.now()
            s._audit(user_id, "user", user_id, "payment_method.revoked", "payment_method", payment_method_id, {})
            return RpcResult(True, "revoked", {"vault_token_reference": vault_ref})

    async def find_by_state(self, state_token: str) -> dict[str, Any] | None:
        s = self.s
        async with s.lock:
            await s._enter("payment_methods.find_by_state")
            now = s.now()
            for pm in s.payment_methods:
                if pm.get("setup_state_hash") == hashlib.sha256(state_token.encode()).hexdigest() and pm['status'] == 'pending':
                    if pm['setup_expires_at'] > now:
                        return dict(pm)
            return None

    async def fail_pending(self, pm_id):
        for pm in self.s.payment_methods:
            if pm['id'] == pm_id and pm['status'] == 'pending':
                pm.update(status='failed', setup_state_hash=None)
