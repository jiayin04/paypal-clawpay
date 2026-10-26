"""In-memory approvals adapter sharing the store transaction lock."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.repositories.ports.approvals import ApprovalChallengeRecord
from app.repositories.ports.common import RpcResult


if TYPE_CHECKING:
    from tests.fakes.store import InMemoryStore


class FakeApprovalRepository:
    """Fake ApprovalRepository mirroring SQL RPCs in phase3 migration."""

    def __init__(self, store: InMemoryStore) -> None:
        self.s = store

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
        s = self.s
        async with s.lock:
            await s._enter("issue_approval_challenge")
            order = s.orders.get(order_id)
            if order is None or order["user_id"] != user_id or order["device_id"] != device_id:
                return RpcResult(False, "order_not_found", {"challenge_id": None})
            device = s.devices.get(device_id)
            if device is None or device["status"] != "active":
                return RpcResult(False, "device_revoked", {"challenge_id": None})
            if order["status"] != "AWAITING_APPROVAL":
                return RpcResult(False, "invalid_order_status", {"challenge_id": None})
            now = s.now()
            if order["quote_expires_at"] is not None and order["quote_expires_at"] <= now:
                order["status"] = "EXPIRED"
                return RpcResult(False, "quote_expired", {"challenge_id": None})
            # Return existing open challenge if any
            for ch in s.approval_challenges.values():
                if (
                    ch["order_id"] == order_id
                    and ch["device_id"] == device_id
                    and ch["consumed_at"] is None
                    and ch["invalidated_at"] is None
                    and ch["expires_at"] > now
                ):
                    return RpcResult(
                        True,
                        "existing",
                        {
                            "challenge_id": ch["id"],
                            "nonce": ch["nonce"],
                            "canonical_payload": ch["canonical_payload"],
                            "expires_at": ch["expires_at"],
                            "key_version": ch["key_version"],
                        },
                    )
            # Invalidate expired open challenges
            for ch in s.approval_challenges.values():
                if (
                    ch["order_id"] == order_id
                    and ch["device_id"] == device_id
                    and ch["consumed_at"] is None
                    and ch["invalidated_at"] is None
                    and ch["expires_at"] <= now
                ):
                    ch["invalidated_at"] = now
            ch_id = str(uuid.uuid4())
            s.approval_challenges[ch_id] = {
                "id": ch_id,
                "order_id": order_id,
                "device_id": device_id,
                "user_id": user_id,
                "nonce": nonce,
                "canonical_payload": canonical,
                "payload_sha256": payload_sha256,
                "amount_minor": amount_minor,
                "currency": currency,
                "key_version": key_version,
                "expires_at": expires_at,
                "consumed_at": None,
                "invalidated_at": None,
                "created_at": now,
            }
            s._audit(user_id, "device", device_id, "approval.challenge_issued", "challenge", ch_id,
                     {"order_id": order_id, "expires_at": expires_at})
            return RpcResult(
                True,
                "issued",
                {
                    "challenge_id": ch_id,
                    "nonce": nonce,
                    "canonical_payload": canonical,
                    "expires_at": expires_at,
                    "key_version": key_version,
                },
            )

    async def get_challenge(self, challenge_id: str) -> ApprovalChallengeRecord | None:
        await self.s._enter("approval_challenges.get")
        ch = self.s.approval_challenges.get(challenge_id)
        if ch is None:
            return None
        return ApprovalChallengeRecord(
            id=ch["id"],
            order_id=ch["order_id"],
            device_id=ch["device_id"],
            user_id=ch["user_id"],
            nonce=ch["nonce"],
            canonical_payload=ch["canonical_payload"],
            payload_sha256=ch["payload_sha256"],
            amount_minor=ch["amount_minor"],
            currency=ch["currency"],
            key_version=ch["key_version"],
            expires_at=ch["expires_at"],
            consumed_at=ch["consumed_at"],
            invalidated_at=ch["invalidated_at"],
            created_at=ch["created_at"],
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
        s = self.s
        async with s.lock:
            await s._enter("record_gadget_denial")
            ch = s.approval_challenges.get(challenge_id)
            if ch is None or ch["device_id"] != device_id or ch["user_id"] != user_id:
                return RpcResult(False, "challenge_not_found", {})
            device = s.devices.get(device_id)
            if device is None or device["status"] != "active":
                return RpcResult(False, "device_revoked", {})
            order = s.orders.get(ch["order_id"])
            if order is None or order["status"] != "AWAITING_APPROVAL":
                return RpcResult(False, "invalid_order_status", {})
            if ch["consumed_at"] is not None or ch["invalidated_at"] is not None:
                return RpcResult(False, "challenge_consumed", {})
            now = s.now()
            if ch["expires_at"] <= now:
                return RpcResult(False, "challenge_expired", {})
            if ch["key_version"] != key_version:
                return RpcResult(False, "key_version_mismatch", {})
            if ch["nonce"] != nonce:
                return RpcResult(False, "nonce_mismatch", {})
            ch["consumed_at"] = now
            s.approvals.append({
                "order_id": ch["order_id"], "user_id": user_id, "device_id": device_id,
                "approval_type": "gadget", "decision": "denied", "verification_status": "pending",
                "nonce": nonce, "signed_payload_hash": payload_sha256, "key_version": key_version,
                "consumed_at": None, "approved_at": None, "expires_at": ch["expires_at"],
            })
            s._transition(order, "DENIED", actor_type, actor_id, "denied")
            s._audit(user_id, actor_type, actor_id, "approval.denied", "order", ch["order_id"],
                     {"challenge_id": challenge_id})
            return RpcResult(True, "denied", {})

    def _reserve_spending(
        self, user_id: str, order_id: str, amount_minor: int, currency: str,
        daily_limit_minor: int, timezone: str
    ) -> tuple[bool, str]:
        from zoneinfo import ZoneInfo
        day = self.s.now().astimezone(ZoneInfo(timezone)).date()
        existing_currency = next(
            (r["currency"] for r in self.s.spending_records if r["user_id"] == user_id
             and r["spending_day"] == day and r["status"] in ("committed", "reserved")), None
        )
        if existing_currency is not None and existing_currency != currency:
            return False, "currency_mismatch"
        committed = sum(
            r["amount_minor"] for r in self.s.spending_records
            if r["user_id"] == user_id and r["spending_day"] == day
            and r["status"] in ("committed", "reserved")
        )
        if committed + amount_minor > daily_limit_minor:
            return False, "daily_limit_exceeded"
        self.s.spending_records.append({
            "user_id": user_id, "order_id": order_id, "amount_minor": amount_minor,
            "currency": currency, "status": "reserved", "spending_day": day,
        })
        return True, "reserved"

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
        s = self.s
        async with s.lock:
            await s._enter("accept_gadget_approval")
            ch = s.approval_challenges.get(challenge_id)
            if ch is None or ch["device_id"] != device_id or ch["user_id"] != user_id:
                return RpcResult(False, "challenge_not_found", {})
            device = s.devices.get(device_id)
            if device is None or device["status"] != "active":
                return RpcResult(False, "device_revoked", {})
            order = s.orders.get(ch["order_id"])
            if order is None or order["status"] != "AWAITING_APPROVAL":
                return RpcResult(False, "invalid_order_status", {})
            now = s.now()
            if order["quote_expires_at"] is not None and order["quote_expires_at"] <= now:
                order["status"] = "EXPIRED"
                return RpcResult(False, "quote_expired", {})
            if ch["consumed_at"] is not None or ch["invalidated_at"] is not None:
                return RpcResult(False, "challenge_consumed", {})
            if ch["expires_at"] <= now:
                return RpcResult(False, "challenge_expired", {})
            if device["key_version"] != ch["key_version"]:
                return RpcResult(False, "key_version_mismatch", {})
            if ch["nonce"] != nonce:
                return RpcResult(False, "nonce_mismatch", {})
            if ch["payload_sha256"] != payload_sha256:
                return RpcResult(False, "hash_mismatch", {})
            # Phone threshold (unconditional)
            if order["total_minor"] > phone_threshold_minor:
                return RpcResult(False, "phone_approval_required", {})
            # Active payment method required
            pm = next(
                (p for p in s.payment_methods
                 if p["user_id"] == user_id and p["status"] == "active"), None
            )
            if pm is None:
                return RpcResult(False, "no_payment_method", {})
            vault_token = pm.get("vault_token_reference", "vault-token")
            # reserve_spending
            sp_ok, sp_reason = self._reserve_spending(
                user_id, ch["order_id"], order["total_minor"],
                ch["currency"], daily_limit_minor, timezone,
            )
            if not sp_ok:
                if sp_reason == "currency_mismatch":
                    return RpcResult(False, "currency_not_supported", {})
                return RpcResult(False, sp_reason, {})
            # Commit: approval, challenge consumed, mandate, transitions, attempt
            ch["consumed_at"] = now
            s.approvals.append({
                "order_id": ch["order_id"], "user_id": user_id, "device_id": device_id,
                "approval_type": "gadget", "decision": "approved", "verification_status": "verified",
                "nonce": nonce, "signed_payload_hash": payload_sha256, "key_version": ch["key_version"],
                "consumed_at": now, "approved_at": now, "expires_at": ch["expires_at"],
            })
            s.mandates.append({
                "order_id": ch["order_id"], "user_id": user_id,
                "mandate_type": "clawpay.gadget_cart.v1",
                "provider_mandate_reference": payload_sha256, "status": "verified",
                "expires_at": order["quote_expires_at"],
            })
            s._transition(order, "APPROVED", "device", device_id, "gadget_approved")
            s._transition(order, "PAYMENT_PENDING", "device", device_id, "payment_initiated")
            attempt_id = str(uuid.uuid4())
            idem_key = f"clawpay-{ch['order_id']}-1"
            lease = now + timedelta(seconds=30)
            s.payment_attempts[attempt_id] = {
                "id": attempt_id, "order_id": ch["order_id"],
                "payment_method_id": pm["id"], "provider": "paypal",
                "status": "initiated", "idempotency_key": idem_key,
                "amount_minor": order["total_minor"], "currency": ch["currency"],
                "claimed_until": lease, "first_sent_at": None, "capture_sent_at": None,
                "capture_send_count": 0, "completed_at": None, "exhausted_at": None,
                "provider_order_id": None, "provider_capture_id": None,
                "provider_status": None, "failure_code": None,
                "vault_token_reference": vault_token,
                "created_at": now, "updated_at": now,
            }
            s._audit(user_id, "device", device_id, "approval.verified", "order", ch["order_id"],
                     {"challenge_id": challenge_id})
            s._audit(user_id, "device", device_id, "payment.initiated", "payment_attempt", attempt_id,
                     {"idempotency_key": idem_key})
            return RpcResult(
                True, "approved",
                {"attempt_id": attempt_id, "idempotency_key": idem_key,
                 "vault_token_reference": vault_token},
            )
