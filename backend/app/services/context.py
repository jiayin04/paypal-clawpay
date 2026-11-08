"""Request-scoped identities passed from API dependencies into services."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceContext:
    """The authenticated device. Identity always comes from the token, never from args."""

    device_id: str
    user_id: str
    key_version: int
    public_key: str


@dataclass(frozen=True)
class UserContext:
    user_id: str
    jwt: str


@dataclass(frozen=True)
class AdminContext:
    actor_id: str = "internal_service_token"
