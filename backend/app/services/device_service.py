"""Device registry: registration, token issuance, revocation, token lookup (§1.3, §2.3).

Raw tokens exist only in the registration response body. Only
HMAC-SHA256(DEVICE_TOKEN_PEPPER, token) is stored. No lookup cache, so a
revocation takes effect on the next request.
"""

from __future__ import annotations

import logging
from typing import Any

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import Settings
from app.core.exceptions import ApiError, Conflict, ServiceUnavailable
from app.core.security import generate_device_token, generate_llm_token, hash_token
from app.repositories.errors import RepositoryError, call_rpc_with_retry
from app.repositories.ports.devices import DeviceRecord, DeviceRepository


logger = logging.getLogger(__name__)

KEY_ALGORITHM = "ecdsa-p256-sha256"
ACTIVE_KEY_CONSTRAINT = "devices_active_public_key_key"
TOKEN_HASH_CONSTRAINTS = frozenset({"devices_token_hash_key", "devices_llm_token_hash_key"})


def normalize_public_key(pem: str) -> str:
    """Return the key as SPKI PEM; P-256 only, else 400 invalid_public_key."""
    try:
        key = serialization.load_pem_public_key(pem.encode("ascii"))
    except (ValueError, TypeError, UnsupportedAlgorithm, UnicodeEncodeError):
        raise ApiError(400, "invalid_public_key", "public_key_pem must be a P-256 SPKI PEM key.") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ApiError(400, "invalid_public_key", "public_key_pem must be a P-256 SPKI PEM key.")
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


class DeviceService:
    def __init__(self, settings: Settings, devices: DeviceRepository) -> None:
        self._settings = settings
        self._devices = devices

    def _pepper(self) -> str:
        pepper = self._settings.device_token_pepper
        if pepper is None or not pepper.get_secret_value():
            raise ServiceUnavailable("Device authentication is not configured.", "device_auth_not_configured")
        return pepper.get_secret_value()

    def hash(self, token: str) -> str:
        return hash_token(self._pepper(), token)

    async def lookup(self, token: str, kind: str) -> DeviceRecord | None:
        token_hash = self.hash(token)
        if kind == "llm":
            return await self._devices.find_by_llm_token_hash(token_hash)
        return await self._devices.find_by_token_hash(token_hash)

    async def register(
        self,
        *,
        user_id: str,
        device_name: str,
        public_key_pem: str,
        key_version: int,
        actor_type: str,
        actor_id: str | None,
    ) -> dict[str, Any]:
        public_key = normalize_public_key(public_key_pem)
        pepper = self._pepper()
        for attempt in range(2):
            device_token = generate_device_token()
            llm_token = generate_llm_token()
            try:
                result = await call_rpc_with_retry(
                    "register_device",
                    lambda: self._devices.register(
                        user_id=user_id,
                        device_name=device_name,
                        public_key=public_key,
                        key_version=key_version,
                        token_hash=hash_token(pepper, device_token),
                        llm_token_hash=hash_token(pepper, llm_token),
                        actor_type=actor_type,
                        actor_id=actor_id,
                    ),
                )
            except RepositoryError as exc:
                if exc.code == "23505" and exc.constraint == ACTIVE_KEY_CONSTRAINT:
                    raise Conflict(
                        "public_key_already_registered", "This public key is already registered."
                    ) from None
                if exc.code == "23505" and exc.constraint in TOKEN_HASH_CONSTRAINTS and attempt == 0:
                    continue  # practically impossible with 256-bit tokens; retry with fresh tokens
                raise ServiceUnavailable() from None
            if not result.ok:
                if result.reason == "user_not_found":
                    raise ApiError(404, "user_not_found", "User not found.")
                if result.reason == "public_key_already_registered":
                    raise Conflict(
                        "public_key_already_registered", "This public key is already registered."
                    )
                raise ServiceUnavailable()
            logger.info("Device registered: %s", result.data["device_id"])
            return {
                "device_id": str(result.data["device_id"]),
                "device_token": device_token,
                "llm_token": llm_token,
                "key_algorithm": KEY_ALGORITHM,
                "key_version": key_version,
                "status": "active",
            }
        raise ServiceUnavailable()

    async def revoke(
        self, device_id: str, *, user_id: str | None, actor_type: str, actor_id: str | None
    ) -> dict[str, Any]:
        result = await call_rpc_with_retry(
            "revoke_device",
            lambda: self._devices.revoke(
                device_id=device_id, user_id=user_id, actor_type=actor_type, actor_id=actor_id
            ),
        )
        if not result.ok:
            raise ApiError(404, "not_found", "Device not found.")
        logger.info(
            "Device revoked: %s (cancelled_orders=%s)", device_id, result.data.get("cancelled_orders", 0)
        )
        return {"device_id": device_id, "status": "revoked"}
