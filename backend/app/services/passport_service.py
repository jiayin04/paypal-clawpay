"""Agent passports (lite): signed, revocable grants from a user to an agent device.

Rules applied by ``create_order``:
- A device that has never had a grant is not governed and orders as before,
  unless AGENT_PASSPORT_REQUIRED=true.
- Once a device has had a grant, its latest grant must be active (not revoked,
  not expired), allow the merchant and cover the order amount. Otherwise the
  order is refused before it is created.
- Every decision for a governed device is written as a signed receipt
  (best-effort: a failed receipt write never blocks or fails an order).

Spending caps, the phone threshold and the signed device approval still apply
on top. Grants are checked when the order is created, not again at approval.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from datetime import timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import ApiError, Conflict, ServiceUnavailable
from app.domain import passport
from app.repositories.errors import RepositoryError, call_rpc_with_retry
from app.repositories.ports.passports import PassportRepository
from app.services.context import DeviceContext
from app.services.errors import ToolError


logger = logging.getLogger(__name__)

DENIAL_MESSAGES: dict[str, str] = {
    "no_delegation": "This device has no agent passport. The owner must grant one before it can order.",
    "delegation_revoked": "This device's agent passport was revoked by the owner.",
    "delegation_expired": "This device's agent passport has expired. Ask the owner to issue a new one.",
    "merchant_not_delegated": "This device's agent passport does not allow ordering from that shop.",
    "currency_not_delegated": "This device's agent passport does not allow that currency.",
    "delegation_limit_exceeded": "This order is above the per-order limit in this device's agent passport.",
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_signing_key(seed_b64url: str) -> Ed25519PrivateKey:
    """32-byte Ed25519 seed, base64url (padding optional). Raises ValueError otherwise."""
    try:
        seed = base64.urlsafe_b64decode(seed_b64url + "=" * (-len(seed_b64url) % 4))
    except (ValueError, TypeError):
        raise ValueError("not base64url") from None
    if len(seed) != 32:
        raise ValueError("must decode to 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


class PassportService:
    def __init__(self, settings: Settings, clock: Clock, repo: PassportRepository) -> None:
        self._settings = settings
        self._clock = clock
        self._repo = repo
        self._key: Ed25519PrivateKey | None = None
        self._public_raw: bytes | None = None
        self.key_id: str | None = None
        secret = settings.agent_passport_signing_key
        if secret is not None:
            self._key = load_signing_key(secret.get_secret_value())
            self._public_raw = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            self.key_id = hashlib.sha256(self._public_raw).hexdigest()[:16]

    # -- signing -------------------------------------------------------------------

    def _sign(self, document: bytes) -> str | None:
        return _b64url(self._key.sign(document)) if self._key is not None else None

    def public_keys(self) -> dict[str, Any]:
        keys = []
        if self._public_raw is not None:
            x = _b64url(self._public_raw)
            keys.append({"key_id": self.key_id, "algorithm": "Ed25519", "public_key": x,
                         "jwk": {"kty": "OKP", "crv": "Ed25519", "x": x, "kid": self.key_id}})
        return {
            "issuer": passport.ISSUER,
            "keys": keys,
            "documents": {
                passport.DELEGATION_PURPOSE: "Grant from a user to an agent device",
                passport.RECEIPT_PURPOSE: "Allowed or denied order decision under a grant",
            },
            "canonicalization": "JSON, keys sorted, no whitespace, ASCII only",
            "signature": "Ed25519 over the exact document bytes, base64url without padding",
        }

    # -- grants (owner API) ----------------------------------------------------------

    async def create(
        self, *, user_id: str, device_id: str, merchants: list[str], max_order_minor: int,
        expires_in_hours: int,
    ) -> dict[str, Any]:
        if self._key is None or self.key_id is None:
            raise ServiceUnavailable("Agent passports are not configured.", "passport_not_configured")
        settings = self._settings
        unknown = sorted(set(merchants) - set(settings.merchant_codes))
        if unknown:
            raise ApiError(422, "validation_error", f"Unknown merchant: {unknown[0]}")
        if max_order_minor > settings.daily_spending_limit_minor:
            raise ApiError(422, "validation_error", "max_order_minor is above the daily spending limit.")
        now = self._clock.utc_now().replace(microsecond=0)
        expires_at = now + timedelta(hours=expires_in_hours)
        delegation_id = str(uuid.uuid4())
        merchants = sorted(set(merchants))
        document = passport.delegation_document(
            delegation_id=delegation_id, user_id=user_id, device_id=device_id, merchants=merchants,
            max_order_minor=max_order_minor, currency=settings.default_currency, issued_at=now,
            expires_at=expires_at, key_id=self.key_id,
        )
        signature = self._sign(document)
        assert signature is not None
        try:
            result = await call_rpc_with_retry("create_agent_delegation", lambda: self._repo.create_delegation(
                delegation_id=delegation_id, user_id=user_id, device_id=device_id, merchants=merchants,
                max_order_minor=max_order_minor, currency=settings.default_currency, expires_at=expires_at,
                key_id=self.key_id, document=document.decode("ascii"), signature=signature,
            ))
        except RepositoryError as exc:
            if exc.code == "23505":
                raise Conflict("delegation_exists", "This device already has an active passport. Revoke it first.") from None
            raise
        if not result.ok:
            if result.reason == "delegation_exists":
                raise Conflict("delegation_exists", "This device already has an active passport. Revoke it first.")
            if result.reason == "device_revoked":
                raise Conflict("device_revoked", "The device is revoked.")
            raise ApiError(404, "not_found", "Device not found.")
        return {
            "id": delegation_id, "device_id": device_id, "status": "active", "merchants": merchants,
            "max_order_minor": max_order_minor, "currency": settings.default_currency,
            "expires_at": expires_at, "revoked_at": None, "created_at": now, "key_id": self.key_id,
            "document": document.decode("ascii"), "signature": signature,
        }

    async def revoke(self, *, delegation_id: str, user_id: str) -> dict[str, Any]:
        result = await call_rpc_with_retry(
            "revoke_agent_delegation",
            lambda: self._repo.revoke_delegation(delegation_id=delegation_id, user_id=user_id),
        )
        if not result.ok:
            raise ApiError(404, "not_found", "Passport not found.")
        return {"id": delegation_id, "status": "revoked"}

    async def list(self, user_id: str) -> list[dict[str, Any]]:
        now = self._clock.utc_now()
        return [passport.public_fields(row, now) for row in await self._repo.list_for_user(user_id)]

    async def receipts(self, user_id: str, *, limit: int) -> list[dict[str, Any]]:
        return await self._repo.list_receipts(user_id, limit=limit)

    # -- enforcement (create_order) --------------------------------------------------

    async def _grant(self, device: DeviceContext) -> passport.Grant | None:
        try:
            row = await self._repo.latest_for_device(device.device_id)
        except RepositoryError as exc:
            # Fail closed: an unreadable grant must not let an order through.
            logger.error("Passport lookup failed: %s", type(exc).__name__)
            raise ToolError("internal_error") from None
        if row is None:
            return None
        return passport.Grant(row["id"], tuple(row["merchants"]), int(row["max_order_minor"]),
                              row["currency"], row["expires_at"], row.get("revoked_at"))

    async def authorize_merchant(self, device: DeviceContext, merchant: str, currency: str) -> passport.Grant | None:
        """Check before quoting. Returns the governing grant, or None for an ungoverned device."""
        grant = await self._grant(device)
        if grant is None and not self._settings.agent_passport_required:
            return None
        reason = passport.check_merchant(grant, merchant, self._clock.utc_now())
        if reason is not None:
            await self._deny(device, grant, reason, merchant, None, currency)
        return grant

    async def authorize_amount(self, device: DeviceContext, grant: passport.Grant | None, merchant: str,
                               amount_minor: int, currency: str) -> None:
        if grant is None:
            return
        reason = passport.check_amount(grant, amount_minor, currency)
        if reason is not None:
            await self._deny(device, grant, reason, merchant, amount_minor, currency)

    async def record_allowed(self, device: DeviceContext, grant: passport.Grant | None, *, order_id: str,
                             merchant: str, amount_minor: int, currency: str) -> None:
        if grant is not None:
            await self._receipt(device, grant, "allowed", "within_delegation", merchant, amount_minor,
                                currency, order_id)

    async def _deny(self, device: DeviceContext, grant: passport.Grant | None, reason: str, merchant: str,
                    amount_minor: int | None, currency: str) -> None:
        logger.info("Order refused by agent passport: %s", reason)
        await self._receipt(device, grant, "denied", reason, merchant, amount_minor, currency, None)
        raise ToolError("delegation_denied", DENIAL_MESSAGES[reason])

    async def _receipt(self, device: DeviceContext, grant: passport.Grant | None, decision: str, reason: str,
                       merchant: str, amount_minor: int | None, currency: str, order_id: str | None) -> None:
        receipt_id = str(uuid.uuid4())
        now = self._clock.utc_now().replace(microsecond=0)
        document = passport.receipt_document(
            receipt_id=receipt_id, delegation_id=grant.id if grant else None, device_id=device.device_id,
            order_id=order_id, decision=decision, reason=reason, merchant=merchant,
            amount_minor=amount_minor, currency=currency, issued_at=now, key_id=self.key_id,
        )
        row = {
            "id": receipt_id, "user_id": device.user_id, "delegation_id": grant.id if grant else None,
            "device_id": device.device_id, "order_id": order_id, "decision": decision, "reason": reason,
            "merchant_code": merchant, "amount_minor": amount_minor, "currency": currency,
            "key_id": self.key_id, "document": document.decode("ascii"), "signature": self._sign(document),
        }
        try:
            await self._repo.insert_receipt(row)
        except Exception as exc:
            logger.warning("Passport receipt write failed: %s", type(exc).__name__)
