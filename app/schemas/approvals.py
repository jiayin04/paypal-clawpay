"""Schemas for approval challenges and gadget approvals (contracts.md §6.2, §6.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApprovalChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str
    order_id: str
    canonical_b64url: str
    canonical: str
    nonce: str
    key_version: int
    expires_at: datetime


class GadgetApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: UUID
    device_id: UUID
    key_version: int = Field(ge=1)
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    decision: Literal["approved", "denied"]
    signature: str | None = None


class GadgetApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str | None = None
    approval: str
    status: str
    message: str | None = None
    # Merchant handoff (orders.fulfillment_status) when the payment result is known.
    fulfillment_status: str | None = None
