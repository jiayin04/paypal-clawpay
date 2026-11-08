"""PayPalGateway that delegates every PayPal call to the PayPal agent over A2A.

Failure mapping keeps the direct-mode guarantees of the classifier (§3.7):

- the agent reports PayPal's own error (``kind=api``)  -> ``PayPalApiError``
  (the only kind that can classify as ``failed`` and release a reservation);
- the agent reports an OAuth failure (``kind=auth``)  -> ``PayPalAuthError``;
- anything else (agent down, timeout, 401 from the agent, malformed or
  mismatched reply, ``unavailable``/``invalid``/``bad_request``)
  -> ``PayPalUnavailable``, i.e. uncertain for a create/capture and no result
  for a poll. An agent problem can therefore never release spending.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.core.metrics import METRICS
from app.integrations.a2a.client import A2AError, send_message
from app.integrations.paypal.errors import PayPalApiError, PayPalAuthError, PayPalUnavailable
from app.integrations.paypal.models import PaymentToken, ProviderOrder, SetupToken
from app.schemas import paypal_agent as contract


logger = logging.getLogger(__name__)


class A2APayPalGateway:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not settings.paypal_agent_endpoint or settings.paypal_agent_token is None:
            raise ValueError("PAYPAL_AGENT_ENDPOINT and PAYPAL_AGENT_TOKEN must both be set")
        self._url = settings.paypal_agent_endpoint
        self._token = settings.paypal_agent_token.get_secret_value()
        # The agent may make two PayPal calls (401 refresh + retry); callers add
        # their own outer budgets (inline approval, reconciler per attempt).
        self._timeout = settings.paypal_http_timeout_seconds * 2 + 2
        self._transport = transport

    async def _call(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        correlation_id = str(uuid.uuid4())
        data = {"type": contract.REQUEST_TYPE, "correlation_id": correlation_id,
                "operation": operation, "params": params}
        label = f"agent:{operation}"
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
                reply = await send_message(client, self._url, correlation_id, data, self._timeout,
                                           headers={"Authorization": f"Bearer {self._token}"})
        except A2AError as exc:
            METRICS.paypal_requests.inc(label, f"agent_{exc.outcome}")
            logger.warning("PayPal agent %s failed: %s/%s", operation, exc.outcome, exc.reason)
            raise PayPalUnavailable(f"PayPal agent {exc.outcome}") from None
        finally:
            METRICS.paypal_duration.observe(time.perf_counter() - started, label)
        try:
            response = contract.AgentResponse.model_validate(reply)
        except ValidationError:
            METRICS.paypal_requests.inc(label, "agent_rejected")
            logger.error("PayPal agent %s returned an invalid response", operation)
            raise PayPalUnavailable("PayPal agent response invalid") from None
        if (response.type != contract.RESPONSE_TYPE or response.correlation_id != correlation_id
                or response.operation != operation):
            METRICS.paypal_requests.inc(label, "agent_rejected")
            logger.error("PayPal agent %s response does not match the request", operation)
            raise PayPalUnavailable("PayPal agent response mismatch")
        if response.ok:
            METRICS.paypal_requests.inc(label, "ok")
            return response.result or {}
        error = response.error
        kind = error.kind if error else "invalid"
        METRICS.paypal_requests.inc(label, f"error_{kind}")
        if kind == "api" and error is not None and error.status is not None and error.name:
            raise PayPalApiError(status=error.status, name=error.name, issue=error.issue, debug_id=error.debug_id)
        if kind == "auth":
            raise PayPalAuthError("PayPal agent OAuth failure")
        if kind == "bad_request":
            logger.error("PayPal agent rejected %s parameters", operation)
        raise PayPalUnavailable(f"PayPal agent error {kind}")

    @staticmethod
    def _parse(parse: Any, result: dict[str, Any]) -> Any:
        try:
            return parse(result)
        except (KeyError, TypeError, ValueError):
            logger.error("PayPal agent result could not be parsed")
            raise PayPalUnavailable("PayPal agent result invalid") from None

    async def ensure_access_token(self) -> str:
        await self._call("ensure_access_token", {})
        return "agent"  # the real token stays inside the agent

    async def create_setup_token(self, *, return_url: str, cancel_url: str, request_id: str) -> SetupToken:
        result = await self._call("create_setup_token",
                                  {"return_url": return_url, "cancel_url": cancel_url, "request_id": request_id})
        return self._parse(contract.setup_token_from_dict, result)

    async def get_setup_token(self, setup_token_id: str) -> SetupToken:
        result = await self._call("get_setup_token", {"setup_token_id": setup_token_id})
        return self._parse(contract.setup_token_from_dict, result)

    async def create_payment_token(self, *, setup_token_id: str, request_id: str) -> PaymentToken:
        result = await self._call("create_payment_token",
                                  {"setup_token_id": setup_token_id, "request_id": request_id})
        return self._parse(contract.payment_token_from_dict, result)

    async def delete_payment_token(self, payment_token_id: str) -> None:
        await self._call("delete_payment_token", {"payment_token_id": payment_token_id})

    async def create_order_with_vault(
        self, *, request_id: str, order_ref: str, invoice_id: str, description: str,
        amount_minor: int, currency: str, vault_id: str,
    ) -> ProviderOrder:
        result = await self._call("create_order_with_vault", {
            "request_id": request_id, "order_ref": order_ref, "invoice_id": invoice_id,
            "description": description, "amount_minor": amount_minor, "currency": currency,
            "vault_id": vault_id,
        })
        return self._parse(contract.provider_order_from_dict, result)

    async def capture_order(self, *, provider_order_id: str, request_id: str) -> ProviderOrder:
        result = await self._call("capture_order",
                                  {"provider_order_id": provider_order_id, "request_id": request_id})
        return self._parse(contract.provider_order_from_dict, result)

    async def get_order(self, provider_order_id: str) -> ProviderOrder:
        result = await self._call("get_order", {"provider_order_id": provider_order_id})
        return self._parse(contract.provider_order_from_dict, result)

    async def verify_webhook(self, *, headers: Mapping[str, str], raw_body: bytes, webhook_id: str) -> bool:
        result = await self._call("verify_webhook", {
            # Only PayPal's transmission headers; nothing else from the inbound request.
            "headers": {k.lower(): v for k, v in headers.items() if k.lower().startswith("paypal-")},
            "raw_body_b64": base64.b64encode(raw_body).decode("ascii"),
            "webhook_id": webhook_id,
        })
        verified = result.get("verified")
        if not isinstance(verified, bool):
            raise PayPalUnavailable("PayPal agent verify result invalid")
        return verified
