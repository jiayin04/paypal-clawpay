"""Test keys generated in memory (never written to disk)."""

from __future__ import annotations

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa


SUPABASE_URL = "https://fakeproject.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"


def p256_public_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def p384_public_pem() -> str:
    key = ec.generate_private_key(ec.SECP384R1())
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def rsa_public_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


class JwtSigner:
    """ES256 signer plus a fake JWKS resolver for AuthService."""

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.public_key = self._key.public_key()

    def resolver(self, token: str) -> Any:
        return self.public_key

    def token(self, sub: str, /, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": sub,
            "aud": "authenticated",
            "iss": ISSUER,
            "role": "authenticated",
            "exp": now + 3600,
            "iat": now,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self._key, algorithm="ES256", headers={"kid": "test"})
