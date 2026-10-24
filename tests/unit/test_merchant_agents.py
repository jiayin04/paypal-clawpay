"""Deterministic mock merchant agents (A2A subset), exercised in-process."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.merchant_agents import catalogs
from app.merchant_agents.app import create_merchant_app, electronics_app, grocer_app
from app.main import create_app
from tests.fakes.app import make_settings


def _send(client: TestClient, data: dict, rpc_id: str = "c1") -> dict:
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": "message/send", "params": {"message": {
        "kind": "message", "role": "user", "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "data", "data": data}]}}}
    resp = client.post("/a2a", json=body)
    assert resp.status_code == 200
    out = resp.json()
    assert out["id"] == rpc_id
    return out["result"]["parts"][0]["data"]


def test_catalog_prices_and_boundaries() -> None:
    prices = {p.sku: p.unit_price_minor for code in catalogs.CATALOGS for p in catalogs.products(code)}
    assert prices["GRC-MILK-1L"] == 690
    assert prices["GRC-BREAD-WW"] == 450
    assert prices["GRC-EGGS-10"] == 1290
    assert prices["GRC-RICE-5KG"] == 2690
    assert prices["GRC-COFFEE-250G"] == 1890
    assert prices["ELC-USBC-CABLE"] == 2490
    assert prices["ELC-POWERBANK"] == 8990
    assert prices["ELC-USBC-HUB"] == 10000
    assert prices["ELC-EARBUDS"] == 12900
    assert prices["ELC-KEYBOARD"] == 25900


def test_search_and_quote() -> None:
    client = TestClient(grocer_app())
    data = _send(client, {"type": "clawpay.search_request.v1", "correlation_id": "c1", "query": "Milk and BREAD",
                          "max_results": 5})
    assert data["merchant_id"] == "grocer"
    assert [p["sku"] for p in data["products"]] == ["GRC-MILK-1L", "GRC-BREAD-WW"]
    quote = _send(client, {"type": "clawpay.quote_request.v1", "correlation_id": "c1",
                           "items": [{"sku": "GRC-MILK-1L", "qty": 2}], "currency": "MYR"})
    assert quote["type"] == "clawpay.quote_response.v1"
    assert quote["total_minor"] == quote["subtotal_minor"] == 1380
    assert quote["expires_at"].endswith("Z")


def test_quote_errors() -> None:
    client = TestClient(electronics_app())
    err = _send(client, {"type": "clawpay.quote_request.v1", "correlation_id": "c1",
                         "items": [{"sku": "GRC-MILK-1L", "qty": 1}], "currency": "MYR"})
    assert err == {"type": "clawpay.error.v1", "correlation_id": "c1", "code": "unknown_sku", "sku": "GRC-MILK-1L"}
    err = _send(client, {"type": "clawpay.quote_request.v1", "correlation_id": "c1",
                         "items": [{"sku": "ELC-USBC-HUB", "qty": 1}], "currency": "EUR"})
    assert err["code"] == "currency_not_supported"
    err = _send(client, {"type": "unknown", "correlation_id": "c1"})
    assert err["code"] == "bad_request"


def test_jsonrpc_errors_and_agent_card() -> None:
    client = TestClient(create_merchant_app("grocer"))
    assert client.post("/a2a", content=b"{").json()["error"]["code"] == -32700
    assert client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "x"}).json()["error"]["code"] == -32601
    card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "Grocer Demo"
    assert {s["id"] for s in card["skills"]} == {"search", "quote"}
    with pytest.raises(ValueError):
        create_merchant_app("bank")


def test_mock_agents_not_mounted_on_main_app() -> None:
    client = TestClient(create_app(make_settings()))
    assert client.post("/a2a", json={}).status_code == 404
    assert client.get("/.well-known/agent-card.json").status_code == 404
