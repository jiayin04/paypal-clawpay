"""Device registry request/response models."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints


UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _canonical_uuid(value: str) -> str:
    return str(UUID(value))


def _printable(value: str) -> str:
    if not value.isprintable():
        raise ValueError("must be printable")
    return value


UuidStr = Annotated[str, StringConstraints(pattern=UUID_RE), AfterValidator(_canonical_uuid)]


class RegisterDeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: UuidStr | None = None
    device_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64), AfterValidator(_printable)
    ]
    public_key_pem: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    key_version: Annotated[int, Field(ge=1, le=1000)] = 1


class RegisterDeviceResponse(BaseModel):
    device_id: str
    device_token: str
    llm_token: str
    key_algorithm: str
    key_version: int
    status: str


class RevokeDeviceResponse(BaseModel):
    device_id: str
    status: str
