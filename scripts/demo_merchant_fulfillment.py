"""Walkthrough demo for multi-merchant + reserve-before-pay (no DB, no PayPal, no hardware).

Run from the repo root:

    uv run python scripts/demo_merchant_fulfillment.py

It drives the real app through the in-memory test harness (fake PayPal Sandbox,
in-memory database, real P-256 device signatures, in-process merchant agents)
and narrates each step. Nothing leaves the machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow "uv run python scripts/..." without installing the test package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging  # noqa: E402

logging.disable(logging.CRITICAL)  # mute the app's request logs so the narration is readable

from tests.fakes.app import tool_payload  # noqa: E402
from tests.fakes.flow import approve, setup_flow  # noqa: E402
from tests.fakes.approvals import get_challenge, sign_canonical  # noqa: E402

ALL = "grocer,electronics,shopee,foodpanda,grab"


def rule(title: str) -> None:
    print("\n" + "=" * 68 + f"\n {title}\n" + "=" * 68)


def tool(h, device, name, args):  # type: ignore[no-untyped-def]
    is_error, out = tool_payload(h.call_tool(device["device_token"], name, args))
    return out


def place_order(h, private, device, cart):  # type: ignore[no-untyped-def]
    out = tool(h, device, "create_order", cart)
    oid = out["order_id"]
    print(f"  create_order -> {oid}  {out['total']} at {out['merchant']}")
    ch = get_challenge(h, device["device_token"], oid)
    print("  device shows the order and the user holds the button to sign it (P-256)...")
    body = {
        "challenge_id": ch["challenge_id"], "device_id": device["device_id"],
        "key_version": ch["key_version"], "nonce": ch["nonce"], "decision": "approved",
        "signature": sign_canonical(private, ch["canonical"]),
    }
    resp = approve(h, device, oid, body).json()
    print(f"  gadget-approval -> status={resp['status']}  fulfillment={resp.get('fulfillment_status')}")
    if resp.get("message"):
        print(f"                     message: {resp['message']}")
    return oid, resp


def paypal_charges(paypal) -> int:  # type: ignore[no-untyped-def]
    return len([c for c in paypal.calls if c.path == "/v2/checkout/orders"])


def a2a_trace(h, oid):  # type: ignore[no-untyped-def]
    rows = [(m["method"], m["direction"], m["outcome"]) for m in h.store.a2a_messages
            if m.get("order_id") == oid]
    for method, direction, outcome in rows:
        if direction in ("response", "error"):
            print(f"      A2A {method:<8} -> {outcome}")


def main() -> None:
    rule("1. HAPPY PATH: Shopee order, reserved then paid then confirmed")
    h, paypal, _uid, private, device = setup_flow(enabled_merchants=ALL)
    print("  Enabled merchants:", ", ".join(h.settings.merchant_codes))

    found = tool(h, device, "search_products", {"query": "noodles tea"})
    print("  search 'noodles tea' ->",
          ", ".join(f"{r['name']} ({r['merchant_id']}, {r['price']})" for r in found["results"]))

    oid, resp = place_order(h, private, device, {"merchant_id": "shopee",
                                                 "items": [{"sku": "SHP-PHONE-CASE", "qty": 2}]})
    a2a_trace(h, oid)
    status = tool(h, device, "get_order_status", {"order_id": oid})
    print(f"  get_order_status -> {status['status']}, fulfillment={status['fulfillment_status']}, "
          f"merchant_reference={status['merchant_reference']}")
    print(f"  PayPal charge attempts: {paypal_charges(paypal)} (expected 1)")

    rule("2. SOLD OUT: reserve fails, so PayPal is NEVER called")
    h, paypal, _uid, private, device = setup_flow(enabled_merchants=ALL)
    oid, resp = place_order(h, private, device, {"merchant_id": "shopee",
                                                 "items": [{"sku": "SHP-PLUSH-LTD", "qty": 1}]})
    a2a_trace(h, oid)
    print(f"  PayPal charge attempts: {paypal_charges(paypal)} (expected 0 - no money moved)")
    print(f"  spending reservation: {h.store.spending_records[0]['status']} (expected released)")

    rule("3. PAYMENT DECLINED: reservation is released")
    h, paypal, _uid, private, device = setup_flow(mode="instrument_declined", enabled_merchants=ALL)
    oid, resp = place_order(h, private, device, {"merchant_id": "shopee",
                                                 "items": [{"sku": "SHP-PHONE-CASE", "qty": 2}]})
    a2a_trace(h, oid)
    # The in-process Shopee agent is created on first use, so read its stock now.
    book = h.services.merchants._apps["shopee"].state.reservations
    print(f"  Shopee stock for SHP-PHONE-CASE back to {book.available('SHP-PHONE-CASE')} "
          f"(hold released; full stock is 50)")

    print("\nDone. Every step above ran against the real application code.\n")


if __name__ == "__main__":
    main()
