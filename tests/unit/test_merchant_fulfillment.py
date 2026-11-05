"""Merchant registry, the reserve / confirm / cancel A2A messages, and reserve-before-pay."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from app.integrations.mcp import tools as mcp_tools
from app.merchant_agents import registry
from app.merchant_agents.app import create_merchant_app
from app.services.audit_service import AuditService
from app.services.merchant_service import A2ALog, MerchantFailure, MerchantService
from tests.fakes.app import make_settings, tool_payload
from tests.fakes.approvals import get_challenge, sign_canonical
from tests.fakes.clock import FakeClock
from tests.fakes.flow import approve, setup_flow
from tests.fakes.merchants import ScriptedMerchant
from tests.fakes.store import InMemoryStore

ALL_DEMO = "grocer,electronics,shopee,foodpanda,grab"
CASE = {"merchant_id": "shopee", "items": [{"sku": "SHP-PHONE-CASE", "qty": 2}]}
SOLD_OUT = {"merchant_id": "shopee", "items": [{"sku": "SHP-PLUSH-LTD", "qty": 1}]}


# --- registry and settings ---------------------------------------------------------------


def test_default_merchants_unchanged() -> None:
    settings = make_settings()
    assert settings.merchant_codes == ("grocer", "electronics")
    assert mcp_tools.tools_for(settings.merchant_codes) == mcp_tools.TOOLS


def test_enabled_merchants_follow_registry_order() -> None:
    settings = make_settings(enabled_merchants="grab, shopee,FOODPANDA")
    assert settings.merchant_codes == ("shopee", "foodpanda", "grab")
    settings = make_settings(enabled_merchants=ALL_DEMO, channel3_api_key="c3-key")
    assert settings.merchant_codes == ("grocer", "electronics", "channel3", "shopee", "foodpanda", "grab")
    enum = mcp_tools.tools_for(settings.merchant_codes)[0]["inputSchema"]["properties"]["merchant_id"]["enum"]
    assert enum == list(settings.merchant_codes)


def test_merchant_settings_validation() -> None:
    with pytest.raises(ValueError, match="ENABLED_MERCHANTS"):
        make_settings(enabled_merchants="grocer,lazada")
    with pytest.raises(ValueError, match="ENABLED_MERCHANTS"):
        make_settings(enabled_merchants="channel3")
    with pytest.raises(ValueError, match="registry"):
        make_settings(merchant_agent_endpoints="lazada=https://agent.example.com/a2a")
    with pytest.raises(ValueError, match=r"MERCHANT_AGENT_ENDPOINTS\[shopee\]"):
        make_settings(merchant_agent_endpoints="shopee=http://example.com/a2a")
    with pytest.raises(ValueError, match="code=url"):
        make_settings(merchant_agent_endpoints="shopee")


def test_endpoints_override_legacy_and_enable_api_merchants() -> None:
    settings = make_settings(
        grocer_agent_endpoint="http://127.0.0.1:8101/a2a",
        merchant_agent_endpoints="grocer=http://127.0.0.1:9101/a2a/,channel3=https://c3.example.com/a2a",
    )
    assert registry.endpoint(settings, "grocer") == "http://127.0.0.1:9101/a2a"
    assert registry.endpoint(settings, "electronics") is None
    assert settings.channel3_enabled and "channel3" in settings.merchant_codes


def test_remote_merchant_needs_an_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = registry.MerchantSpec("partner", "remote")
    monkeypatch.setattr(registry, "MERCHANTS", (*registry.MERCHANTS, spec))
    monkeypatch.setitem(registry.BY_CODE, "partner", spec)
    assert "partner" not in make_settings().merchant_codes
    settings = make_settings(merchant_agent_endpoints="partner=https://agent.partner.example/a2a")
    assert settings.merchant_codes[-1] == "partner"
    with pytest.raises(LookupError, match="MERCHANT_AGENT_ENDPOINTS"):
        registry.build_app("partner", make_settings(), FakeClock())


def test_every_catalog_merchant_has_a_catalog_and_agent() -> None:
    from app.merchant_agents import catalogs

    assert set(registry.CATALOG_CODES) == set(catalogs.CATALOGS)
    for code in registry.CATALOG_CODES:
        card = TestClient(registry.build_app(code, make_settings(), FakeClock())).get(
            "/.well-known/agent-card.json").json()
        assert {s["id"] for s in card["skills"]} >= {"reserve", "confirm", "cancel"}


# --- mock agent: reserve / confirm / cancel --------------------------------------------------


def _send(client: TestClient, data: dict[str, Any]) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": "c1", "method": "message/send", "params": {"message": {
        "kind": "message", "role": "user", "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "data", "data": {"correlation_id": "c1", **data}}]}}}
    return client.post("/a2a", json=body).json()["result"]["parts"][0]["data"]


def _reserve(client: TestClient, order_ref: str, sku: str = "SHP-PHONE-CASE", qty: int = 2,
             price: int = 1590, total: int | None = None) -> dict[str, Any]:
    return _send(client, {"type": "clawpay.reserve_request.v1", "order_ref": order_ref, "currency": "MYR",
                          "items": [{"sku": sku, "qty": qty, "unit_price_minor": price}],
                          "total_minor": qty * price if total is None else total})


def test_agent_reserve_is_idempotent_and_holds_stock() -> None:
    clock = FakeClock()
    app = create_merchant_app("shopee", clock=clock)
    client, book = TestClient(app), app.state.reservations
    first = _reserve(client, "order-1")
    assert first["type"] == "clawpay.reserve_response.v1"
    assert first["items"] == [{"sku": "SHP-PHONE-CASE", "qty": 2}] and first["total_minor"] == 3180
    assert _reserve(client, "order-1")["reservation_id"] == first["reservation_id"]
    assert book.available("SHP-PHONE-CASE") == 48
    assert _reserve(client, "order-1", qty=3)["code"] == "bad_request"  # same order, different cart
    clock.advance(16 * 60)  # hold expired
    assert book.available("SHP-PHONE-CASE") == 50


@pytest.mark.parametrize(("kwargs", "code"), [
    ({"sku": "SHP-PLUSH-LTD", "qty": 1, "price": 5900}, "out_of_stock"),
    ({"price": 1500}, "price_changed"),
    ({"sku": "GRC-MILK-1L", "price": 690}, "unknown_sku"),
    ({"total": 1}, "bad_request"),
    ({"qty": 21}, "bad_request"),
])
def test_agent_reserve_refusals(kwargs: dict[str, Any], code: str) -> None:
    out = _reserve(TestClient(create_merchant_app("shopee")), "order-1", **kwargs)
    assert out["type"] == "clawpay.error.v1" and out["code"] == code


def test_agent_confirm_and_cancel_rules() -> None:
    app = create_merchant_app("grab")
    client, book = TestClient(app), app.state.reservations
    res = _reserve(client, "order-1", sku="GRB-RIDE-KLCC-KLS", qty=1, price=1800)
    confirm = {"type": "clawpay.confirm_request.v1", "order_ref": "order-1", "reservation_id": res["reservation_id"]}
    first = _send(client, confirm)
    assert first["status"] == "accepted" and first["fulfillment_id"]
    assert _send(client, confirm)["fulfillment_id"] == first["fulfillment_id"]
    assert book.available("GRB-RIDE-KLCC-KLS") == 19
    assert _send(client, {"type": "clawpay.cancel_request.v1", "order_ref": "order-1"})["code"] == "already_confirmed"
    assert _send(client, {**confirm, "order_ref": "order-2"})["code"] == "unknown_reservation"

    other = _reserve(client, "order-3", sku="GRB-RIDE-KLCC-KLS", qty=1, price=1800)
    cancelled = _send(client, {"type": "clawpay.cancel_request.v1", "order_ref": "order-3"})
    assert cancelled["status"] == "released"
    assert book.available("GRB-RIDE-KLCC-KLS") == 19
    again = {"type": "clawpay.confirm_request.v1", "order_ref": "order-3", "reservation_id": other["reservation_id"]}
    assert _send(client, again)["code"] == "reservation_expired"
    # Cancelling something never held is a no-op.
    assert _send(client, {"type": "clawpay.cancel_request.v1", "order_ref": "nothing"})["status"] == "released"


# --- ClawPay side: untrusted fulfillment answers ---------------------------------------------

LINES = [{"sku": "GRC-MILK-1L", "qty": 2, "unit_price_minor": 690}]


def _service(mutate: Any = None) -> tuple[MerchantService, InMemoryStore]:
    store = InMemoryStore()
    repos = store.repositories()
    transports = {"grocer": ScriptedMerchant("grocer", store.clock, mutate).transport()} if mutate else None
    return MerchantService(make_settings(), store.clock, AuditService(repos.audit), repos.a2a_messages,
                           transports), store


def _set(key: str, value: Any):  # type: ignore[no-untyped-def]
    def mutate(envelope: dict, data: dict) -> None:
        data[key] = value
    return mutate


@pytest.mark.anyio
async def test_reserve_confirm_cancel_in_process_and_logged() -> None:
    svc, store = _service()
    log = A2ALog(user_id="u1")
    held = await svc.reserve("grocer", "order-1", LINES, "MYR", 1380, log)
    done = await svc.confirm("grocer", "order-1", held.reservation_id, log)
    assert done.fulfillment_id.startswith("gro_f_")
    await svc.write_log(log, "order-1")
    assert [(r["method"], r["direction"]) for r in store.a2a_messages] == [
        ("reserve", "request"), ("reserve", "response"), ("confirm", "request"), ("confirm", "response")]
    assert all(r["order_id"] == "order-1" for r in store.a2a_messages)
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.cancel("grocer", "order-1", held.reservation_id)
    assert exc_info.value.code == "already_confirmed"


RESERVE_CASES = {
    "wrong_merchant": _set("merchant_id", "electronics"),
    "wrong_order_ref": _set("order_ref", "someone-else"),
    "wrong_total": _set("total_minor", 1),
    "wrong_currency": _set("currency", "USD"),
    "wrong_items": _set("items", [{"sku": "GRC-MILK-1L", "qty": 1}]),
    "bad_reservation_id": _set("reservation_id", "has spaces"),
    "expires_too_soon": lambda env, data: data.update(expires_at=data["expires_at"][:11] + "00:00:00Z"),
}


@pytest.mark.anyio
@pytest.mark.parametrize("case", list(RESERVE_CASES))
async def test_tampered_reserve_rejected(case: str) -> None:
    svc, store = _service(RESERVE_CASES[case])
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.reserve("grocer", "order-1", LINES, "MYR", 1380)
    assert exc_info.value.code == "fulfillment_invalid"
    assert store.audit_events("merchant.response_rejected")


@pytest.mark.anyio
async def test_tampered_confirm_rejected() -> None:
    svc, _ = _service(_set("reservation_id", "res_other"))
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.confirm("grocer", "order-1", "res_test_1")
    assert exc_info.value.code == "fulfillment_invalid" and exc_info.value.reason == "reservation_id"


@pytest.mark.anyio
async def test_merchant_errors_mapped() -> None:
    svc, _ = _service()
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.reserve("grocer", "order-1", [{**LINES[0], "unit_price_minor": 100}], "MYR", 200)
    assert exc_info.value.code == "price_changed" and exc_info.value.sku == "GRC-MILK-1L"
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.confirm("grocer", "order-1", "res_unknown")
    assert exc_info.value.code == "reservation_lost"


# --- reserve-before-pay through the HTTP device flow -------------------------------------------


def _order_and_sign(h, private, device, cart):  # type: ignore[no-untyped-def]
    token = device["device_token"]
    is_error, order = tool_payload(h.call_tool(token, "create_order", cart))
    assert not is_error, order
    ch = get_challenge(h, token, order["order_id"])
    body = {"challenge_id": ch["challenge_id"], "device_id": device["device_id"], "key_version": ch["key_version"],
            "nonce": ch["nonce"], "decision": "approved", "signature": sign_canonical(private, ch["canonical"])}
    return order["order_id"], body


def _status(h, device, order_id):  # type: ignore[no-untyped-def]
    is_error, out = tool_payload(h.call_tool(device["device_token"], "get_order_status", {"order_id": order_id}))
    assert not is_error
    return out


def _creates(paypal) -> int:  # type: ignore[no-untyped-def]
    return len([c for c in paypal.calls if c.path == "/v2/checkout/orders"])


def _stock(h, code: str, sku: str) -> int | None:  # type: ignore[no-untyped-def]
    return h.services.merchants._apps[code].state.reservations.available(sku)


def test_paid_order_is_reserved_then_confirmed() -> None:
    h, paypal, _, private, device = setup_flow(enabled_merchants=ALL_DEMO)
    is_error, search = tool_payload(h.call_tool(device["device_token"], "search_products", {"query": "noodles tea"}))
    assert not is_error and {r["merchant_id"] for r in search["results"]} == {"foodpanda"}
    oid, body = _order_and_sign(h, private, device, CASE)
    r = approve(h, device, oid, body)
    assert r.status_code == 200 and r.json()["status"] == "paid", r.text
    assert r.json()["fulfillment_status"] == "confirmed"
    order = h.store.orders[oid]
    assert order["fulfillment_status"] == "confirmed" and order["merchant_fulfillment_id"].startswith("sho_f_")
    assert _stock(h, "shopee", "SHP-PHONE-CASE") == 48
    status = _status(h, device, oid)
    assert status["status"] == "paid" and status["fulfillment_status"] == "confirmed"
    assert status["merchant_reference"] == order["merchant_fulfillment_id"]
    methods = [(m["method"], m["direction"]) for m in h.store.a2a_messages if m["order_id"] == oid]
    assert ("reserve", "response") in methods and ("confirm", "response") in methods


def test_sold_out_item_is_never_charged() -> None:
    h, paypal, uid, private, device = setup_flow(enabled_merchants=ALL_DEMO)
    oid, body = _order_and_sign(h, private, device, SOLD_OUT)
    r = approve(h, device, oid, body)
    assert r.status_code == 200 and r.json()["status"] == "payment_failed", r.text
    assert "out of stock" in r.json()["message"] and r.json()["fulfillment_status"] == "reservation_failed"
    assert _creates(paypal) == 0
    attempt = next(iter(h.store.payment_attempts.values()))
    assert attempt["failure_code"] == "merchant_out_of_stock" and attempt["first_sent_at"] is None
    assert h.store.orders[oid]["status"] == "PAYMENT_FAILED"
    assert h.store.spending_records[0]["status"] == "released"
    assert _status(h, device, oid)["fulfillment_status"] == "reservation_failed"


def test_declined_payment_releases_the_reservation() -> None:
    h, paypal, _, private, device = setup_flow(mode="instrument_declined", enabled_merchants=ALL_DEMO)
    oid, body = _order_and_sign(h, private, device, CASE)
    r = approve(h, device, oid, body)
    assert r.json()["status"] == "payment_failed" and r.json()["fulfillment_status"] == "released", r.text
    assert _stock(h, "shopee", "SHP-PHONE-CASE") == 50


def test_unreachable_merchant_blocks_payment() -> None:
    store = InMemoryStore()
    down = ScriptedMerchant("grocer", store.clock, lambda env, data: (
        httpx.Response(503, json={}) if data["type"] == "clawpay.reserve_response.v1" else None))
    h, paypal, _, private, device = setup_flow(store=store, merchant_transports={"grocer": down.transport()})
    oid, body = _order_and_sign(h, private, device, {"merchant_id": "grocer",
                                                     "items": [{"sku": "GRC-MILK-1L", "qty": 1}]})
    r = approve(h, device, oid, body)
    assert r.json()["status"] == "payment_failed" and "did not respond" in r.json()["message"], r.text
    assert _creates(paypal) == 0
    # ClawPay asked the merchant to release anything it might be holding.
    assert any(req["params"]["message"]["parts"][0]["data"]["type"] == "clawpay.cancel_request.v1"
               for req in down.requests)


def test_confirm_failure_keeps_payment_and_retries_on_status() -> None:
    h, paypal, _, private, device = setup_flow(enabled_merchants=ALL_DEMO)
    real_confirm = h.services.merchants.confirm
    calls = {"n": 0}

    async def flaky_confirm(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise MerchantFailure("merchant_unavailable", "timeout")
        return await real_confirm(*args, **kwargs)

    h.services.merchants.confirm = flaky_confirm  # type: ignore[method-assign]
    oid, body = _order_and_sign(h, private, device, CASE)
    r = approve(h, device, oid, body)
    assert r.json()["status"] == "paid" and r.json()["fulfillment_status"] == "fulfillment_pending", r.text
    assert h.store.orders[oid]["status"] == "PAID"
    status = _status(h, device, oid)
    assert status["status"] == "paid" and status["fulfillment_status"] == "confirmed" and status["merchant_reference"]


def test_lost_reservation_is_reheld_before_confirm() -> None:
    h, paypal, _, private, device = setup_flow(enabled_merchants=ALL_DEMO)
    paypal.oauth_timeout = True  # inline payment cannot finish; the reconciler completes it later
    oid, body = _order_and_sign(h, private, device, CASE)
    assert approve(h, device, oid, body).json()["status"] == "payment_pending"
    assert h.store.orders[oid]["fulfillment_status"] == "reserved"
    paypal.oauth_timeout = False
    # The merchant agent restarts and forgets its holds before the payment completes.
    book = h.services.merchants._apps["shopee"].state.reservations
    book._by_id.clear()
    book._by_order.clear()
    h.clock.advance(61)
    anyio.run(h.services.reconciliation.run_once)
    assert h.store.orders[oid]["status"] == "PAID"
    status = _status(h, device, oid)
    assert status["fulfillment_status"] == "confirmed", status
    assert _stock(h, "shopee", "SHP-PHONE-CASE") == 48


def test_reconciler_never_charges_an_unreserved_order() -> None:
    h, paypal, _, private, device = setup_flow()

    async def broken_reserve(order_id: str, user_id: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("crashed before reserving")

    h.services.fulfillment.reserve = broken_reserve  # type: ignore[method-assign]
    oid, body = _order_and_sign(h, private, device, {"merchant_id": "grocer",
                                                     "items": [{"sku": "GRC-MILK-1L", "qty": 1}]})
    assert approve(h, device, oid, body).json()["status"] == "payment_pending"
    h.clock.advance(61)
    anyio.run(h.services.reconciliation.run_once)
    assert _creates(paypal) == 0
    attempt = next(iter(h.store.payment_attempts.values()))
    assert attempt["status"] == "failed" and attempt["failure_code"] == "merchant_not_reserved"
    assert h.store.orders[oid]["status"] == "PAYMENT_FAILED"


def test_handoff_can_be_switched_off() -> None:
    h, paypal, _, private, device = setup_flow(enabled_merchants=ALL_DEMO, merchant_fulfillment_enabled=False)
    oid, body = _order_and_sign(h, private, device, SOLD_OUT)
    r = approve(h, device, oid, body)
    assert r.json()["status"] == "paid" and r.json()["fulfillment_status"] is None
    assert not [m for m in h.store.a2a_messages if m["method"] != "quote"]
    assert h.store.orders[oid].get("fulfillment_status", "none") == "none"


def test_reservation_expiry_window_is_bounded() -> None:
    assert timedelta(minutes=15) >= timedelta(seconds=make_settings().reconcile_replay_window_seconds)
