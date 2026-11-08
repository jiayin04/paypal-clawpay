"""Watch HTTP contract through both agents; only external providers are mocked."""

import json

import httpx

from app.merchant_agents.channel3 import create_channel3_app, product_id_to_sku
from app.paypal_agent.app import create_paypal_agent_app
from tests.fakes.app import ADMIN_TOKEN, make_harness, make_settings, tool_payload
from tests.fakes.approvals import generate_p256, get_challenge, sign_canonical
from tests.fakes.paypal import FakePayPalTransport
from tests.fakes.store import InMemoryStore


def test_watch_search_order_approval_and_payment_through_both_agents():
    api_calls = []

    def catalog(request):
        api_calls.append(request.url.path)
        product = {"id": "watchDemo1", "title": "Demo Socks", "offers": [{
            "domain": "shop.example", "availability": "InStock",
            "price": {"price": 12, "currency": "USD"},
        }]}
        return httpx.Response(200, json={"products": [product]} if request.url.path == "/v1/search" else product)

    sku = product_id_to_sku("watchDemo1")
    turns = iter([
        ("search_products", {"query": "socks", "merchant_id": "channel3"}),
        (None, "Demo Socks cost US$12.00. Would you like one pair?"),
        ("create_order", {"merchant_id": "channel3", "items": [{"sku": sku, "qty": 1}]}),
        (None, "Tap Approve on your watch to pay USD 12.00."),
    ])

    def llm(request):
        name, args = next(turns)
        message = {"role": "assistant", "content": args if name is None else None}
        if name:
            message["tool_calls"] = [{"id": name, "type": "function", "function": {
                "name": name, "arguments": json.dumps(args),
            }}]
        return httpx.Response(200, json={"choices": [{"message": message}], "usage": {"total_tokens": 5}})

    agent_token = "watch-test-agent-token-0123456789-abcdef"
    settings = make_settings(default_currency="USD", channel3_api_key="test-channel3-key",
                             paypal_agent_token=agent_token, paypal_client_id="fake", paypal_client_secret="fake")
    store = InMemoryStore()
    merchant = create_channel3_app(settings, transport=httpx.MockTransport(catalog), clock=store.clock)
    paypal = FakePayPalTransport()
    payment_agent = create_paypal_agent_app(settings, paypal_transport=paypal)
    h = make_harness(
        store=store, default_currency="USD", channel3_api_key="test-channel3-key",
        merchant_transports={"channel3": httpx.ASGITransport(app=merchant)},
        paypal_agent_endpoint="http://127.0.0.1:8201/a2a", paypal_agent_token=agent_token,
        paypal_agent_transport=httpx.ASGITransport(app=payment_agent),
        llm_proxy_upstream_base_url="https://llm.test/v1", llm_proxy_upstream_api_key="fake",
        llm_proxy_model="test", llm_client=httpx.AsyncClient(transport=httpx.MockTransport(llm)),
    )
    private, pem = generate_p256()
    registered = h.client.post("/api/v1/devices/register", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                               json={"user_id": h.new_user(), "device_name": "watch", "public_key_pem": pem})
    assert registered.status_code == 201
    device = registered.json()
    auth = {"Authorization": f"Bearer {device['device_token']}"}
    def ask(text):
        response = h.client.post("/api/v1/assistant/messages/stream", headers=auth,
                                 json={"text": text, "approver": "watch"})
        assert response.status_code == 200, response.text
        return next(json.loads(line)["data"] for line in response.text.splitlines() if json.loads(line)["type"] == "result")

    found = ask("Find socks on Channel3 under $20")
    assert found["order"] is None and found["products"][0]["sku"] == sku
    assert not store.orders and not paypal.calls
    response = ask("Yes, order one pair")
    assert response["approval_required"] is True
    assert not paypal.calls  # Chat cannot move money.
    oid = response["order"]["order_id"]
    assert response["order"]["items"][0]["qty"] == 1
    challenge = get_challenge(h, device["device_token"], oid)
    approval = {"challenge_id": challenge["challenge_id"], "device_id": device["device_id"],
                "key_version": challenge["key_version"], "nonce": challenge["nonce"], "decision": "approved",
                "signature": sign_canonical(private, challenge["canonical"])}
    result = h.client.post(f"/api/v1/orders/{oid}/gadget-approval", headers=auth, json=approval)
    assert result.status_code == 200 and result.json()["status"] == "paid", result.text
    error, status = tool_payload(h.call_tool(device["device_token"], "get_order_status", {"order_id": oid}))
    assert not error and status["status"] == "paid"
    assert api_calls == ["/v1/search", "/v1/products/watchDemo1"]
    creates = [c for c in paypal.calls if c.path == "/v2/checkout/orders"]
    assert len(creates) == 1
    assert creates[0].body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "12.00"}
    receipt = h.client.get(f"/api/v1/orders/{oid}/receipt", headers=auth).json()
    assert receipt["status"] == "paid" and receipt["items"][0]["name"] == "Demo Socks (shop.example)"
    assert receipt["retailer_order_status"] == "not_submitted" and receipt["fulfillment_mode"] == "simulated"
