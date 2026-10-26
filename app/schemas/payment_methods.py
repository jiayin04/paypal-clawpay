"""Pydantic schemas for payment method linking endpoints (design §3.10)."""

from __future__ import annotations

from pydantic import BaseModel


class SetupPaymentMethodRequest(BaseModel):
    """POST /api/v1/payment-methods/setup request body."""

    return_url: str | None = None   # override; defaults to settings.app_public_url/return
    cancel_url: str | None = None   # override; defaults to return_url?cancelled=1


class SetupPaymentMethodResponse(BaseModel):
    payment_method_id: str
    approve_url: str
    expires_at: str


class PaymentMethodItem(BaseModel):
    id: str
    status: str
    payer_email_masked: str | None = None
    created_at: str | None = None


class ListPaymentMethodsResponse(BaseModel):
    payment_methods: list[PaymentMethodItem]
