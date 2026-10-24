"""MCP Streamable HTTP protocol, replaying cap_mcp_client (contracts.md §3, AC2.5)."""

from __future__ import annotations

import json

import pytest

from app.core.security import generate_device_token, generate_llm_token
from tests.fakes.app import MCP_HEADERS, Harness, make_harness

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "esp-claw", "version": "x"}}}


@pytest.fixture
def h() -> Harness:
    return make_harness()


def test_initialize_stateless(h: Harness) -> None:
    token, _, _ = h.new_device()
    resp = h.mcp(token, INIT)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert "mcp-session-id" not in resp.headers
    assert resp.json() == {"jsonrpc": "2.0", "id": 1, "result": {
        "protocolVersion": "2025-11-25", "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "clawpay", "version": "0.1.0"}}}
    old = {**INIT, "params": {"protocolVersion": "2025-03-26"}}
    assert h.mcp(token, old).json()["result"]["protocolVersion"] == "2025-03-26"
    unknown = {**INIT, "params": {"protocolVersion": "1999-01-01"}}
    assert h.mcp(token, unknown).json()["result"]["protocolVersion"] == "2025-11-25"
    # A session id sent by the client is ignored.
    assert h.mcp(token, INIT, {"Mcp-Session-Id": "abc"}).status_code == 200


def test_fresh_initialize_per_tool_call(h: Harness) -> None:
    token, _, _ = h.new_device()
    for rpc_id in (2, 2, 7):  # JSON-RPC ids restart in every fresh session
        resp = h.call_tool(token, "get_spending_summary", {}, rpc_id=rpc_id)
        assert resp.status_code == 200 and resp.json()["id"] == rpc_id
        assert resp.json()["result"]["isError"] is False


def test_notifications_202_empty(h: Harness) -> None:
    token, _, _ = h.new_device()
    for message in ({"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 5, "method": "notifications/cancelled"}):
        resp = h.mcp(token, message)
        assert resp.status_code == 202 and resp.content == b""


def test_ping_and_unknown_method(h: Harness) -> None:
    token, _, _ = h.new_device()
    assert h.mcp(token, {"jsonrpc": "2.0", "id": "p", "method": "ping"}).json() == {"jsonrpc": "2.0", "id": "p", "result": {}}
    resp = h.mcp(token, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert resp.json()["error"]["code"] == -32601


def test_tools_list_exact(h: Harness) -> None:
    token, _, _ = h.new_device()
    resp = h.mcp(token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"cursor": "x"}})
    result = resp.json()["result"]
    assert "nextCursor" not in result
    names = {t["name"] for t in result["tools"]}
    assert names == {"search_products", "create_order", "get_order_status", "get_spending_summary"}
    for forbidden in ("approve", "pay", "capture", "refund", "transfer", "charge"):
        assert not any(forbidden in n for n in names)
    assert len(resp.content) <= 3900


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize("path", ["/mcp", f"/mcp/{generate_device_token()}"])
def test_get_delete_405(h: Harness, method: str, path: str) -> None:
    resp = h.client.request(method, path)
    assert resp.status_code == 405
    assert resp.headers["allow"] == "POST"
    assert resp.content == b""


@pytest.mark.parametrize("auth", [None, "Basic abc", "Bearer ", "Bearer garbage", f"Bearer {generate_device_token()}",
                                  f"Bearer {generate_llm_token()}"])
def test_bad_bearer_401(h: Harness, auth: str | None) -> None:
    headers = dict(MCP_HEADERS)
    if auth is not None:
        headers["Authorization"] = auth
    resp = h.client.post("/mcp", json=INIT, headers=headers)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["error"]["code"] == "unauthorized"


def test_revoked_device_401(h: Harness) -> None:
    token, device_id, _ = h.new_device()
    assert h.mcp(token, INIT).status_code == 200
    h.store.devices[device_id]["status"] = "revoked"
    assert h.mcp(token, INIT).status_code == 401


def test_cpl_token_on_mcp_rejected(h: Harness) -> None:
    from app.core.security import hash_token
    from tests.fakes.app import PEPPER

    llm = generate_llm_token()
    user = h.new_user()
    h.store.add_device(user, token_hash="x", llm_token_hash=hash_token(PEPPER, llm))
    assert h.mcp(llm, INIT).status_code == 401


def test_path_token_disabled_404() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    resp = h.client.post(f"/mcp/{token}", json=INIT, headers=MCP_HEADERS)
    assert resp.status_code == 404


def test_path_token_enabled() -> None:
    h = make_harness(mcp_path_token_enabled=True)
    token, _, _ = h.new_device()
    resp = h.client.post(f"/mcp/{token}", json=INIT, headers=MCP_HEADERS)
    assert resp.status_code == 200
    assert h.client.post(f"/mcp/{generate_device_token()}", json=INIT, headers=MCP_HEADERS).status_code == 401
    assert h.client.post("/mcp/garbage", json=INIT, headers=MCP_HEADERS).status_code == 401


def test_mcp_disabled_not_mounted() -> None:
    h = make_harness(mcp_enabled=False)
    assert h.client.post("/mcp", json=INIT).status_code == 404


def test_unsupported_protocol_header_400(h: Harness) -> None:
    token, _, _ = h.new_device()
    resp = h.mcp(token, INIT, {"MCP-Protocol-Version": "2024-11-05"})
    assert resp.status_code == 400
    assert resp.json() == {"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32600, "message": "Unsupported MCP-Protocol-Version"}}
    headers = {k: v for k, v in MCP_HEADERS.items() if k != "MCP-Protocol-Version"}
    headers["Authorization"] = f"Bearer {token}"
    assert h.client.post("/mcp", json=INIT, headers=headers).status_code == 200


def test_batch_and_shape_errors(h: Harness) -> None:
    token, _, _ = h.new_device()
    assert h.mcp(token, [INIT]).json()["error"]["code"] == -32600
    for bad in ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 1},
                {"jsonrpc": "2.0", "id": None, "method": "ping"}, {"jsonrpc": "2.0", "id": True, "method": "ping"},
                {"jsonrpc": "2.0", "id": 1.5, "method": "ping"}, "string"):
        resp = h.mcp(token, bad)
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32600


def test_parse_error(h: Harness) -> None:
    token, _, _ = h.new_device()
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    resp = h.client.post("/mcp", content=b"{not json", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700 and resp.json()["id"] is None


def test_body_over_64kb_413(h: Harness) -> None:
    token, _, _ = h.new_device()
    big = {**INIT, "params": {"pad": "x" * 70000}}
    assert h.mcp(token, big).status_code == 413
    # Declared size is checked before auth.
    resp = h.client.post("/mcp", content=json.dumps(big), headers=MCP_HEADERS)
    assert resp.status_code == 413


def test_foreign_origin_403() -> None:
    h = make_harness(mcp_allowed_origins="https://dash.example")
    token, _, _ = h.new_device()
    resp = h.mcp(token, INIT, {"Origin": "https://evil.example"})
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden_origin"
    assert h.mcp(token, INIT, {"Origin": "https://dash.example"}).status_code == 200
    assert h.mcp(token, INIT).status_code == 200  # the device sends no Origin


def test_id_required_executes_nothing(h: Harness) -> None:
    token, _, _ = h.new_device()
    resp = h.mcp(token, {"jsonrpc": "2.0", "method": "tools/call",
                         "params": {"name": "search_products", "arguments": {"query": "milk"}}})
    assert resp.status_code == 200
    assert resp.json() == {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "id required"}}
    assert h.store.a2a_messages == []
    assert h.store.conversation_events == []
    assert "merchants.get_active_by_code" not in h.store.calls


def test_per_device_request_limit() -> None:
    h = make_harness(mcp_requests_per_minute=3)
    token, _, _ = h.new_device()
    other, _, _ = h.new_device()
    for _ in range(3):
        assert h.mcp(token, INIT).status_code == 200
    resp = h.mcp(token, INIT)
    assert resp.status_code == 429 and "retry-after" in resp.headers
    assert h.mcp(other, INIT).status_code == 200


def test_bodies_and_tokens_not_logged(h: Harness, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    token, _, _ = h.new_device()
    with caplog.at_level(logging.DEBUG):
        h.call_tool(token, "search_products", {"query": "secret-shopping-list"})
        h.mcp("cpd_" + "x" * 43, INIT)
    assert token not in caplog.text
    assert "secret-shopping-list" not in caplog.text
