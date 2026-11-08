"""Request ids, log context, JSON logs and the /metrics endpoint."""

import io
import json
import logging

import httpx
import pytest

from app.core.metrics import METRICS, Histogram, reset_metrics
from app.core.observability import ContextFilter, JsonFormatter
from app.integrations.paypal.client import operation_label
from tests.fakes.app import make_harness, make_settings
from tests.fakes.merchants import ScriptedMerchant

METRICS_TOKEN = "test-metrics-token-0123456789-abcdefghij"
MILK = {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 1}]}


@pytest.fixture(autouse=True)
def _fresh_metrics():
    reset_metrics()
    yield
    reset_metrics()


@pytest.fixture
def json_lines():
    """A handler like the production one (context filter + JSON) on the root logger."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    finally:
        root.removeHandler(handler)


def test_request_id_generated_and_echoed() -> None:
    h = make_harness()
    first = h.client.get("/health")
    second = h.client.get("/health")
    assert len(first.headers["x-request-id"]) == 32
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_valid_incoming_request_id_reused_and_invalid_replaced() -> None:
    h = make_harness()
    kept = h.client.get("/health", headers={"X-Request-ID": "edge-abc.123_XYZ"})
    assert kept.headers["x-request-id"] == "edge-abc.123_XYZ"
    for bad in ("short", "has space in it!", "x" * 65, "bad/slash/value-1"):
        replaced = h.client.get("/health", headers={"X-Request-ID": bad})
        assert replaced.headers["x-request-id"] != bad
        assert len(replaced.headers["x-request-id"]) == 32


def test_llm_proxy_does_not_echo_request_id() -> None:
    h = make_harness()
    _, _, _ = h.new_device()
    resp = h.client.post("/llm/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]})
    assert "x-request-id" not in resp.headers


def test_log_lines_carry_request_device_and_order_ids(json_lines) -> None:
    h = make_harness()
    token, device_id, user_id = h.new_device()
    from tests.fakes.app import tool_payload

    _, created = tool_payload(h.call_tool(token, "create_order", MILK))
    order_id = created["order_id"]
    resp = h.call_tool(token, "get_order_status", {"order_id": order_id})
    request_id = resp.headers["x-request-id"]

    lines = [line for line in json_lines() if line.get("request_id") == request_id]
    access = [line for line in lines if line.get("event") == "http_request"]
    assert access and access[0]["route"] == "/mcp" and access[0]["status"] == 200
    assert access[0]["device_id"] == device_id and access[0]["user_id"] == user_id
    assert access[0]["order_id"] == order_id
    assert any(line["message"] == "Tool get_order_status ok" for line in lines)
    # The token never appears in any log line.
    assert token not in json.dumps(json_lines())


def test_json_logs_stay_redacted(json_lines) -> None:
    make_harness()  # configures the redacting record factory
    secret = "cpd_" + "A" * 43
    logging.getLogger("app.test").warning("leaked %s and Bearer abc.def", secret)
    message = [line for line in json_lines() if line["logger"] == "app.test"][-1]["message"]
    assert secret not in message and "[REDACTED]" in message and "abc.def" not in message


def test_merchant_calls_forward_request_id() -> None:
    from tests.fakes.app import tool_payload
    from tests.fakes.clock import FakeClock

    clock = FakeClock()
    merchant = ScriptedMerchant("grocer", clock)
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-request-id"))
        return merchant(request)

    from tests.fakes.store import InMemoryStore

    h = make_harness(store=InMemoryStore(clock), merchant_transports={"grocer": httpx.MockTransport(handler)})
    token, _, _ = h.new_device()
    resp = h.call_tool(token, "search_products", {"query": "milk", "merchant_id": "grocer"})
    assert tool_payload(resp)[0] is False
    assert seen == [resp.headers["x-request-id"]]
    assert METRICS.merchant_requests.value("grocer", "search", "ok") == 1


def test_metrics_disabled_without_token() -> None:
    h = make_harness()
    assert h.client.get("/metrics").status_code == 404


def test_metrics_requires_bearer_token() -> None:
    h = make_harness(metrics_token=METRICS_TOKEN)
    assert h.client.get("/metrics").status_code == 401
    assert h.client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_metrics_exposes_route_templates_not_raw_paths() -> None:
    h = make_harness(metrics_token=METRICS_TOKEN, mcp_path_token_enabled=True)
    token, _, _ = h.new_device()
    h.call_tool(token, "get_spending_summary", {})
    h.mcp(None, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, path=f"/mcp/{token}")
    h.client.get("/no/such/path/cpd_secret")
    resp = h.client.get("/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = resp.text
    assert 'clawpay_http_requests_total{method="POST",route="/mcp",status="200"} 2' in body
    assert 'route="/mcp/{device_token}"' in body
    assert 'route="unmatched",status="404"' in body
    assert token not in body and "cpd_secret" not in body
    assert "# TYPE clawpay_http_request_duration_seconds histogram" in body


def test_auth_failures_counted_by_kind_and_reason() -> None:
    h = make_harness()
    h.mcp("cpd_bad", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert METRICS.auth_failures.value("device", "malformed") == 1


def test_histogram_render_is_cumulative() -> None:
    hist = Histogram("x_seconds", "help", ("op",), buckets=(0.1, 1.0))
    hist.observe(0.05, "a")
    hist.observe(0.5, "a")
    hist.observe(5, "a")
    lines = list(hist.render())
    assert 'x_seconds_bucket{op="a",le="0.1"} 1' in lines
    assert 'x_seconds_bucket{op="a",le="1"} 2' in lines
    assert 'x_seconds_bucket{op="a",le="+Inf"} 3' in lines
    assert 'x_seconds_count{op="a"} 3' in lines


@pytest.mark.parametrize(
    ("method", "path", "label"),
    [
        ("post", "/v2/checkout/orders", "POST v2/checkout/orders"),
        ("POST", "v2/checkout/orders/5O190127TN364715T/capture", "POST v2/checkout/orders/{id}/capture"),
        ("GET", "/v3/vault/setup-tokens/5C991763VB2771612", "GET v3/vault/setup-tokens/{id}"),
        ("POST", "/v1/notifications/verify-webhook-signature", "POST v1/notifications/verify-webhook-signature"),
    ],
)
def test_paypal_operation_label_strips_ids(method: str, path: str, label: str) -> None:
    assert operation_label(method, path) == label


def test_config_metrics_token_length_and_log_format() -> None:
    with pytest.raises(ValueError, match="METRICS_TOKEN"):
        make_settings(metrics_token="short")
    assert make_settings(log_format="JSON").log_format == "json"
