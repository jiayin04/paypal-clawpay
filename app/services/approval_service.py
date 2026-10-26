"""Approval challenge issuance and gadget approval/denial (design §3.2, §3.3).

Phase 3.2: issue_challenge
Phase 3.3: submit_gadget_approval (ECDSA verify + RPC)
Inline execution uses the persisted approval lease and a bounded PayPal budget.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import re
from datetime import datetime, timezone
from typing import Any

import anyio

from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import ApiError, Conflict, Forbidden, Gone
from app.domain.canonical import build_approval_payload, payload_sha256
from app.repositories.errors import RepositoryError, RpcRetryRepository
from app.repositories.ports.approvals import ApprovalRepository
from app.repositories.ports.orders import OrderRepository
from app.services.context import DeviceContext


logger = logging.getLogger(__name__)

_NONCE_BYTES = 32  # 43 base64url chars (no padding)
_SIG_RE = re.compile(r"^[A-Za-z0-9_-]{60,120}$")


def _b64url_decode(value: str) -> bytes:
    """Base64url-decode; re-pad and use strict alphabet."""
    pad = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + pad,altchars=b"-_",validate=True)
    except Exception as exc:
        raise ValueError(f"base64url decode failed: {exc}") from exc


def _nonce() -> str:
    raw = os.urandom(_NONCE_BYTES)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class ApprovalService:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        approvals: ApprovalRepository,
        orders: OrderRepository,
        payment_service: Any = None,
        payment_repo: Any = None,
        audit: Any = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._approvals = RpcRetryRepository(approvals)
        self._audit = audit
        self._orders = orders
        self._payment_service = payment_service
        self._payment_repo = payment_repo

    # ------------------------------------------------------------------
    # §3.2: GET /api/v1/orders/{order_id}/approval-challenge
    # ------------------------------------------------------------------

    async def issue_challenge(
        self, device: DeviceContext, order_id: str, merchant_info: dict[str, Any]
    ) -> dict[str, Any]:
        """Build a candidate payload and issue (or return existing) challenge."""
        now = self._clock.utc_now()
        challenge_expires_at = int(
            math.floor(
                min(
                    merchant_info["quote_expires_at"].timestamp(),
                    now.timestamp() + self._settings.challenge_ttl_seconds,
                )
            )
        )
        nonce = _nonce()
        canonical_bytes = build_approval_payload(
            amount_minor=merchant_info["total_minor"],
            cart_sha256=merchant_info["cart_sha256"],
            currency=merchant_info["currency"],
            device_id=device.device_id,
            expires_at=challenge_expires_at,
            key_version=device.key_version,
            merchant_id=merchant_info["merchant_code"],
            merchant_name=merchant_info["merchant_name"],
            nonce=nonce,
            order_id=order_id,
        )
        sha256 = payload_sha256(canonical_bytes)
        expires_at_dt = datetime.fromtimestamp(challenge_expires_at, tz=timezone.utc)

        async def _do_issue() -> Any:
            return await self._approvals.issue_challenge(
                order_id=order_id,
                device_id=device.device_id,
                user_id=device.user_id,
                key_version=device.key_version,
                nonce=nonce,
                canonical=canonical_bytes.decode("ascii"),
                payload_sha256=sha256,
                amount_minor=merchant_info["total_minor"],
                currency=merchant_info["currency"],
                expires_at=expires_at_dt,
            )

        try:
            result = await _do_issue()
        except RepositoryError as exc:
            if exc.code == "23505":  # unique violation: concurrent GET won the race
                await anyio.sleep(0.05)
                try:
                    result = await _do_issue()
                except RepositoryError as exc2:
                    logger.error("issue_approval_challenge retry failed: %s", exc2.code)
                    raise
            else:
                logger.error("issue_approval_challenge RPC failed: %s", exc.code)
                raise

        if not result.ok:
            reason = result.reason
            if reason == "order_not_found":
                raise ApiError(404, "order_not_found", "Order not found.")
            if reason == "device_revoked":
                raise ApiError(403, "device_revoked", "Device has been revoked.")
            if reason == "invalid_order_status":
                raise Conflict("invalid_order_status", "Order is not awaiting approval.")
            if reason == "quote_expired":
                raise Gone("quote_expired", "The quote has expired.")
            raise ApiError(500, "internal_error", f"Unexpected reason: {reason}")

        data = result.data
        canonical_text: str = data["canonical_payload"]
        canonical_b64url = (
            base64.urlsafe_b64encode(canonical_text.encode("ascii")).rstrip(b"=").decode()
        )
        return {
            "challenge_id": str(data["challenge_id"]),
            "order_id": order_id,
            "canonical_b64url": canonical_b64url,
            "canonical": canonical_text,
            "nonce": data["nonce"],
            "key_version": int(data["key_version"]),
            "expires_at": data["expires_at"],
        }

    # ------------------------------------------------------------------
    # §3.3: POST /api/v1/orders/{order_id}/gadget-approval
    # ------------------------------------------------------------------

    async def submit_gadget_approval(
        self,
        device: DeviceContext,
        order_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate signature and call record_gadget_denial / accept_gadget_approval."""
        challenge_id = str(body["challenge_id"])
        req_device_id = str(body["device_id"])
        key_version = int(body["key_version"])
        nonce = str(body["nonce"])
        decision = str(body["decision"])
        signature_raw: str | None = body.get("signature")

        # 1. device_id in body must match token device
        if req_device_id.lower() != device.device_id.lower():
            raise Forbidden("Device ID does not match authenticated device.", code="device_mismatch")

        # Signature pre-check for approved
        if decision == "approved":
            if signature_raw is None or not _SIG_RE.match(signature_raw):
                raise ApiError(400, "malformed_signature", "Signature is missing or malformed.")
        else:
            signature_raw = None  # denied: discard any provided signature

        # 2-6: read challenge (pre-check, no lock)
        challenge = await self._approvals.get_challenge(challenge_id)
        if challenge is None or challenge.order_id != order_id or challenge.device_id != device.device_id:
            await self._audit_approval(device, "approval.challenge_not_found", order_id)
            raise ApiError(404, "challenge_not_found", "Challenge not found.")
        if challenge.consumed_at is not None or challenge.invalidated_at is not None:
            await self._audit_approval(device, "approval.replay_rejected", order_id)
            raise Conflict("challenge_consumed", "Challenge already consumed.")
        now = self._clock.utc_now()
        if challenge.expires_at is not None and challenge.expires_at <= now:
            await self._audit_approval(device, "approval.challenge_expired", order_id)
            raise Gone("challenge_expired", "Challenge has expired.")
        if key_version != challenge.key_version or key_version != device.key_version:
            await self._audit_approval(device, "approval.key_version_mismatch", order_id)
            raise Conflict("key_version_mismatch", "Key version mismatch.")
        import hmac as _hmac
        if not _hmac.compare_digest(nonce, challenge.nonce):
            await self._audit_approval(device, "approval.replay_rejected", order_id)
            raise Conflict("nonce_mismatch", "Nonce mismatch.")

        # 7-8: signature verification (approved only)
        if decision == "approved":
            try:
                der_bytes = _b64url_decode(signature_raw)  # type: ignore[arg-type]
                from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
                decode_dss_signature(der_bytes)
            except Exception:
                raise ApiError(400, "malformed_signature", "Signature could not be decoded.")
            try:
                from cryptography.hazmat.primitives import hashes
                from cryptography.hazmat.primitives.asymmetric import ec
                from cryptography.hazmat.primitives.serialization import load_pem_public_key
                pem = device.public_key
                if not pem.startswith("-----"):
                    # raw base64 PEM-less key: try DER
                    key_bytes = base64.b64decode(pem)
                    from cryptography.hazmat.backends import default_backend
                    pub_key = load_pem_public_key(
                        b"-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(key_bytes) + b"\n-----END PUBLIC KEY-----",
                        backend=default_backend(),
                    )
                else:
                    pub_key = load_pem_public_key(pem.encode())
                canonical_bytes = challenge.canonical_payload.encode("ascii")
                pub_key.verify(der_bytes, canonical_bytes, ec.ECDSA(hashes.SHA256()))  # type: ignore[arg-type]
            except ApiError:
                raise
            except Exception as exc:
                logger.info("Signature verification failed: %s", type(exc).__name__)
                await self._audit_approval(device, "approval.signature_failed", order_id)
                raise ApiError(422, "signature_invalid", "Signature verification failed.")

        # Atomic RPC
        if decision == "denied":
            result = await self._approvals.record_denial(
                challenge_id=challenge_id,
                device_id=device.device_id,
                user_id=device.user_id,
                nonce=nonce,
                payload_sha256=challenge.payload_sha256,
                key_version=key_version,
                actor_type="device",
                actor_id=device.device_id,
            )
            if not result.ok:
                return self._map_rpc_error(result.reason)
            return {
                "order_id": order_id,
                "approval": "denied",
                "status": "denied",
                "message": "Declined on the device.",
            }

        # decision == "approved"
        settings = self._settings
        result = await self._approvals.accept_approval(
            challenge_id=challenge_id,
            device_id=device.device_id,
            user_id=device.user_id,
            nonce=nonce,
            payload_sha256=challenge.payload_sha256,
            policy_currency=settings.default_currency,
            daily_limit_minor=settings.daily_spending_limit_minor,
            timezone=settings.spending_day_timezone,
            phone_threshold_minor=settings.phone_approval_threshold_minor,
        )
        if not result.ok:
            return self._map_rpc_error(result.reason)

        # Inline payment execution inside budget (§3.8)
        attempt_id = result.data.get("attempt_id") if result.data else None
        if self._payment_service is not None and self._payment_repo is not None and attempt_id:
            try:
                attempt = await self._payment_repo.get_attempt(str(attempt_id))
                if attempt is not None:
                    budget = float(self._settings.paypal_inline_budget_seconds)
                    with anyio.fail_after(budget):
                        classification = await self._payment_service.execute(attempt)
                        if classification.status == "completed":
                            return {
                                "order_id": order_id,
                                "approval": "verified",
                                "status": "paid",
                                "message": "Payment completed.",
                            }
                        if classification.status == "failed":
                            return {
                                "order_id": order_id,
                                "approval": "verified",
                                "status": "payment_failed",
                                "message": "Payment failed.",
                            }
            except TimeoutError:
                logger.info("Inline payment execution timed out; returning payment_pending")
            except Exception as exc:
                logger.warning("Inline payment execution failed with %s; returning payment_pending", exc)

        return {
            "order_id": order_id,
            "approval": "verified",
            "status": "payment_pending",
            "message": "Approved. Payment is processing.",
        }

    async def _audit_approval(self, device: DeviceContext, event: str, order_id: str) -> None:
        """Best-effort local audit log; the RPC logs its own audit inside the DB."""
        if self._audit is not None:
            await self._audit.record(event,actor_type="device",actor_id=device.device_id,user_id=device.user_id,entity_type="order",entity_id=order_id)

    def _map_rpc_error(self, reason: str) -> dict[str, Any]:
        """Map RPC failure reasons to HTTP errors (design §3.3 tables)."""
        _4xx: dict[str, tuple[int, str, str]] = {
            "order_not_found": (404, "order_not_found", "Order not found."),
            "challenge_not_found": (404, "challenge_not_found", "Challenge not found."),
            "device_revoked": (403, "device_revoked", "Device has been revoked."),
            "challenge_consumed": (409, "challenge_consumed", "Challenge already consumed."),
            "challenge_expired": (410, "challenge_expired", "Challenge has expired."),
            "invalid_order_status": (409, "invalid_order_status", "Order status does not allow approval."),
            "quote_expired": (410, "quote_expired", "The quote has expired."),
            "key_version_mismatch": (409, "key_version_mismatch", "Key version mismatch."),
            "nonce_mismatch": (409, "nonce_mismatch", "Nonce mismatch."),
            "hash_mismatch": (409, "hash_mismatch", "Payload hash mismatch."),
            "phone_approval_required": (
                409,
                "phone_approval_required",
                "Orders above the threshold need phone approval, which is not available yet.",
            ),
            "no_payment_method": (
                409,
                "no_payment_method",
                "No active payment method linked.",
            ),
            "daily_limit_exceeded": (409, "daily_limit_exceeded", "Daily spending limit exceeded."),
            "currency_not_supported": (409, "currency_not_supported", "Currency not supported."),
        }
        if reason in _4xx:
            status, code, msg = _4xx[reason]
            raise ApiError(status, code, msg)
        raise ApiError(500, "internal_error", f"Unexpected approval reason: {reason}")
