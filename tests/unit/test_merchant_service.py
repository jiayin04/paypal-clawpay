"""Untrusted merchant-response validation: one failing case per §2.5 row (AC2.8)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest

from app.services.audit_service import AuditService
from app.services.merchant_service import A2ALog, MerchantFailure, MerchantService
from tests.fakes.app import make_settings
from tests.fakes.merchants import ScriptedMerchant
from tests.fakes.store import InMemoryStore

MILK = [{"sku": "GRC-MILK-1L", "qty": 2}]


def _service(transports: dict[str, Any] | None = None, **settings: Any) -> tuple[MerchantService, InMemoryStore]:
    store = InMemoryStore()
    repos = store.repositories()
    svc = MerchantService(make_settings(**settings), store.clock, AuditService(repos.audit), repos.a2a_messages,
                          transports)
    return svc, store


def _scripted(mutate: Any, code: str = "grocer") -> tuple[MerchantService, InMemoryStore, ScriptedMerchant]:
    store = InMemoryStore()
    merchant = ScriptedMerchant(code, store.clock, mutate)
    repos = store.repositories()
    svc = MerchantService(make_settings(), store.clock, AuditService(repos.audit), repos.a2a_messages,
                          {code: merchant.transport()})
    return svc, store, merchant


@pytest.mark.anyio
async def test_valid_quote_recomputed_and_expiry_capped() -> None:
    svc, store, _ = _scripted(None)
    quote = await svc.request_quote("grocer", MILK, "MYR")
    assert quote.total_minor == 1380
    assert [(line.sku, line.qty, line.line_total_minor) for line in quote.lines] == [("GRC-MILK-1L", 2, 1380)]
    assert quote.expires_at == store.clock.utc_now() + timedelta(seconds=300)


@pytest.mark.anyio
async def test_quote_ttl_caps_merchant_expiry() -> None:
    store = InMemoryStore()
    merchant = ScriptedMerchant("grocer", store.clock, None)
    repos = store.repositories()
    svc = MerchantService(make_settings(quote_ttl_seconds=60), store.clock, AuditService(repos.audit),
                          repos.a2a_messages, {"grocer": merchant.transport()})
    quote = await svc.request_quote("grocer", MILK, "MYR")
    assert quote.expires_at == store.clock.utc_now() + timedelta(seconds=60)


def _set(key: str, value: Any):  # type: ignore[no-untyped-def]
    def mutate(envelope: dict, data: dict) -> None:
        data[key] = value
    return mutate


def _line(key: str, value: Any):  # type: ignore[no-untyped-def]
    def mutate(envelope: dict, data: dict) -> None:
        data["items"][0][key] = value
    return mutate


def _extra_line(envelope: dict, data: dict) -> None:
    data["items"].append({"sku": "GRC-EGGS-10", "name": "Eggs", "qty": 1, "unit_price_minor": 1290,
                          "line_total_minor": 1290})
    data["subtotal_minor"] += 1290
    data["total_minor"] += 1290


def _missing_line(envelope: dict, data: dict) -> None:
    data["items"].pop()


def _consistent_price(value: Any):  # type: ignore[no-untyped-def]
    """Tamper the unit price but keep totals consistent with it, so only the price rule fires."""
    def mutate(envelope: dict, data: dict) -> None:
        data["items"][0]["unit_price_minor"] = value
        if isinstance(value, int):
            data["items"][0]["line_total_minor"] = value * data["items"][0]["qty"]
            data["subtotal_minor"] = data["total_minor"] = value * data["items"][0]["qty"]
    return mutate


QUOTE_CASES = {
    "wrong_merchant_id": _set("merchant_id", "electronics"),
    "wrong_correlation_id": _set("correlation_id", "00000000-0000-0000-0000-000000000000"),
    "extra_sku": _extra_line,
    "missing_sku": _missing_line,
    "qty_changed": _line("qty", 3),
    "price_zero": _consistent_price(0),
    "price_negative": _consistent_price(-5),
    "price_float": _consistent_price(6.9),
    "price_string": _consistent_price("690"),
    "price_too_large": _consistent_price(10_000_001),
    "currency_mismatch": _set("currency", "USD"),
    "stated_line_total_wrong": _line("line_total_minor", 1),
    "stated_subtotal_wrong": _set("subtotal_minor", 1),
    "stated_total_wrong": _set("total_minor", 1),
    "bad_quote_id": _set("quote_id", "has spaces!"),
    "long_quote_id": _set("quote_id", "q" * 65),
    "bad_sku_format": _line("sku", "grc-milk-1l"),
    "long_name": _line("name", "N" * 81),
    "missing_field": lambda env, data: data.pop("expires_at"),
    "naive_expiry": _set("expires_at", "2026-10-24T05:05:00"),
    "unparseable_expiry": _set("expires_at", "soon"),
}


@pytest.mark.anyio
@pytest.mark.parametrize("case", list(QUOTE_CASES))
async def test_tampered_quote_rejected(case: str) -> None:
    svc, store, _ = _scripted(QUOTE_CASES[case])
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.request_quote("grocer", MILK, "MYR")
    assert exc_info.value.code == "quote_invalid"
    rejected = store.audit_events("merchant.response_rejected")
    assert len(rejected) == 1
    assert set(rejected[0]["details"]) == {"merchant", "reason"}


@pytest.mark.anyio
async def test_missing_line_of_two_rejected_by_item_rule() -> None:
    def drop_second(envelope: dict, data: dict) -> None:
        removed = data["items"].pop()
        data["subtotal_minor"] -= removed["line_total_minor"]
        data["total_minor"] -= removed["line_total_minor"]

    svc, store, _ = _scripted(drop_second)
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.request_quote("grocer", [{"sku": "GRC-MILK-1L", "qty": 1}, {"sku": "GRC-EGGS-10", "qty": 1}], "MYR")
    assert exc_info.value.reason == "items"


@pytest.mark.anyio
@pytest.mark.parametrize(("seconds", "ok"), [(-10, False), (29, False), (31, True), (1800, True), (1801, False)])
async def test_expiry_window(seconds: int, ok: bool) -> None:
    store = InMemoryStore()

    def mutate(envelope: dict, data: dict) -> None:
        data["expires_at"] = (store.clock.utc_now() + timedelta(seconds=seconds)).isoformat()

    merchant = ScriptedMerchant("grocer", store.clock, mutate)
    repos = store.repositories()
    svc = MerchantService(make_settings(), store.clock, AuditService(repos.audit), repos.a2a_messages,
                          {"grocer": merchant.transport()})
    if ok:
        quote = await svc.request_quote("grocer", MILK, "MYR")
        assert quote.expires_at <= store.clock.utc_now() + timedelta(seconds=300)
    else:
        with pytest.raises(MerchantFailure) as exc_info:
            await svc.request_quote("grocer", MILK, "MYR")
        assert exc_info.value.code == "quote_invalid"


TRANSPORT_CASES = {
    "http_500": (lambda env, data: httpx.Response(500, json={}), "merchant_unavailable"),
    "http_404": (lambda env, data: httpx.Response(404, json={}), "merchant_unavailable"),
    "jsonrpc_error": (lambda env, data: httpx.Response(200, json={"jsonrpc": "2.0", "id": env["id"],
                                                                  "error": {"code": -1, "message": "x"}}),
                      "merchant_unavailable"),
    "wrong_rpc_id": (lambda env, data: httpx.Response(200, json={**env, "id": "other"}), "quote_invalid"),
    "not_json_content_type": (lambda env, data: httpx.Response(200, text="hello"), "quote_invalid"),
    "invalid_json": (lambda env, data: httpx.Response(200, content=b"{", headers={"content-type": "application/json"}),
                     "quote_invalid"),
    "no_data_part": (lambda env, data: httpx.Response(200, json={**env, "result": {"kind": "message", "parts": []}}),
                     "quote_invalid"),
    "body_too_large": (lambda env, data: httpx.Response(200, json={**env, "pad": "x" * 70000}), "quote_invalid"),
    "connect_error": (lambda env, data: (_ for _ in ()).throw(httpx.ConnectError("down")), "merchant_unavailable"),
    "read_timeout": (lambda env, data: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "merchant_unavailable"),
}


@pytest.mark.anyio
@pytest.mark.parametrize("case", list(TRANSPORT_CASES))
async def test_transport_failures(case: str) -> None:
    mutate, expected = TRANSPORT_CASES[case]
    svc, _, _ = _scripted(mutate)
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.request_quote("grocer", MILK, "MYR")
    assert exc_info.value.code == expected


@pytest.mark.anyio
async def test_slow_merchant_times_out() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    svc, _ = _service({"grocer": httpx.MockTransport(slow)}, merchant_agent_timeout_seconds=0.05)
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.request_quote("grocer", MILK, "MYR")
    assert exc_info.value.code == "merchant_unavailable"


@pytest.mark.anyio
async def test_merchant_error_responses_mapped() -> None:
    svc, _ = _service()
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.request_quote("grocer", [{"sku": "GRC-NOPE", "qty": 1}], "MYR")
    assert exc_info.value.code == "unknown_sku" and exc_info.value.sku == "GRC-NOPE"
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.request_quote("grocer", MILK, "USD")
    assert exc_info.value.code == "currency_not_supported"


@pytest.mark.anyio
async def test_default_currency_mismatch_rejected() -> None:
    store = InMemoryStore()
    merchant = ScriptedMerchant("grocer", store.clock, None)  # echoes requested currency
    svc = MerchantService(make_settings(default_currency="USD"), store.clock,
                          AuditService(store.repositories().audit), store.repositories().a2a_messages,
                          {"grocer": merchant.transport()})
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.request_quote("grocer", MILK, "MYR")
    assert exc_info.value.code == "quote_invalid"


# --- search ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_search_both_merchants_in_process() -> None:
    svc, _ = _service()
    products = await svc.search("usb milk", 10, None)
    merchants = {p.merchant_id for p in products}
    assert merchants == {"grocer", "electronics"}
    assert [p.merchant_id for p in products] == sorted(p.merchant_id for p in products)


@pytest.mark.anyio
async def test_search_partial_failure_returns_healthy_results() -> None:
    store = InMemoryStore()
    bad = ScriptedMerchant("electronics", store.clock, lambda env, data: httpx.Response(503, json={}))
    repos = store.repositories()
    svc = MerchantService(make_settings(), store.clock, AuditService(repos.audit), repos.a2a_messages,
                          {"electronics": bad.transport()})
    products = await svc.search("milk", 5, None)
    assert [p.sku for p in products] == ["GRC-MILK-1L"]


@pytest.mark.anyio
async def test_search_all_fail() -> None:
    store = InMemoryStore()
    down = lambda env, data: httpx.Response(500, json={})  # noqa: E731
    transports = {c: ScriptedMerchant(c, store.clock, down).transport() for c in ("grocer", "electronics")}
    repos = store.repositories()
    svc = MerchantService(make_settings(), store.clock, AuditService(repos.audit), repos.a2a_messages, transports)
    with pytest.raises(MerchantFailure) as exc_info:
        await svc.search("milk", 5, None)
    assert exc_info.value.code == "merchant_unavailable"


@pytest.mark.anyio
async def test_search_drops_invalid_products_individually() -> None:
    def mutate(envelope: dict, data: dict) -> None:
        data["products"] = [
            {"sku": "GRC-OK", "name": "Fine", "unit_price_minor": 100, "currency": "MYR"},
            {"sku": "GRC-FLOAT", "name": "Float", "unit_price_minor": 1.5, "currency": "MYR"},
            {"sku": "GRC-ZERO", "name": "Zero", "unit_price_minor": 0, "currency": "MYR"},
            {"sku": "GRC-USD", "name": "Dollar", "unit_price_minor": 100, "currency": "USD"},
            {"sku": "lower", "name": "Bad sku", "unit_price_minor": 100, "currency": "MYR"},
            {"sku": "GRC-LONG", "name": "L" * 81, "unit_price_minor": 100, "currency": "MYR"},
        ]

    svc, _, _ = _scripted(mutate)
    products = await svc.search("milk", 10, "grocer")
    assert [p.sku for p in products] == ["GRC-OK"]


@pytest.mark.anyio
async def test_search_wrong_merchant_identity_rejected() -> None:
    svc, store, _ = _scripted(_set("merchant_id", "electronics"))
    with pytest.raises(MerchantFailure):
        await svc.search("milk", 5, "grocer")
    assert store.audit_events("merchant.response_rejected")


@pytest.mark.anyio
async def test_a2a_log_rows_and_payload_rules() -> None:
    svc, store = _service()
    log = A2ALog(user_id="u1")
    await svc.request_quote("grocer", MILK, "MYR", log)
    await svc.write_log(log, "order-1")
    assert [(r["direction"], r["outcome"]) for r in store.a2a_messages] == [("request", "ok"), ("response", "ok")]
    assert all(r["order_id"] == "order-1" for r in store.a2a_messages)
    assert store.a2a_messages[0]["payload"]["type"] == "clawpay.quote_request.v1"
    assert store.a2a_messages[1]["summary"]["total_minor"] == 1380

    svc2, store2, _ = _scripted(_set("total_minor", 1))
    log2 = A2ALog(user_id="u1")
    with pytest.raises(MerchantFailure):
        await svc2.request_quote("grocer", MILK, "MYR", log2)
    await svc2.write_log(log2, None)
    rejected = store2.a2a_messages[1]
    assert rejected["outcome"] == "rejected" and rejected["payload"] == {}
    assert rejected["summary"]["reason"] == "total"

    search_log = A2ALog(user_id="u1")
    await svc.search("milk", 5, "grocer", search_log)
    await svc.write_log(search_log, "order-should-not-stick")
    assert all(r["order_id"] is None for r in store.a2a_messages[2:])


@pytest.mark.anyio
async def test_a2a_log_write_failure_is_swallowed() -> None:
    svc, store = _service()
    log = A2ALog(user_id="u1")
    await svc.search("milk", 5, "grocer", log)
    store.fail_next("a2a_messages.insert", "08006", times=5)
    await svc.write_log(log)  # no exception


def test_large_payload_omitted() -> None:
    from app.services.merchant_service import _payload

    assert _payload({"x": "y" * 20000}) == {"omitted": "too_large"}
    assert _payload({"x": "y"}) == {"x": "y"}
