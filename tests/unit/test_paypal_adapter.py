"""Unit tests for PayPal Sandbox adapter (contracts.md §3.7, AC3.4).

Covers:
- OAuth token caching + asyncio.Lock.
- 401 clear-cache and single retry.
- OAuth failure / timeout -> PayPalUnavailable / PayPalAuthError.
- Vault v3.4 setup-tokens: creation, approve URL domain check, get, payment token creation, delete (404 ok).
- Orders v2: create with vault, capture, get, amount formatting via to_paypal_value, currency check.
- Webhook signature verification.
- Security: bodies never logged, debug_id captured on failure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.integrations.paypal import (
    PayPalApiError,
    PayPalAuthError,
    PayPalClient,
    PayPalService,
    PayPalUnavailable,
    to_paypal_value,
)
from tests.fakes.app import make_settings
from tests.fakes.paypal import FakePayPalTransport


def _make_client(
    transport: FakePayPalTransport,
    **settings_kwargs: Any,
) -> tuple[PayPalClient, PayPalService]:
    defaults = {
        "paypal_client_id": "test-client-id",
        "paypal_client_secret": "test-client-secret",
        "paypal_http_timeout_seconds": 5.0,
    }
    defaults.update(settings_kwargs)
    settings = make_settings(**defaults)
    client = PayPalClient(settings, transport=transport)
    service = PayPalService(client)
    return client, service


# ---------------------------------------------------------------------------
# OAuth Token Caching and Retry Logic
# ---------------------------------------------------------------------------


def test_oauth_token_caching() -> None:
    """Multiple API calls reuse cached OAuth token without re-querying /v1/oauth2/token."""
    transport = FakePayPalTransport()
    client, service = _make_client(transport)

    async def _test() -> None:
        # 1. First call fetches token
        st1 = await service.get_setup_token("ST-001")
        assert st1.id == "ST-001"

        # 2. Second call reuses token
        st2 = await service.get_setup_token("ST-002")
        assert st2.id == "ST-002"

        # Count OAuth calls in call log
        oauth_calls = [c for c in transport.calls if c.path == "/v1/oauth2/token"]
        assert len(oauth_calls) == 1

    asyncio.run(_test())


def test_401_retries_once_with_fresh_token() -> None:
    """A 401 response clears token cache and retries the request once with a new token."""
    transport = FakePayPalTransport()
    # Inject one 401 on the first API call
    transport.oauth_401_count = 1
    client, service = _make_client(transport)

    async def _test() -> None:
        # First API call triggers 401, re-fetches token, then succeeds
        st = await service.get_setup_token("ST-RETRY")
        assert st.id == "ST-RETRY"

        # Should have 2 OAuth calls (initial + retry)
        oauth_calls = [c for c in transport.calls if c.path == "/v1/oauth2/token"]
        assert len(oauth_calls) == 2

    asyncio.run(_test())


def test_persistent_401_raises_api_error() -> None:
    """If 401 persists after retry, PayPalApiError(401) is raised."""
    transport = FakePayPalTransport()
    transport.oauth_401_count = 5
    client, service = _make_client(transport)

    async def _test() -> None:
        with pytest.raises(PayPalApiError) as exc_info:
            await service.get_setup_token("ST-FAIL")
        assert exc_info.value.status == 401

    asyncio.run(_test())


def test_oauth_timeout_raises_unavailable() -> None:
    """Timeout during OAuth call raises PayPalUnavailable."""
    transport = FakePayPalTransport()
    transport.oauth_timeout = True
    client, service = _make_client(transport)

    async def _test() -> None:
        with pytest.raises(PayPalUnavailable):
            await service.get_setup_token("ST-FAIL")

    asyncio.run(_test())


def test_oauth_failure_status_raises_auth_error() -> None:
    """Non-200 OAuth status raises PayPalAuthError."""
    transport = FakePayPalTransport()
    transport.oauth_status = 401
    client, service = _make_client(transport)

    async def _test() -> None:
        with pytest.raises(PayPalAuthError):
            await service.get_setup_token("ST-FAIL")

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# Vault v3.4 Operations
# ---------------------------------------------------------------------------


def test_create_setup_token_success() -> None:
    """create_setup_token sends PayPal-Request-Id and returns SetupToken."""
    transport = FakePayPalTransport()
    client, service = _make_client(transport)

    async def _test() -> None:
        req_id = f"setup-{uuid4()}"
        st = await service.create_setup_token(
            return_url="http://localhost:8000/return",
            cancel_url="http://localhost:8000/cancel",
            request_id=req_id,
        )
        assert st.id == "ST-SETUP-TOKEN-001"
        assert st.status == "CREATED"
        assert st.approve_url is not None
        assert "sandbox.paypal.com" in st.approve_url

        # Check call headers and body
        post_calls = [c for c in transport.calls if c.path == "/v3/vault/setup-tokens"]
        assert len(post_calls) == 1
        call = post_calls[0]
        assert call.headers.get("paypal-request-id") == req_id
        assert call.body["payment_source"]["paypal"]["experience_context"]["return_url"] == "http://localhost:8000/return"

    asyncio.run(_test())


def test_create_setup_token_invalid_host_rejected() -> None:
    """approve_url with non-paypal.com host raises PayPalApiError."""
    transport = FakePayPalTransport()
    transport.setup_token_mode = "invalid_host"
    client, service = _make_client(transport)

    async def _test() -> None:
        with pytest.raises(PayPalApiError) as exc_info:
            await service.create_setup_token(
                return_url="http://localhost:8000/return",
                cancel_url="http://localhost:8000/cancel",
                request_id="req-123",
            )
        assert exc_info.value.status == 502
        assert exc_info.value.name == "INVALID_APPROVE_URL"

    asyncio.run(_test())


def test_create_payment_token_success() -> None:
    """create_payment_token exchanges setup token for payment token."""
    transport = FakePayPalTransport()
    client, service = _make_client(transport)

    async def _test() -> None:
        req_id = f"pt-{uuid4()}"
        pt = await service.create_payment_token(
            setup_token_id="ST-SETUP-TOKEN-001",
            request_id=req_id,
        )
        assert pt.id == "PT-VAULT-TOKEN-999"
        assert pt.customer_id == "CUST-001"

        post_calls = [c for c in transport.calls if c.path == "/v3/vault/payment-tokens"]
        assert len(post_calls) == 1
        call = post_calls[0]
        assert call.headers.get("paypal-request-id") == req_id
        assert call.body["payment_source"]["token"]["id"] == "ST-SETUP-TOKEN-001"

    asyncio.run(_test())


def test_delete_payment_token_success_and_404_idempotent() -> None:
    """delete_payment_token succeeds on 204 and treats 404 as success."""
    transport = FakePayPalTransport()
    client, service = _make_client(transport)

    async def _test() -> None:
        # 1. 204 success
        await service.delete_payment_token("PT-001")

        # 2. 404 treated as success
        transport.custom_routes["/v3/vault/payment-tokens/PT-404"] = (
            404,
            {"name": "RESOURCE_NOT_FOUND", "message": "Specified token not found"},
        )
        await service.delete_payment_token("PT-404")

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# Orders v2 Operations
# ---------------------------------------------------------------------------


def test_create_order_with_vault_amount_and_headers() -> None:
    """create_order_with_vault converts amount, sets headers, and returns ProviderOrder."""
    transport = FakePayPalTransport()
    client, service = _make_client(transport)

    async def _test() -> None:
        order = await service.create_order_with_vault(
            request_id="clawpay-ord-1",
            order_ref="ord-1",
            invoice_id="clawpay-ord-1",
            description="Grocer Demo order",
            amount_minor=1380,
            currency="MYR",
            vault_id="PT-VAULT-001",
        )
        assert order.id == "ORD-SANDBOX-001"
        assert order.status == "COMPLETED"
        assert order.capture_id == "CAP-001"
        assert order.capture_status == "COMPLETED"
        assert order.capture_amount_minor == 1380
        assert order.capture_currency == "MYR"

        post_calls = [c for c in transport.calls if c.path == "/v2/checkout/orders"]
        assert len(post_calls) == 1
        call = post_calls[0]
        assert call.headers.get("paypal-request-id") == "clawpay-ord-1"
        assert call.headers.get("prefer") == "return=representation"
        pu = call.body["purchase_units"][0]
        assert pu["amount"]["value"] == "13.80"
        assert pu["amount"]["currency_code"] == "MYR"
        assert call.body["payment_source"]["paypal"]["vault_id"] == "PT-VAULT-001"

    asyncio.run(_test())


def test_unsupported_currency_raises_value_error() -> None:
    """Currencies outside {MYR, USD} raise ValueError before any HTTP call."""
    transport = FakePayPalTransport()
    client, service = _make_client(transport)

    async def _test() -> None:
        with pytest.raises(ValueError, match="Unsupported PayPal currency"):
            await service.create_order_with_vault(
                request_id="req-1",
                order_ref="ord-1",
                invoice_id="inv-1",
                description="test",
                amount_minor=1000,
                currency="EUR",  # Unsupported
                vault_id="PT-001",
            )
        assert len(transport.calls) == 0  # No HTTP request made

    asyncio.run(_test())


def test_capture_order_and_get_order() -> None:
    """capture_order and get_order invoke expected endpoints."""
    transport = FakePayPalTransport()
    client, service = _make_client(transport)

    async def _test() -> None:
        cap_order = await service.capture_order(
            provider_order_id="ORD-123",
            request_id="clawpay-ord-123-capture",
        )
        assert cap_order.id == "ORD-123"
        assert cap_order.capture_id == "CAP-EXPLICIT-001"

        get_order = await service.get_order("ORD-123")
        assert get_order.id == "ORD-123"
        assert get_order.capture_status == "COMPLETED"

    asyncio.run(_test())


def test_verify_webhook_signature() -> None:
    """verify_webhook sends signature verification payload and returns bool."""
    transport = FakePayPalTransport()
    client, service = _make_client(transport)

    async def _test() -> None:
        headers = {
            "PAYPAL-AUTH-ALGO": "SHA256withRSA",
            "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-123",
            "PAYPAL-TRANSMISSION-ID": "trans-123",
            "PAYPAL-TRANSMISSION-SIG": "sig-123",
            "PAYPAL-TRANSMISSION-TIME": "2026-10-24T12:00:00Z",
        }
        raw_body = b'{"id":"WH-123","event_type":"PAYMENT.CAPTURE.COMPLETED"}'

        # 1. Success
        transport.webhook_verify_success = True
        ok = await service.verify_webhook(headers=headers, raw_body=raw_body, webhook_id="WHID-1")
        assert ok is True

        # 2. Failure
        transport.webhook_verify_success = False
        ok = await service.verify_webhook(headers=headers, raw_body=raw_body, webhook_id="WHID-1")
        assert ok is False

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# Security: Body Logging and Debug ID Capture
# ---------------------------------------------------------------------------


def test_client_never_logs_bodies_and_captures_debug_id(caplog: pytest.LogCaptureFixture) -> None:
    """Client never logs request/response bodies; logs debug_id on failure."""
    transport = FakePayPalTransport()
    transport.orders_mode = "instrument_declined"
    client, service = _make_client(transport)

    async def _test() -> None:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(PayPalApiError) as exc_info:
                await service.create_order_with_vault(
                    request_id="req-sec",
                    order_ref="ord-sec",
                    invoice_id="inv-sec",
                    description="Secret Item Description",
                    amount_minor=1380,
                    currency="MYR",
                    vault_id="PT-SECRET-VAULT-TOKEN",
                )
            assert exc_info.value.status == 422
            assert exc_info.value.issue == "INSTRUMENT_DECLINED"
            assert exc_info.value.debug_id == "dbg-declined"

        # Check logs: neither "PT-SECRET-VAULT-TOKEN" nor "Secret Item Description" appear in logs
        for record in caplog.records:
            msg = record.getMessage()
            assert "PT-SECRET-VAULT-TOKEN" not in msg
            assert "Secret Item Description" not in msg

        # debug_id is present in warning log
        warning_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("dbg-declined" in m for m in warning_msgs)

    asyncio.run(_test())
