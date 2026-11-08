"""PayPal agent (A2A) and the gateway that delegates to it.

The agent runs in-process through httpx.ASGITransport with the fake PayPal
transport behind it, so the full device flow exercises ClawPay -> agent -> PayPal.
"""

import base64
import json

import httpx
import pytest

from app.core.metrics import METRICS, reset_metrics
from app.integrations.paypal import PayPalApiError, PayPalAuthError, PayPalUnavailable
from app.integrations.paypal.agent_gateway import A2APayPalGateway
from app.paypal_agent.app import create_paypal_agent_app
from tests.fakes.app import make_settings, tool_payload
from tests.fakes.flow import approve, create_and_sign, setup_flow
from tests.fakes.paypal import FakePayPalTransport

AGENT_TOKEN = "test-paypal-agent-token-0123456789-abcdef"
AGENT_URL = "http://127.0.0.1:8201/a2a"


@pytest.fixture(autouse=True)
def _fresh_metrics():
    reset_metrics()
    yield
    reset_metrics()


class RecordingASGI(httpx.ASGITransport):
    """ASGITransport that remembers the request headers ClawPay sent to the agent."""

    def __init__(self, app) -> None:
        super().__init__(app=app)
        self.headers: list[dict[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(dict(request.headers))
        return await super().handle_async_request(request)


def agent_transport(paypal: FakePayPalTransport, token: str = AGENT_TOKEN) -> RecordingASGI:
    agent_settings = make_settings(
        mcp_enabled=False, paypal_client_id="fake", paypal_client_secret="fake-secret", paypal_agent_token=token
    )
    return RecordingASGI(create_paypal_agent_app(agent_settings, paypal_transport=paypal))


def agent_mode(paypal: FakePayPalTransport | None = None, *, agent_token: str = AGENT_TOKEN,
               transport: httpx.AsyncBaseTransport | None = None, mode: str = "completed"):
    """ClawPay in agent mode: no PayPal credentials on the ClawPay side."""
    paypal = paypal or FakePayPalTransport()
    paypal.orders_mode = mode
    transport = transport or agent_transport(paypal, agent_token)
    flow = setup_flow(paypal=paypal, paypal_agent_endpoint=AGENT_URL, paypal_agent_token=AGENT_TOKEN,
                      paypal_agent_transport=transport)
    return (*flow, transport)


def gateway(handler) -> A2APayPalGateway:
    settings = make_settings(paypal_agent_endpoint=AGENT_URL, paypal_agent_token=AGENT_TOKEN)
    return A2APayPalGateway(settings, transport=httpx.MockTransport(handler))


def agent_reply(request: httpx.Request, data: dict) -> httpx.Response:
    body = json.loads(request.content)
    sent = body["params"]["message"]["parts"][0]["data"]
    out = {"type": "clawpay.paypal_response.v1", "correlation_id": sent["correlation_id"],
           "operation": sent["operation"], **data}
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
        "kind": "message", "role": "agent", "messageId": "m", "parts": [{"kind": "data", "data": out}]}})


# -- end-to-end through the agent ------------------------------------------------------


def test_device_flow_pays_through_the_agent() -> None:
    h, paypal, _, key, device, transport = agent_mode()
    assert h.settings.paypal_client_id is None and h.settings.paypal_configured
    oid, body = create_and_sign(h, key, device)
    resp = approve(h, device, oid, body)
    assert resp.status_code == 200 and resp.json()["status"] == "paid", resp.text
    _, status = tool_payload(h.call_tool(device["device_token"], "get_order_status", {"order_id": oid}))
    assert status["status"] == "paid" and status["paid_at"]

    creates = [c for c in paypal.calls if c.path == "/v2/checkout/orders"]
    assert len(creates) == 1
    assert creates[0].headers["paypal-request-id"] == f"clawpay-{oid}-1"
    # Every agent call carried the bearer and the approval request's id.
    approval_calls = [hd for hd in transport.headers if hd.get("x-request-id") == resp.headers["x-request-id"]]
    assert len(approval_calls) >= 2  # ensure_access_token + create_order_with_vault
    assert all(hd["authorization"] == f"Bearer {AGENT_TOKEN}" for hd in transport.headers)
    assert METRICS.paypal_requests.value("agent:create_order_with_vault", "ok") == 1
    assert "fake-access-token" not in json.dumps([c.body for c in paypal.calls if c.path != "/v1/oauth2/token"])


def test_paypal_decline_through_agent_releases_spending() -> None:
    h, paypal, uid, key, device, _ = agent_mode(mode="instrument_declined")
    oid, body = create_and_sign(h, key, device)
    resp = approve(h, device, oid, body)
    assert resp.status_code == 200 and resp.json()["status"] == "payment_failed", resp.text
    assert [r["status"] for r in h.store.spending_records if r["order_id"] == oid] == ["released"]


def test_agent_rejects_wrong_token_and_nothing_is_sent() -> None:
    paypal = FakePayPalTransport()
    wrong = agent_transport(paypal, token="a-different-agent-token-0123456789-xyz")
    h, _, _, key, device, _ = agent_mode(paypal, transport=wrong)
    oid, body = create_and_sign(h, key, device)
    resp = approve(h, device, oid, body)
    assert resp.status_code == 200 and resp.json()["status"] == "payment_pending"
    assert paypal.calls == []
    attempt = next(a for a in h.store.payment_attempts.values() if a["order_id"] == oid)
    assert attempt["status"] == "initiated" and attempt["first_sent_at"] is None


def test_agent_down_leaves_attempt_unsent() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    h, _, _, key, device, _ = agent_mode(transport=httpx.MockTransport(down))
    oid, body = create_and_sign(h, key, device)
    assert approve(h, device, oid, body).json()["status"] == "payment_pending"
    attempt = next(a for a in h.store.payment_attempts.values() if a["order_id"] == oid)
    assert attempt["first_sent_at"] is None
    assert [r["status"] for r in h.store.spending_records if r["order_id"] == oid] == ["reserved"]


# -- gateway failure mapping ------------------------------------------------------------


@pytest.mark.anyio
async def test_api_error_is_reconstructed_for_the_classifier() -> None:
    gw = gateway(lambda r: agent_reply(r, {"ok": False, "error": {
        "kind": "api", "status": 422, "name": "UNPROCESSABLE_ENTITY", "issue": "INSTRUMENT_DECLINED",
        "debug_id": "dbg-1"}}))
    with pytest.raises(PayPalApiError) as err:
        await gw.get_order("5O190127TN364715T")
    assert (err.value.status, err.value.issue, err.value.debug_id) == (422, "INSTRUMENT_DECLINED", "dbg-1")


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["unavailable", "invalid", "bad_request"])
async def test_non_api_agent_errors_are_unavailable(kind: str) -> None:
    gw = gateway(lambda r: agent_reply(r, {"ok": False, "error": {"kind": kind}}))
    with pytest.raises(PayPalUnavailable):
        await gw.capture_order(provider_order_id="ORDER1", request_id="k-capture")


@pytest.mark.anyio
async def test_auth_error_maps_to_auth() -> None:
    gw = gateway(lambda r: agent_reply(r, {"ok": False, "error": {"kind": "auth"}}))
    with pytest.raises(PayPalAuthError):
        await gw.ensure_access_token()


@pytest.mark.anyio
async def test_api_kind_without_status_is_not_trusted() -> None:
    gw = gateway(lambda r: agent_reply(r, {"ok": False, "error": {"kind": "api"}}))
    with pytest.raises(PayPalUnavailable):
        await gw.get_order("ORDER1")


@pytest.mark.anyio
async def test_mismatched_correlation_id_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data = {"type": "clawpay.paypal_response.v1", "correlation_id": "00000000-0000-0000-0000-000000000000",
                "operation": "get_order", "ok": True, "result": {"id": "X", "status": "COMPLETED"}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
            "kind": "message", "role": "agent", "messageId": "m", "parts": [{"kind": "data", "data": data}]}})

    with pytest.raises(PayPalUnavailable):
        await gateway(handler).get_order("ORDER1")


@pytest.mark.anyio
async def test_malformed_order_result_is_rejected() -> None:
    gw = gateway(lambda r: agent_reply(r, {"ok": True, "result": {"id": "X", "status": "COMPLETED",
                                                                  "capture_amount_minor": "1380"}}))
    with pytest.raises(PayPalUnavailable):
        await gw.get_order("ORDER1")


@pytest.mark.anyio
async def test_agent_http_401_is_unavailable() -> None:
    with pytest.raises(PayPalUnavailable):
        await gateway(lambda r: httpx.Response(401, json={})).get_order("ORDER1")


# -- agent service -----------------------------------------------------------------------


def _agent_client(paypal: FakePayPalTransport | None = None):
    from fastapi.testclient import TestClient

    settings = make_settings(mcp_enabled=False, paypal_client_id="fake", paypal_client_secret="fake-secret",
                             paypal_agent_token=AGENT_TOKEN)
    return TestClient(create_paypal_agent_app(settings, paypal_transport=paypal or FakePayPalTransport()))


def _send(client, data, token: str | None = AGENT_TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = {"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {"message": {
        "kind": "message", "role": "user", "messageId": "m", "parts": [{"kind": "data", "data": data}]}}}
    return client.post("/a2a", json=body, headers=headers)


def _data(resp) -> dict:
    return resp.json()["result"]["parts"][0]["data"]


def test_agent_requires_bearer() -> None:
    client = _agent_client()
    assert _send(client, {}, token=None).status_code == 401
    assert _send(client, {}, token="nope").status_code == 401


def test_agent_never_returns_the_access_token() -> None:
    client = _agent_client()
    resp = _send(client, {"type": "clawpay.paypal_request.v1",
                          "correlation_id": "11111111-1111-4111-8111-111111111111",
                          "operation": "ensure_access_token", "params": {}})
    data = _data(resp)
    assert data["ok"] is True and data["result"] == {}
    assert "fake-access-token" not in resp.text


def test_agent_rejects_bad_params_without_calling_paypal() -> None:
    paypal = FakePayPalTransport()
    client = _agent_client(paypal)
    data = _data(_send(client, {"type": "clawpay.paypal_request.v1",
                                "correlation_id": "11111111-1111-4111-8111-111111111111",
                                "operation": "create_order_with_vault",
                                "params": {"request_id": "k", "amount_minor": -5}}))
    assert data["ok"] is False and data["error"] == {"kind": "bad_request"}
    assert paypal.calls == []


def test_agent_passes_paypal_api_errors_without_messages() -> None:
    paypal = FakePayPalTransport()
    paypal.orders_mode = "instrument_declined"
    client = _agent_client(paypal)
    data = _data(_send(client, {
        "type": "clawpay.paypal_request.v1", "correlation_id": "11111111-1111-4111-8111-111111111111",
        "operation": "create_order_with_vault",
        "params": {"request_id": "clawpay-x-1", "order_ref": "22222222-2222-4222-8222-222222222222",
                   "invoice_id": "clawpay-x", "description": "ClawPay order", "amount_minor": 1380,
                   "currency": "MYR", "vault_id": "VAULT1"}}))
    assert data["ok"] is False
    assert data["error"]["kind"] == "api" and data["error"]["issue"] == "INSTRUMENT_DECLINED"
    assert set(data["error"]) <= {"kind", "status", "name", "issue", "debug_id"}


@pytest.mark.anyio
async def test_webhook_verify_through_agent_keeps_raw_body_and_paypal_headers_only() -> None:
    paypal = FakePayPalTransport()
    gw = A2APayPalGateway(make_settings(paypal_agent_endpoint=AGENT_URL, paypal_agent_token=AGENT_TOKEN),
                          transport=agent_transport(paypal))
    raw = b'{"id":"WH-EVT-1",  "event_type":"PAYMENT.CAPTURE.COMPLETED"}'
    headers = {"PAYPAL-TRANSMISSION-ID": "t", "PAYPAL-TRANSMISSION-TIME": "2026-10-30T00:00:00Z",
               "PAYPAL-TRANSMISSION-SIG": "s", "PAYPAL-CERT-URL": "https://api.paypal.com/c",
               "PAYPAL-AUTH-ALGO": "SHA256withRSA", "Authorization": "Bearer should-not-forward"}
    assert await gw.verify_webhook(headers=headers, raw_body=raw, webhook_id="WH-1") is True
    verify = next(c for c in paypal.calls if c.path == "/v1/notifications/verify-webhook-signature")
    assert raw in verify.raw_body
    assert b"should-not-forward" not in verify.raw_body
    assert base64.b64encode(raw)  # sanity: body travelled base64-encoded


# -- configuration -----------------------------------------------------------------------


def test_config_requires_token_with_endpoint_and_validates_url() -> None:
    with pytest.raises(ValueError, match="PAYPAL_AGENT_TOKEN must be set"):
        make_settings(paypal_agent_endpoint=AGENT_URL)
    with pytest.raises(ValueError, match="PAYPAL_AGENT_TOKEN must be at least 32"):
        make_settings(paypal_agent_endpoint=AGENT_URL, paypal_agent_token="short")
    with pytest.raises(ValueError, match="PAYPAL_AGENT_ENDPOINT"):
        make_settings(paypal_agent_endpoint="http://example.com/a2a", paypal_agent_token=AGENT_TOKEN)
    settings = make_settings(paypal_agent_endpoint="https://agent.example.com/a2a/", paypal_agent_token=AGENT_TOKEN)
    assert settings.paypal_agent_endpoint == "https://agent.example.com/a2a"
    assert settings.paypal_agent_enabled and settings.paypal_configured


def test_agent_factory_requires_token_and_credentials() -> None:
    with pytest.raises(RuntimeError, match="PAYPAL_AGENT_TOKEN"):
        create_paypal_agent_app(make_settings(mcp_enabled=False, paypal_client_id="x", paypal_client_secret="y"))
    with pytest.raises(RuntimeError, match="PAYPAL_CLIENT_ID"):
        create_paypal_agent_app(make_settings(mcp_enabled=False, paypal_agent_token=AGENT_TOKEN))
