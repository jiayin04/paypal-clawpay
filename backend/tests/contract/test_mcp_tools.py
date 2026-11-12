"""MCP tool schemas, argument rules, result shape and size caps (contracts.md §4, AC2.5/2.6)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.integrations.mcp import tools as mcp_tools
from tests.fakes.app import Harness, make_harness, tool_payload
from tests.fakes.merchants import ScriptedMerchant

CONTRACTS = Path(__file__).resolve().parents[2] / "docs" / "contracts.md"


def _contract_schemas() -> dict[str, dict]:
    text = CONTRACTS.read_text(encoding="utf-8")
    out: dict[str, dict] = {}
    for name in ("search_products", "create_order", "get_order_status", "get_spending_summary"):
        section = text.split(f"### `{name}`", 1)[1]
        block = re.search(r"```json\n(.*?)\n```", section, re.S).group(1)
        out[name] = json.loads(block)
    return out


def test_schemas_match_contract_verbatim() -> None:
    contract = _contract_schemas()
    published = {t["name"]: t["inputSchema"] for t in mcp_tools.TOOLS}
    assert published == contract


def test_descriptions_match_contract() -> None:
    text = CONTRACTS.read_text(encoding="utf-8")
    for tool in mcp_tools.TOOLS:
        assert f'Description: "{tool["description"]}"' in text, tool["name"]


@pytest.mark.parametrize(
    ("name", "good", "bad"),
    [
        ("search_products", [{"query": "milk"}, {"query": "x", "max_results": 10, "merchant_id": "grocer"}],
         [{}, {"query": ""}, {"query": "   "}, {"query": "x" * 101}, {"query": "a", "max_results": 0},
          {"query": "a", "max_results": 11}, {"query": "a", "max_results": "5"}, {"query": "a", "merchant_id": "bank"},
          {"query": "a", "extra": 1}, {"query": 5}]),
        ("create_order", [{"merchant_id": "grocer", "items": [{"sku": "A", "qty": 1}]},
                          {"merchant_id": "electronics", "items": [{"sku": "A", "qty": 20}] * 10, "currency": "MYR"}],
         [{"merchant_id": "grocer"}, {"merchant_id": "grocer", "items": [{"sku": "A", "qty": 1}] * 11},
          {"merchant_id": "grocer", "items": [{"sku": "A" * 65, "qty": 1}]},
          {"merchant_id": "grocer", "items": [{"sku": "A", "qty": 1.0}]},
          {"merchant_id": "grocer", "items": [{"sku": "A", "qty": True}]},
          {"merchant_id": "grocer", "items": [{"sku": "A", "qty": 1}], "currency": "MYRR"}]),
        ("get_order_status", [{"order_id": "7f3c0c1e-5a1b-4c1e-9a43-0b7d2f9e6a11"},
                              {"order_id": "7F3C0C1E-5A1B-4C1E-9A43-0B7D2F9E6A11"}],
         [{}, {"order_id": "7f3c0c1e5a1b4c1e9a430b7d2f9e6a11"}, {"order_id": 5}, {"order_id": "x"}]),
        ("get_spending_summary", [{}, {"period": "today"}], [{"period": "week"}, {"other": 1}]),
    ],
)
def test_pydantic_models_accept_and_reject_like_schema(name: str, good: list, bad: list) -> None:
    model = mcp_tools.ARG_MODELS[name]
    for args in good:
        model.model_validate(args)
    for args in bad:
        with pytest.raises(ValidationError):
            model.model_validate(args)


def test_order_id_normalised_lowercase() -> None:
    args = mcp_tools.ARG_MODELS["get_order_status"].model_validate({"order_id": "7F3C0C1E-5A1B-4C1E-9A43-0B7D2F9E6A11"})
    assert args.order_id == "7f3c0c1e-5a1b-4c1e-9a43-0b7d2f9e6a11"


def test_error_messages_table_complete() -> None:
    codes = {"invalid_arguments", "rate_limited", "merchant_unavailable", "unknown_sku", "quote_invalid",
             "currency_not_supported", "no_payment_method", "daily_limit_exceeded", "phone_approval_unavailable",
             "delegation_denied", "not_found", "internal_error"}
    assert set(mcp_tools.ERROR_MESSAGES) == codes
    assert all(mcp_tools.ERROR_MESSAGES.values())


def _call(h: Harness, token: str, params: object, rpc_id: int = 2):  # type: ignore[no-untyped-def]
    return h.mcp(token, {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call", "params": params})


def test_unknown_tool_and_non_object_arguments() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    assert _call(h, token, {"name": "approve_order", "arguments": {}}).json()["error"]["code"] == -32602
    assert _call(h, token, {"name": "get_spending_summary", "arguments": [1]}).json()["error"]["code"] == -32602
    assert _call(h, token, "x").json()["error"]["code"] == -32602
    ok = _call(h, token, {"name": "get_spending_summary"})
    assert ok.json()["result"]["isError"] is False


def test_invalid_arguments_is_tool_error_with_field() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    is_error, out = tool_payload(h.call_tool(token, "search_products", {"query": "milk", "max_results": "5"}))
    assert is_error and out["error"] == "invalid_arguments"
    assert out["message"].startswith("max_results:")
    is_error, out = tool_payload(h.call_tool(token, "get_order_status", {"order_id": "abc"}))
    assert is_error and out["message"].startswith("order_id:")


def test_tool_rate_limit_is_iserror() -> None:
    h = make_harness(mcp_tool_calls_per_minute=2)
    token, _, _ = h.new_device()
    for _ in range(2):
        assert tool_payload(h.call_tool(token, "get_spending_summary", {}))[0] is False
    resp = h.call_tool(token, "get_spending_summary", {})
    assert resp.status_code == 200
    is_error, out = tool_payload(resp)
    assert is_error and out == {"error": "rate_limited", "message": "Too many requests. Wait a minute and try again."}


def test_search_products_results() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    is_error, out = tool_payload(h.call_tool(token, "search_products", {"query": "milk bread"}))
    assert not is_error
    assert out["results"] == [
        {"sku": "GRC-MILK-1L", "name": "Fresh milk 1L", "merchant_id": "grocer", "price_minor": 690,
         "price": "RM6.90", "currency": "MYR"},
        {"sku": "GRC-BREAD-WW", "name": "Wholemeal bread", "merchant_id": "grocer", "price_minor": 450,
         "price": "RM4.50", "currency": "MYR"},
    ]
    is_error, out = tool_payload(h.call_tool(token, "search_products", {"query": "spaceship"}))
    assert not is_error and out == {"results": [], "message": "No matching products."}
    is_error, out = tool_payload(h.call_tool(token, "search_products", {"query": "usb", "merchant_id": "electronics",
                                                                        "max_results": 1}))
    assert len(out["results"]) == 1
    assert {r["merchant_id"] for r in out["results"]} == {"electronics"}
    assert len([e for e in h.store.conversation_events if e["event_type"] == "agent_response"]) == 3
    assert all(r["order_id"] is None for r in h.store.a2a_messages)


def test_search_all_merchants_down() -> None:
    import httpx

    clock_h = make_harness()
    down = lambda env, data: httpx.Response(500, json={})  # noqa: E731
    h = make_harness(store=clock_h.store, merchant_transports={
        c: ScriptedMerchant(c, clock_h.clock, down).transport() for c in ("grocer", "electronics")})
    token, _, _ = h.new_device()
    is_error, out = tool_payload(h.call_tool(token, "search_products", {"query": "milk"}))
    assert is_error and out == {"error": "merchant_unavailable",
                                "message": "The shops are not responding. Try again shortly."}


def test_worst_case_search_response_fits_3900_bytes() -> None:
    base = make_harness()

    def huge(envelope: dict, data: dict) -> None:
        data["products"] = [
            {"sku": f"GRC-{i:02d}-" + "X" * 57, "name": '"' * 80, "unit_price_minor": 9_999_999, "currency": "MYR"}
            for i in range(10)
        ]

    h = make_harness(store=base.store, merchant_transports={
        "grocer": ScriptedMerchant("grocer", base.clock, huge).transport()})
    token, _, _ = h.new_device()
    resp = h.call_tool(token, "search_products", {"query": "milk", "merchant_id": "grocer", "max_results": 10})
    assert len(resp.content) <= 3900
    is_error, out = tool_payload(resp)
    assert not is_error and out["truncated"] is True
    assert 0 < len(out["results"]) < 10


def test_internal_exception_becomes_internal_error() -> None:
    h = make_harness()
    token, _, _ = h.new_device()

    async def boom(user_id: str) -> dict:
        raise RuntimeError("kaboom")

    h.services.spending.tool_summary = boom  # type: ignore[method-assign]
    resp = h.call_tool(token, "get_spending_summary", {})
    assert resp.status_code == 200
    is_error, out = tool_payload(resp)
    assert is_error and out == {"error": "internal_error", "message": "Something went wrong. Try again later."}


def test_compact_json_text() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    resp = h.call_tool(token, "get_spending_summary", {})
    text = resp.json()["result"]["content"][0]["text"]
    assert ": " not in text and ", " not in text
