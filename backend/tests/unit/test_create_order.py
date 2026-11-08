"""create_order / get_order_status / get_spending_summary through MCP (AC2.9, AC2.6)."""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.canonical import cart_sha256
from tests.fakes.app import Harness, make_harness, tool_payload
from tests.fakes.merchants import ScriptedMerchant

MILK2 = {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 2}]}


def _create(h: Harness, token: str, args: dict[str, Any]) -> tuple[bool, Any]:
    return tool_payload(h.call_tool(token, "create_order", args))


def test_create_order_happy_path() -> None:
    h = make_harness()
    token, device_id, user_id = h.new_device()
    is_error, out = _create(h, token, MILK2)
    assert is_error is False, out
    assert out == {
        "order_id": out["order_id"],
        "merchant": "Grocer Demo",
        "items": [{"name": "Fresh milk 1L", "qty": 2, "line_total": "RM13.80"}],
        "total_minor": 1380,
        "total": "RM13.80",
        "currency": "MYR",
        "expires_at": "2026-10-24T05:05:00Z",
        "status": "awaiting_device_approval",
        "next_step": "call clawpay_request_approval",
    }
    order = h.store.orders[out["order_id"]]
    assert order["status"] == "AWAITING_APPROVAL"
    assert order["device_id"] == device_id and order["user_id"] == user_id
    assert order["total_minor"] == order["subtotal_minor"] == 1380
    assert order["cart_sha256"] == cart_sha256([{"sku": "GRC-MILK-1L", "qty": 2, "unit_price_minor": 690}])
    transitions = [(a["details"]["from"], a["details"]["to"]) for a in h.store.audit_events("order.status_changed")]
    assert transitions == [("CREATED", "QUOTED"), ("QUOTED", "AWAITING_APPROVAL")]
    assert h.store.audit_events("order.created")
    # a2a rows for the quote carry the created order id.
    quote_rows = [r for r in h.store.a2a_messages if r["method"] == "quote"]
    assert len(quote_rows) == 2 and all(r["order_id"] == out["order_id"] for r in quote_rows)
    # Conversation events: user-safe summary, empty payload.
    events = h.store.conversation_events
    assert [e["event_type"] for e in events] == ["agent_call", "agent_response"]
    assert all(e["payload"] == {} for e in events)
    assert events[1]["summary"]["order_id"] == out["order_id"]
    assert events[1]["order_id"] == out["order_id"]
    conv = next(iter(h.store.conversations.values()))
    assert conv["external_session_id"] == f"device:{device_id}:2026-10-24"


def test_totals_recomputed_from_tampered_merchant_never_trusted() -> None:
    store_h = make_harness()
    clock = store_h.clock

    def lie(envelope: dict, data: dict) -> None:
        data["items"][0]["unit_price_minor"] = 1
        data["items"][0]["line_total_minor"] = 2
        data["subtotal_minor"] = data["total_minor"] = 1  # inconsistent: rejected, not fixed

    h = make_harness(store=store_h.store, merchant_transports={"grocer": ScriptedMerchant("grocer", clock, lie).transport()})
    token, _, _ = h.new_device()
    is_error, out = _create(h, token, MILK2)
    assert is_error and out["error"] == "quote_invalid"
    assert h.store.orders == {}


def test_duplicate_skus_merged_and_limit() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    is_error, out = _create(h, token, {"merchant_id": "grocer", "items": [
        {"sku": "GRC-MILK-1L", "qty": 1}, {"sku": "GRC-MILK-1L", "qty": 2}]})
    assert not is_error and out["items"] == [{"name": "Fresh milk 1L", "qty": 3, "line_total": "RM20.70"}]
    is_error, out = _create(h, token, {"merchant_id": "grocer", "items": [
        {"sku": "GRC-MILK-1L", "qty": 15}, {"sku": "GRC-MILK-1L", "qty": 6}]})
    assert is_error and out["error"] == "invalid_arguments"


@pytest.mark.parametrize(("sku", "ok"), [("ELC-USBC-HUB", True), ("ELC-EARBUDS", False)])
def test_phone_threshold_boundary(sku: str, ok: bool) -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    is_error, out = _create(h, token, {"merchant_id": "electronics", "items": [{"sku": sku, "qty": 1}]})
    if ok:
        assert not is_error and out["total_minor"] == 10000
    else:
        assert is_error and out["error"] == "phone_approval_unavailable"
        assert "RM100" in out["message"]
        assert h.store.orders == {}


def test_threshold_10001_rejected() -> None:
    h = make_harness(phone_approval_threshold_minor=10000)

    def price_10001(envelope: dict, data: dict) -> None:
        data["items"][0]["unit_price_minor"] = 10001
        data["items"][0]["line_total_minor"] = 10001
        data["subtotal_minor"] = data["total_minor"] = 10001

    h2 = make_harness(store=h.store, merchant_transports={
        "electronics": ScriptedMerchant("electronics", h.clock, price_10001).transport()})
    token, _, _ = h2.new_device()
    is_error, out = _create(h2, token, {"merchant_id": "electronics", "items": [{"sku": "ELC-USBC-HUB", "qty": 1}]})
    assert is_error and out["error"] == "phone_approval_unavailable"


def test_daily_cap_precheck() -> None:
    h = make_harness()
    token, _, user = h.new_device()
    h.store.add_spending(user, 49000, "committed")
    h.store.add_spending(user, 500, "reserved")
    is_error, out = _create(h, token, MILK2)
    assert is_error and out["error"] == "daily_limit_exceeded"
    assert "RM5.00" in out["message"]
    ok_error, _ = _create(h, token, {"merchant_id": "grocer", "items": [{"sku": "GRC-BREAD-WW", "qty": 1}]})
    assert ok_error is False


def test_no_payment_method() -> None:
    h = make_harness()
    user = h.new_user(payment_method=False)
    token, _, _ = h.new_device(user)
    is_error, out = _create(h, token, MILK2)
    assert is_error and out == {"error": "no_payment_method",
                                "message": "Link a PayPal account in the ClawPay setup page first."}
    h2 = make_harness(store=h.store, order_require_payment_method=False)
    is_error, _ = _create(h2, token, MILK2)
    assert is_error is False


def test_dedup_within_60s_new_order_after() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    _, first = _create(h, token, MILK2)
    h.clock.advance(30)
    _, second = _create(h, token, MILK2)
    assert second["order_id"] == first["order_id"]
    assert second["expires_at"] == first["expires_at"]
    h.clock.advance(31)
    _, third = _create(h, token, MILK2)
    assert third["order_id"] != first["order_id"]
    assert len(h.store.orders) == 2
    _, other_cart = _create(h, token, {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 1}]})
    assert other_cart["order_id"] not in (first["order_id"], third["order_id"])


def test_dedup_is_per_device() -> None:
    h = make_harness()
    token_a, _, user = h.new_device()
    token_b, _, _ = h.new_device(user)
    _, a = _create(h, token_a, MILK2)
    _, b = _create(h, token_b, MILK2)
    assert a["order_id"] != b["order_id"]


def test_llm_identity_and_price_fields() -> None:
    h = make_harness()
    token, device_id, user = h.new_device()
    other_user = h.new_user()
    args = {**MILK2, "device_id": "00000000-0000-0000-0000-000000000001", "user_id": other_user}
    is_error, out = _create(h, token, args)
    assert not is_error
    order = h.store.orders[out["order_id"]]
    assert order["device_id"] == device_id and order["user_id"] == user
    for extra in ({"total_minor": 1}, {"price": "RM0.01"}, {"merchant_name": "x"}):
        is_error, out = _create(h, token, {**MILK2, **extra})
        assert is_error and out["error"] == "invalid_arguments", extra
    is_error, out = _create(h, token, {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 1,
                                                                            "unit_price_minor": 1}]})
    assert is_error and out["error"] == "invalid_arguments"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"merchant_id": "grocer", "items": [{"sku": "GRC-NOPE", "qty": 1}]}, "unknown_sku"),
        ({**MILK2, "currency": "USD"}, "currency_not_supported"),
        ({**MILK2, "currency": "myr"}, "invalid_arguments"),
        ({"merchant_id": "bank", "items": [{"sku": "x", "qty": 1}]}, "invalid_arguments"),
        ({"merchant_id": "grocer", "items": []}, "invalid_arguments"),
        ({"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 21}]}, "invalid_arguments"),
        ({"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": "2"}]}, "invalid_arguments"),
    ],
)
def test_create_order_errors(args: dict, code: str) -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    is_error, out = _create(h, token, args)
    assert is_error and out["error"] == code
    if code == "unknown_sku":
        assert "GRC-NOPE" in out["message"]


def test_inactive_merchant_unavailable() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    for m in h.store.merchants.values():
        m["status"] = "inactive"
    is_error, out = _create(h, token, MILK2)
    assert is_error and out["error"] == "merchant_unavailable"


def test_device_revoked_race_inside_rpc() -> None:
    h = make_harness()
    token, device_id, _ = h.new_device()
    h.store.before_rpc("create_order_from_quote", lambda s: s.devices[device_id].update(status="revoked"))
    is_error, out = _create(h, token, MILK2)
    assert is_error and out["error"] == "internal_error"
    assert h.store.orders == {}


def test_db_failure_is_internal_error_not_500() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    h.store.fail_next("create_order_from_quote", "40P01", times=2)
    is_error, out = _create(h, token, MILK2)
    assert is_error and out["error"] == "internal_error"
    h.store.fail_next("create_order_from_quote", "40P01")
    is_error, _ = _create(h, token, MILK2)
    assert is_error is False


# --- get_order_status ------------------------------------------------------------------


def _status(h: Harness, token: str, order_id: str) -> tuple[bool, Any]:
    return tool_payload(h.call_tool(token, "get_order_status", {"order_id": order_id}))


def test_get_order_status_and_uuid_normalisation() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    _, created = _create(h, token, MILK2)
    for variant in (created["order_id"], created["order_id"].upper()):
        is_error, out = _status(h, token, variant)
        assert not is_error
        assert out == {"order_id": created["order_id"], "status": "awaiting_device_approval", "paypal_status": None,
                       "total": "RM13.80", "currency": "MYR", "approved_at": None, "paid_at": None,
                       "message": "Waiting for approval on the device."}


def test_get_order_status_foreign_and_missing() -> None:
    h = make_harness()
    token_a, _, _ = h.new_device()
    token_b, _, _ = h.new_device()
    _, created = _create(h, token_a, MILK2)
    for token, oid in ((token_b, created["order_id"]), (token_a, "00000000-0000-0000-0000-000000000000")):
        is_error, out = _status(h, token, oid)
        assert is_error and out == {"error": "not_found", "message": "Order not found."}
    is_error, out = _status(h, token_a, "not-a-uuid")
    assert is_error and out["error"] == "invalid_arguments"


def test_expired_quote_reported_and_order_expired() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    _, created = _create(h, token, MILK2)
    h.clock.advance(301)
    is_error, out = _status(h, token, created["order_id"])
    assert not is_error and out["status"] == "expired"
    assert out["message"] == "The order expired before it was approved."
    assert h.store.orders[created["order_id"]]["status"] == "EXPIRED"
    assert h.store.audit_events("order.expired")


@pytest.mark.parametrize(
    ("db_status", "api", "fragment"),
    [
        ("PAID", "paid", "Payment completed."),
        ("PAYMENT_FAILED", "payment_failed", "If PayPal later confirms the payment"),
        ("REQUIRES_RECONCILIATION", "requires_reconciliation", "still processing"),
        ("DENIED", "denied", "declined on the device"),
        ("CANCELLED", "cancelled", "cancelled"),
    ],
)
def test_status_messages(db_status: str, api: str, fragment: str) -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    _, created = _create(h, token, MILK2)
    h.store.orders[created["order_id"]]["status"] = db_status
    _, out = _status(h, token, created["order_id"])
    assert out["status"] == api and fragment in out["message"]


# --- get_spending_summary ---------------------------------------------------------------


def test_spending_summary_shape() -> None:
    h = make_harness()
    token, _, user = h.new_device()
    h.store.add_spending(user, 1380, "committed")
    h.store.add_spending(user, 2000, "reserved")
    h.store.add_spending(user, 999, "released")
    is_error, out = tool_payload(h.call_tool(token, "get_spending_summary", {}))
    assert not is_error
    assert out == {"period": "today", "day": "2026-10-24", "timezone": "Asia/Kuala_Lumpur", "currency": "MYR",
                   "limit_minor": 50000, "committed_minor": 1380, "reserved_minor": 2000,
                   "remaining_minor": 46620, "remaining": "RM466.20"}
    is_error, out = tool_payload(h.call_tool(token, "get_spending_summary", {"period": "week"}))
    assert is_error and out["error"] == "invalid_arguments"


def test_spending_summary_never_negative() -> None:
    h = make_harness()
    token, _, user = h.new_device()
    h.store.add_spending(user, 60000, "committed")
    _, out = tool_payload(h.call_tool(token, "get_spending_summary", {"period": "today"}))
    assert out["remaining_minor"] == 0 and out["remaining"] == "RM0.00"
