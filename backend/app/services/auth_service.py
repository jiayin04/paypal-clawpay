"""Supabase JWT verification and admin-token check (design §1.4).

HS256 only when SUPABASE_JWT_SECRET is set (legacy projects); otherwise
ES256/RS256 via the project JWKS. The key resolver is injectable for tests.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

import anyio
import jwt

from app.core.config import Settings
from app.core.exceptions import ServiceUnavailable, Unauthorized
from app.core.security import constant_time_equals


logger = logging.getLogger(__name__)

ASYMMETRIC_ALGORITHMS = ["ES256", "RS256"]
LEEWAY_SECONDS = 30

KeyResolver = Callable[[str], Any]  # token -> verification key (sync; run in a thread)


class AuthUnavailable(Exception):
    """JWKS could not be fetched."""


def _default_resolver(settings: Settings) -> KeyResolver | None:
    url = settings.supabase_jwks_url
    if url is None:
        return None
    client = jwt.PyJWKClient(url, cache_keys=True, lifespan=600)

    def resolve(token: str) -> Any:
        try:
            return client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError as exc:
            raise AuthUnavailable() from exc

    return resolve


class AuthService:
    def __init__(self, settings: Settings, key_resolver: KeyResolver | None = None) -> None:
        self._settings = settings
        self._resolver = key_resolver
        self._default_built = key_resolver is not None

    def _get_resolver(self) -> KeyResolver | None:
        if not self._default_built:
            self._resolver = _default_resolver(self._settings)
            self._default_built = True
        return self._resolver

    def is_admin_token(self, token: str | None) -> bool:
        configured = self._settings.internal_service_token
        if not token or configured is None:
            return False
        return constant_time_equals(token, configured.get_secret_value())

    async def verify_user_jwt(self, token: str) -> str:
        """Return the verified ``sub`` (user id) or raise Unauthorized / ServiceUnavailable."""
        settings = self._settings
        issuer = settings.supabase_jwt_issuer
        if issuer is None:
            raise ServiceUnavailable("Authentication is not configured.", "auth_unavailable")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise Unauthorized() from None
        alg = header.get("alg")

        if settings.supabase_jwt_secret is not None:
            if alg != "HS256":
                raise Unauthorized()
            key: Any = settings.supabase_jwt_secret.get_secret_value()
            algorithms = ["HS256"]
        else:
            if alg not in ASYMMETRIC_ALGORITHMS:
                raise Unauthorized()  # covers alg:none and HS256 without a secret
            resolver = self._get_resolver()
            if resolver is None:
                raise ServiceUnavailable("Authentication is not configured.", "auth_unavailable")
            try:
                key = await anyio.to_thread.run_sync(resolver, token)
            except AuthUnavailable:
                logger.warning("JWKS fetch failed")
                raise ServiceUnavailable("Authentication is unavailable.", "auth_unavailable") from None
            except jwt.PyJWTError:
                raise Unauthorized() from None
            except Exception as exc:
                logger.warning("JWKS key resolution failed: %s", type(exc).__name__)
                raise ServiceUnavailable("Authentication is unavailable.", "auth_unavailable") from None
            algorithms = ASYMMETRIC_ALGORITHMS

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience="authenticated",
                issuer=issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError:
            raise Unauthorized() from None
        if claims.get("role") != "authenticated":
            raise Unauthorized()
        try:
            return str(uuid.UUID(str(claims["sub"])))
        except ValueError:
            raise Unauthorized() from None
