"""Channel3 merchant agent: Channel3 API mapping, MCP exposure and the full order flow.

The Channel3 API is an httpx.MockTransport; the agent runs in-process through
ASGITransport exactly as ClawPay runs it when CHANNEL3_API_KEY is set.
"""

import json

import httpx
import pytest

from app.core.config import Settings
from app.integrations.mcp import tools as mcp_tools
from app.merchant_agents import channel3
from tests.fakes.app import make_harness, make_settings, tool_payload
from tests.fakes.flow import approve
from tests.fakes.approvals import get_challenge, sign_canonical
from tests.fakes.store import InMemoryStore

API_KEY = "c3-test-key-0123456789"
PID_A, PID_B = "2yh8WH5", "Zx91kQp"
SKU_A = channel3.product_id_to_sku(PID_A)
SKU_B = channel3.product_id_to_sku(PID_B)


def offer(price, currency="MYR", domain="shop.example", availability="InStock", shape="price"):
    return {"domain": domain, "url": f"https://buy.example/{domain}", "availability": availability,
            "price": {shape: price, "currency": currency, "compare_at_price": None}}


class FakeChannel3:
    """Scriptable Channel3 API recording every request."""

    def __init__(self) -> None:
        self.products = {
            PID_A: {"id": PID_A, "title": "Trail Running Shoe", "offers": [
                offer(89.9, domain="b-shop.example"), offer(79.5, domain="a-shop.example"),
                offer(60, domain="sold.example", availability="OutOfStock"), offer(10, currency="USD")]},
            PID_B: {"id": PID_B, "title": "Wool Socks \"Pro\"é", "offers": [offer("12.00", shape="amount")]},
        }
        self.requests: list[httpx.Request] = []
        self.search_status = 200
        self.product_status: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/search":
            if self.search_status != 200:
                return httpx.Response(self.search_status, json={"detail": "nope"})
            body = json.loads(request.content)
            items = list(self.products.values())[: body["limit"]]
            return httpx.Response(200, json={"products": items, "next_page_token": None})
        pid = request.url.path.rsplit("/", 1)[-1]
        status = self.product_status.get(pid, 200 if pid in self.products else 404)
        if status != 200:
            return httpx.Response(status, json={"detail": "x"})
        return httpx.Response(200, json=self.products[pid])


def harness(api: FakeChannel3 | None = None, **overrides):
    api = api or FakeChannel3()
    store = InMemoryStore()
    settings = make_settings(channel3_api_key=API_KEY, **overrides)
    agent = channel3.create_channel3_app(settings, transport=httpx.MockTransport(api), clock=store.clock)
    h = make_harness(store=store, merchant_transports={"channel3": httpx.ASGITransport(app=agent)},
                     channel3_api_key=API_KEY, **overrides)
    return h, api


# -- pure mapping ----------------------------------------------------------------------


def test_sku_round_trip_and_rejections() -> None:
    assert SKU_A.startswith("C3-") and SKU_A == SKU_A.upper()
    assert channel3.sku_to_product_id(SKU_A) == PID_A
    for bad in ("GRC-MILK-1L", "C3-", "C3-!!!", "C3-" + "A" * 61, None):
        assert channel3.sku_to_product_id(bad) is None  # type: ignore[arg-type]
    assert channel3.product_id_to_sku("bad id/../") is None


def test_choose_offer_cheapest_in_stock_in_currency() -> None:
    api = FakeChannel3()
    assert channel3.choose_offer(api.products[PID_A], "MYR") == (7950, "a-shop.example")
    assert channel3.choose_offer(api.products[PID_A], "USD") == (1000, "shop.example")
    assert channel3.choose_offer(api.products[PID_B], "MYR") == (1200, "shop.example")  # "amount" shape
    assert channel3.choose_offer({"offers": [offer(1.999)]}, "MYR") is None  # sub-cent price rejected
    assert channel3.choose_offer({"offers": [offer(0)]}, "MYR") is None
    assert channel3.choose_offer({"offers": [offer(True)]}, "MYR") is None
    assert channel3.choose_offer({"offers": "junk"}, "MYR") is None


# -- MCP exposure ----------------------------------------------------------------------


def test_channel3_hidden_unless_configured() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    listed = h.mcp(token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    assert listed == mcp_tools.TOOLS  # exactly contracts.md §4
    error, out = tool_payload(h.call_tool(token, "search_products", {"query": "shoe", "merchant_id": "channel3"}))
    assert error and out["error"] == "invalid_arguments" and "merchant_id" in out["message"]


def test_tools_list_includes_channel3_and_fits_device_buffer() -> None:
    h, _ = harness()
    token, _, _ = h.new_device()
    resp = h.mcp(token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    enums = [t["inputSchema"]["properties"]["merchant_id"]["enum"]
             for t in resp.json()["result"]["tools"] if "merchant_id" in t["inputSchema"]["properties"]]
    assert enums == [["grocer", "electronics", "channel3"]] * 2
    assert len(resp.content) <= mcp_tools.MAX_RESPONSE_BYTES


def test_search_through_channel3() -> None:
    h, api = harness()
    token, _, _ = h.new_device()
    error, out = tool_payload(h.call_tool(token, "search_products", {"query": "shoe", "merchant_id": "channel3"}))
    assert not error, out
    by_sku = {r["sku"]: r for r in out["results"]}
    assert by_sku[SKU_A]["price_minor"] == 7950 and by_sku[SKU_A]["name"] == "Trail Running Shoe (a-shop.example)"
    assert by_sku[SKU_A]["merchant_id"] == "channel3"
    sent = api.requests[0]
    assert sent.headers["x-api-key"] == API_KEY
    assert json.loads(sent.content)["config"] == {"country": "US", "currency": "MYR"}


def test_search_fanout_survives_channel3_outage() -> None:
    api = FakeChannel3()
    api.search_status = 500
    h, _ = harness(api)
    token, _, _ = h.new_device()
    error, out = tool_payload(h.call_tool(token, "search_products", {"query": "milk"}))
    assert not error and out["results"] and all(r["merchant_id"] != "channel3" for r in out["results"])


# -- orders ----------------------------------------------------------------------------


def test_create_order_uses_live_quote_price() -> None:
    h, api = harness()
    token, _, _ = h.new_device()
    api.products[PID_A]["offers"] = [offer(61.25, domain="a-shop.example")]  # price moved after search
    error, out = tool_payload(h.call_tool(token, "create_order", {
        "merchant_id": "channel3", "items": [{"sku": SKU_A, "qty": 1}, {"sku": SKU_B, "qty": 2}]}))
    assert not error, out
    assert out["total_minor"] == 6125 + 2 * 1200 and out["merchant"] == "Channel3 Marketplace"
    order = h.store.orders[out["order_id"]]
    assert h.store.merchants[order["merchant_id"]]["merchant_code"] == "channel3"
    assert {r.url.path for r in api.requests} == {f"/v1/products/{PID_A}", f"/v1/products/{PID_B}"}


@pytest.mark.parametrize(("status", "code"), [(404, "unknown_sku"), (500, "merchant_unavailable"),
                                              (429, "merchant_unavailable")])
def test_quote_failures(status: int, code: str) -> None:
    api = FakeChannel3()
    api.product_status[PID_A] = status
    h, _ = harness(api)
    token, _, _ = h.new_device()
    error, out = tool_payload(h.call_tool(token, "create_order",
                                          {"merchant_id": "channel3", "items": [{"sku": SKU_A, "qty": 1}]}))
    assert error and out["error"] == code


def test_out_of_stock_at_quote_time_is_unknown_sku() -> None:
    api = FakeChannel3()
    api.products[PID_B]["offers"] = [offer(12, availability="OutOfStock")]
    h, _ = harness(api)
    token, _, _ = h.new_device()
    error, out = tool_payload(h.call_tool(token, "create_order",
                                          {"merchant_id": "channel3", "items": [{"sku": SKU_B, "qty": 1}]}))
    assert error and out["error"] == "unknown_sku"


def test_phone_threshold_still_applies() -> None:
    api = FakeChannel3()
    api.products[PID_A]["offers"] = [offer(100.01)]
    h, _ = harness(api)
    token, _, _ = h.new_device()
    error, out = tool_payload(h.call_tool(token, "create_order",
                                          {"merchant_id": "channel3", "items": [{"sku": SKU_A, "qty": 1}]}))
    assert error and out["error"] == "phone_approval_unavailable"


def test_channel3_order_approved_and_paid() -> None:
    from tests.fakes.approvals import generate_p256
    from tests.fakes.app import ADMIN_TOKEN
    from tests.fakes.paypal import FakePayPalTransport

    api = FakeChannel3()
    store = InMemoryStore()
    settings = make_settings(channel3_api_key=API_KEY)
    agent = channel3.create_channel3_app(settings, transport=httpx.MockTransport(api), clock=store.clock)
    paypal = FakePayPalTransport()
    h = make_harness(store=store, merchant_transports={"channel3": httpx.ASGITransport(app=agent)},
                     paypal_transport=paypal, paypal_client_id="fake", paypal_client_secret="fake-secret",
                     channel3_api_key=API_KEY)
    uid = h.new_user()
    private, pem = generate_p256()
    device = h.client.post("/api/v1/devices/register", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                           json={"user_id": uid, "device_name": "c3", "public_key_pem": pem}).json()
    _, order = tool_payload(h.call_tool(device["device_token"], "create_order",
                                        {"merchant_id": "channel3", "items": [{"sku": SKU_B, "qty": 3}]}))
    oid = order["order_id"]
    ch = get_challenge(h, device["device_token"], oid)
    assert '"merchant_id":"channel3"' in ch["canonical"] and '"amount_minor":3600' in ch["canonical"]
    body = {"challenge_id": ch["challenge_id"], "device_id": device["device_id"], "key_version": ch["key_version"],
            "nonce": ch["nonce"], "decision": "approved", "signature": sign_canonical(private, ch["canonical"])}
    resp = approve(h, device, oid, body)
    assert resp.status_code == 200 and resp.json()["status"] == "paid", resp.text
    create = next(c for c in paypal.calls if c.path == "/v2/checkout/orders")
    assert create.body["purchase_units"][0]["amount"] == {"currency_code": "MYR", "value": "36.00"}


# -- configuration ---------------------------------------------------------------------


def test_config_validation() -> None:
    assert not make_settings().channel3_enabled
    assert make_settings(channel3_api_key=API_KEY).merchant_codes == ("grocer", "electronics", "channel3")
    assert make_settings(channel3_agent_endpoint="http://127.0.0.1:8103/a2a").channel3_enabled
    with pytest.raises(ValueError, match="CHANNEL3_BASE_URL"):
        make_settings(channel3_base_url="http://example.com")
    with pytest.raises(ValueError):
        make_settings(channel3_country="us")


def test_agent_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="CHANNEL3_API_KEY"):
        channel3.create_channel3_app(Settings(_env_file=None))


def test_api_key_never_logged(caplog) -> None:
    api = FakeChannel3()
    api.search_status = 401
    h, _ = harness(api)
    token, _, _ = h.new_device()
    caplog.set_level("DEBUG")
    h.call_tool(token, "search_products", {"query": "shoe", "merchant_id": "channel3"})
    assert API_KEY not in caplog.text
    assert any("Channel3 search returned 401" in r.getMessage() for r in caplog.records)
