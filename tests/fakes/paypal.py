"""Scriptable MockTransport handler for PayPal Sandbox APIs (§3.7, AC3.4).

Simulates:
- OAuth client_credentials token issuance, token expiration, 401 errors, and timeouts.
- Vault v3.4 setup-tokens and payment-tokens creation, get, and delete.
- Orders v2 checkout/orders creation with vault, capture, and get.
- Webhook signature verification.
- Call logging: records (method, path, headers, body) for assertions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx


@dataclass
class PayPalCall:
    """Recorded HTTP request received by FakePayPalTransport."""

    method: str
    path: str
    headers: dict[str, str]
    body: Any


class FakePayPalTransport(httpx.AsyncBaseTransport):
    """Configurable in-memory MockTransport for PayPal Sandbox tests."""

    def __init__(self) -> None:
        self.calls: list[PayPalCall] = []

        # Modes:
        self.oauth_status: int = 200
        self.oauth_timeout: bool = False
        self.oauth_401_count: int = 0  # Number of times to return 401 on API calls before succeeding

        # Vault setup token
        self.setup_token_mode: str = "success"  # "success", "invalid_host", "error_422"
        self.setup_token_status: str = "APPROVED"  # for get_setup_token

        # Vault payment token
        self.payment_token_mode: str = "success"  # "success", "error_422"

        # Orders create mode
        # Options: "completed", "pending", "declined", "payer_action_required",
        #          "approved_no_capture", "duplicate_invoice", "instrument_declined",
        #          "server_error", "timeout"
        self.orders_mode: str = "completed"

        # Orders capture mode
        self.capture_mode: str = "completed"  # "completed", "declined", "timeout", "server_error"

        # Orders get mode
        self.get_order_mode: str = "completed"

        # Webhook verify mode
        self.webhook_verify_success: bool = True

        # Custom override handlers: path -> (status_code, json_dict)
        self.custom_routes: dict[str, tuple[int, dict[str, Any]]] = {}

    def record_call(self, request: httpx.Request, body: Any) -> None:
        parsed = urlparse(str(request.url))
        self.calls.append(
            PayPalCall(
                method=request.method,
                path=parsed.path,
                headers=dict(request.headers),
                body=body,
            )
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        path = parsed.path

        # Parse body
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content.decode("utf-8"))
            except Exception:
                body = request.content.decode("utf-8", errors="replace")

        self.record_call(request, body)

        # 1. Custom route overrides
        if path in self.custom_routes:
            status, data = self.custom_routes[path]
            return httpx.Response(status, json=data, request=request)

        # 2. OAuth token endpoint
        if path == "/v1/oauth2/token":
            if self.oauth_timeout:
                raise httpx.TimeoutException("OAuth request timed out", request=request)
            if self.oauth_status != 200:
                return httpx.Response(
                    self.oauth_status,
                    json={"error": "invalid_client", "error_description": "Client authentication failed"},
                    headers={"PayPal-Debug-Id": "dbg-oauth-err"},
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "scope": "https://api-m.paypal.com/v1/vault/payment-tokens",
                    "access_token": "fake-access-token-12345",
                    "token_type": "Bearer",
                    "app_id": "APP-80W284485P519543T",
                    "expires_in": 32400,
                    "nonce": "fake-oauth-nonce",
                },
                request=request,
            )

        # 3. Simulate transient 401 on API endpoints
        if self.oauth_401_count > 0:
            self.oauth_401_count -= 1
            return httpx.Response(
                401,
                json={"name": "AUTHENTICATION_FAILURE", "message": "Authentication failed."},
                headers={"PayPal-Debug-Id": "dbg-401-transient"},
                request=request,
            )

        # 4. Vault Setup Tokens: POST /v3/vault/setup-tokens
        if path == "/v3/vault/setup-tokens" and request.method == "POST":
            if self.setup_token_mode == "invalid_host":
                return httpx.Response(
                    201,
                    json={
                        "id": "ST-INVALID-HOST",
                        "status": "CREATED",
                        "links": [{"rel": "approve", "href": "https://evil.attacker.com/approve"}],
                    },
                    request=request,
                )
            if self.setup_token_mode == "error_422":
                return httpx.Response(
                    422,
                    json={
                        "name": "UNPROCESSABLE_ENTITY",
                        "details": [{"issue": "INVALID_PARAMETER_VALUE"}],
                        "debug_id": "dbg-setup-422",
                    },
                    headers={"PayPal-Debug-Id": "dbg-setup-422"},
                    request=request,
                )
            return httpx.Response(
                201,
                json={
                    "id": "ST-SETUP-TOKEN-001",
                    "status": "CREATED",
                    "links": [
                        {"rel": "approve", "href": "https://www.sandbox.paypal.com/agreements/approve?ba_token=BA-001"}
                    ],
                },
                request=request,
            )

        # 5. Vault Setup Tokens: GET /v3/vault/setup-tokens/{id}
        if path.startswith("/v3/vault/setup-tokens/") and request.method == "GET":
            st_id = path.split("/")[-1]
            return httpx.Response(
                200,
                json={
                    "id": st_id,
                    "status": self.setup_token_status,
                    "links": [
                        {"rel": "approve", "href": "https://www.sandbox.paypal.com/agreements/approve?ba_token=BA-001"}
                    ],
                },
                request=request,
            )

        # 6. Vault Payment Tokens: POST /v3/vault/payment-tokens
        if path == "/v3/vault/payment-tokens" and request.method == "POST":
            if self.payment_token_mode == "error_422":
                return httpx.Response(
                    422,
                    json={
                        "name": "UNPROCESSABLE_ENTITY",
                        "details": [{"issue": "SETUP_TOKEN_NOT_APPROVED"}],
                        "debug_id": "dbg-pt-422",
                    },
                    headers={"PayPal-Debug-Id": "dbg-pt-422"},
                    request=request,
                )
            return httpx.Response(
                201,
                json={
                    "id": "PT-VAULT-TOKEN-999",
                    "customer": {"id": "CUST-001"},
                    "status": "VAULTED",
                },
                request=request,
            )

        # 7. Vault Payment Tokens: DELETE /v3/vault/payment-tokens/{id}
        if path.startswith("/v3/vault/payment-tokens/") and request.method == "DELETE":
            return httpx.Response(204, request=request)

        # 8. Orders v2: POST /v2/checkout/orders
        if path == "/v2/checkout/orders" and request.method == "POST":
            # Parse amount/currency from request body early
            amount_val = "13.80"
            currency = "MYR"
            if isinstance(body, dict):
                pus = body.get("purchase_units", [])
                if pus and isinstance(pus[0], dict):
                    amt = pus[0].get("amount", {})
                    amount_val = amt.get("value", "13.80")
                    currency = amt.get("currency_code", "MYR")
            self._last_order_amount = amount_val
            self._last_order_currency = currency

            if self.orders_mode == "timeout":
                raise httpx.TimeoutException("Orders create timed out", request=request)
            if self.orders_mode == "server_error":
                return httpx.Response(
                    500,
                    json={"name": "INTERNAL_SERVER_ERROR", "debug_id": "dbg-ord-500"},
                    headers={"PayPal-Debug-Id": "dbg-ord-500"},
                    request=request,
                )
            if self.orders_mode == "duplicate_invoice":
                return httpx.Response(
                    422,
                    json={
                        "name": "UNPROCESSABLE_ENTITY",
                        "details": [{"issue": "DUPLICATE_INVOICE_ID"}],
                        "debug_id": "dbg-dup-inv",
                    },
                    headers={"PayPal-Debug-Id": "dbg-dup-inv"},
                    request=request,
                )
            if self.orders_mode == "instrument_declined":
                return httpx.Response(
                    422,
                    json={
                        "name": "UNPROCESSABLE_ENTITY",
                        "details": [{"issue": "INSTRUMENT_DECLINED"}],
                        "debug_id": "dbg-declined",
                    },
                    headers={"PayPal-Debug-Id": "dbg-declined"},
                    request=request,
                )
            if self.orders_mode == "payer_action_required":
                return httpx.Response(
                    201,
                    json={"id": "ORD-PAR-001", "status": "PAYER_ACTION_REQUIRED"},
                    request=request,
                )
            if self.orders_mode == "approved_no_capture":
                return httpx.Response(
                    201,
                    json={"id": "ORD-APPR-001", "status": "APPROVED", "purchase_units": [{"reference_id": "default"}]},
                    request=request,
                )

            # Determine capture status based on orders_mode
            cap_status = "COMPLETED"
            reason = None
            if self.orders_mode == "pending":
                cap_status = "PENDING"
            elif self.orders_mode == "declined":
                cap_status = "DECLINED"
                reason = "TRANSACTION_REFUSED"

            return httpx.Response(
                201,
                json={
                    "id": "ORD-SANDBOX-001",
                    "status": "COMPLETED",
                    "purchase_units": [
                        {
                            "reference_id": "default",
                            "payments": {
                                "captures": [
                                    {
                                        "id": "CAP-001",
                                        "status": cap_status,
                                        "status_details": {"reason": reason} if reason else {},
                                        "amount": {"currency_code": currency, "value": amount_val},
                                    }
                                ]
                            },
                        }
                    ],
                },
                request=request,
            )

        # 9. Orders Capture: POST /v2/checkout/orders/{id}/capture
        if path.startswith("/v2/checkout/orders/") and path.endswith("/capture") and request.method == "POST":
            ord_id = path.split("/")[4]
            cap_amt = getattr(self, "_last_order_amount", "13.80")
            cap_curr = getattr(self, "_last_order_currency", "MYR")
            if self.capture_mode == "timeout":
                raise httpx.TimeoutException("Capture timed out", request=request)
            if self.capture_mode == "server_error":
                return httpx.Response(
                    500,
                    json={"name": "INTERNAL_SERVER_ERROR", "debug_id": "dbg-cap-500"},
                    headers={"PayPal-Debug-Id": "dbg-cap-500"},
                    request=request,
                )
            if self.capture_mode == "declined":
                return httpx.Response(
                    201,
                    json={
                        "id": ord_id,
                        "status": "COMPLETED",
                        "purchase_units": [
                            {
                                "payments": {
                                    "captures": [
                                        {
                                            "id": "CAP-DECLINED-001",
                                            "status": "DECLINED",
                                            "status_details": {"reason": "PAYMENT_SOURCE_DECLINED"},
                                            "amount": {"currency_code": cap_curr, "value": cap_amt},
                                        }
                                    ]
                                }
                            }
                        ],
                    },
                    request=request,
                )
            return httpx.Response(
                201,
                json={
                    "id": ord_id,
                    "status": "COMPLETED",
                    "purchase_units": [
                        {
                            "payments": {
                                "captures": [
                                    {
                                        "id": "CAP-EXPLICIT-001",
                                        "status": "COMPLETED",
                                        "amount": {"currency_code": cap_curr, "value": cap_amt},
                                    }
                                ]
                            }
                        }
                    ],
                },
                request=request,
            )

        # 10. Orders Get: GET /v2/checkout/orders/{id}
        if path.startswith("/v2/checkout/orders/") and request.method == "GET":
            ord_id = path.split("/")[-1]
            return httpx.Response(
                200,
                json={
                    "id": ord_id,
                    "status": "COMPLETED",
                    "purchase_units": [
                        {
                            "payments": {
                                "captures": [
                                    {
                                        "id": "CAP-GET-001",
                                        "status": "COMPLETED",
                                        "amount": {"currency_code": "MYR", "value": "13.80"},
                                    }
                                ]
                            }
                        }
                    ],
                },
                request=request,
            )

        # 11. Webhook signature verification: POST /v1/notifications/verify-webhook-signature
        if path == "/v1/notifications/verify-webhook-signature":
            status_str = "SUCCESS" if self.webhook_verify_success else "FAILURE"
            return httpx.Response(200, json={"verification_status": status_str}, request=request)

        # Fallback 404
        return httpx.Response(404, json={"message": f"Not found: {path}"}, request=request)
