"""A2A data contract between ClawPay and the PayPal agent.

Request (first ``data`` part of an A2A ``message/send``)::

    {"type": "clawpay.paypal_request.v1", "correlation_id": "<uuid4>",
     "operation": "create_order_with_vault", "params": {...}}

Response::

    {"type": "clawpay.paypal_response.v1", "correlation_id": "<same>", "operation": "<same>",
     "ok": true, "result": {...}}
    {"type": "clawpay.paypal_response.v1", ..., "ok": false,
     "error": {"kind": "api", "status": 422, "name": "UNPROCESSABLE_ENTITY",
               "issue": "INSTRUMENT_DECLINED", "debug_id": "..."}}

``kind`` tells ClawPay how to classify the failure. Only ``api`` (PayPal itself
answered with an error status) can ever release a reservation; ``auth``,
``unavailable``, ``invalid`` and ``bad_request`` are treated like a network
failure (uncertain / no result), exactly as in direct mode.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.integrations.paypal.models import PaymentToken, ProviderOrder, SetupToken
from app.schemas.devices import UuidStr

REQUEST_TYPE = "clawpay.paypal_request.v1"
RESPONSE_TYPE = "clawpay.paypal_response.v1"

Operation = Literal[
    "ensure_access_token",
    "create_setup_token",
    "get_setup_token",
    "create_payment_token",
    "delete_payment_token",
    "create_order_with_vault",
    "capture_order",
    "get_order",
    "verify_webhook",
]
ErrorKind = Literal["api", "auth", "unavailable", "invalid", "bad_request"]

# PayPal-Request-Id allows up to 108 characters.
RequestId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,108}$")]
ProviderId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Url = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=127)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NoParams(_Strict):
    pass


class SetupTokenParams(_Strict):
    return_url: Url
    cancel_url: Url
    request_id: RequestId


class SetupTokenIdParams(_Strict):
    setup_token_id: ProviderId


class PaymentTokenParams(_Strict):
    setup_token_id: ProviderId
    request_id: RequestId


class PaymentTokenIdParams(_Strict):
    payment_token_id: ProviderId


class CreateOrderParams(_Strict):
    request_id: RequestId
    order_ref: UuidStr
    invoice_id: ShortText
    description: ShortText
    amount_minor: Annotated[int, Field(ge=1, le=10_000_000)]
    currency: Literal["MYR", "USD"]
    vault_id: ProviderId


class CaptureParams(_Strict):
    provider_order_id: ProviderId
    request_id: RequestId


class OrderIdParams(_Strict):
    provider_order_id: ProviderId


class VerifyWebhookParams(_Strict):
    headers: Annotated[dict[str, Annotated[str, StringConstraints(max_length=2048)]], Field(max_length=20)]
    raw_body_b64: Annotated[str, StringConstraints(min_length=1, max_length=360_000)]
    webhook_id: ShortText


PARAMS: dict[str, type[_Strict]] = {
    "ensure_access_token": NoParams,
    "create_setup_token": SetupTokenParams,
    "get_setup_token": SetupTokenIdParams,
    "create_payment_token": PaymentTokenParams,
    "delete_payment_token": PaymentTokenIdParams,
    "create_order_with_vault": CreateOrderParams,
    "capture_order": CaptureParams,
    "get_order": OrderIdParams,
    "verify_webhook": VerifyWebhookParams,
}


class AgentRequest(_Strict):
    type: Literal["clawpay.paypal_request.v1"]
    correlation_id: UuidStr
    operation: Operation
    params: dict[str, Any] = Field(default_factory=dict)


class AgentError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ErrorKind
    status: int | None = None
    name: str | None = Field(None, max_length=127)
    issue: str | None = Field(None, max_length=127)
    debug_id: str | None = Field(None, max_length=127)


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["clawpay.paypal_response.v1"]
    correlation_id: str
    operation: Operation
    ok: bool
    result: dict[str, Any] | None = None
    error: AgentError | None = None


# -- result (de)serialisation ------------------------------------------------------


def setup_token_to_dict(token: SetupToken) -> dict[str, Any]:
    return {"id": token.id, "status": token.status, "approve_url": token.approve_url}


def setup_token_from_dict(data: dict[str, Any]) -> SetupToken:
    return SetupToken(id=str(data["id"]), status=str(data["status"]), approve_url=data.get("approve_url"))


def payment_token_to_dict(token: PaymentToken) -> dict[str, Any]:
    return {"id": token.id, "customer_id": token.customer_id, "payer_email": token.payer_email}


def payment_token_from_dict(data: dict[str, Any]) -> PaymentToken:
    return PaymentToken(id=str(data["id"]), customer_id=data.get("customer_id"), payer_email=data.get("payer_email"))


_ORDER_FIELDS = (
    "id", "status", "capture_id", "capture_status", "capture_status_reason",
    "capture_amount_minor", "capture_currency", "capture_amount_value",
)


def provider_order_to_dict(order: ProviderOrder) -> dict[str, Any]:
    return {name: getattr(order, name) for name in _ORDER_FIELDS}


def provider_order_from_dict(data: dict[str, Any]) -> ProviderOrder:
    amount = data.get("capture_amount_minor")
    if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
        raise ValueError("capture_amount_minor must be an integer")
    values = {name: data.get(name) for name in _ORDER_FIELDS}
    for name, value in values.items():
        if name != "capture_amount_minor" and value is not None and not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
    if not values["id"] or not values["status"]:
        raise ValueError("id and status are required")
    return ProviderOrder(**values)  # type: ignore[arg-type]
