"""httpx.MockTransport merchants that return correct or tampered A2A responses."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx

from app.merchant_agents import catalogs
from tests.fakes.clock import FakeClock

Mutator = Callable[[dict[str, Any], dict[str, Any]], Any]


def _iso(clock: FakeClock, seconds: float) -> str:
    return (clock.utc_now() + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def good_result(code: str, request_data: dict[str, Any], clock: FakeClock) -> dict[str, Any]:
    by_sku = {p.sku: p for p in catalogs.products(code)}
    if request_data["type"] == "clawpay.search_request.v1":
        hits = catalogs.search(code, request_data["query"], request_data["max_results"])
        return {
            "type": "clawpay.search_response.v1",
            "correlation_id": request_data["correlation_id"],
            "merchant_id": code,
            "products": [
                {"sku": p.sku, "name": p.name, "unit_price_minor": p.unit_price_minor, "currency": "MYR"}
                for p in hits
            ],
        }
    lines = []
    for item in request_data["items"]:
        p = by_sku[item["sku"]]
        lines.append({"sku": p.sku, "name": p.name, "qty": item["qty"], "unit_price_minor": p.unit_price_minor,
                      "line_total_minor": item["qty"] * p.unit_price_minor})
    total = sum(line["line_total_minor"] for line in lines)
    return {
        "type": "clawpay.quote_response.v1",
        "correlation_id": request_data["correlation_id"],
        "merchant_id": code,
        "merchant_name": "Totally \"Legit\" Shop",
        "quote_id": "q_test_1",
        "currency": request_data["currency"],
        "items": lines,
        "subtotal_minor": total,
        "total_minor": total,
        "expires_at": _iso(clock, 300),
    }


class ScriptedMerchant:
    """Builds a correct response, then lets ``mutate(envelope, data)`` tamper with it.

    ``mutate`` may return an ``httpx.Response`` (or raise) to replace the reply entirely.
    """

    def __init__(self, code: str, clock: FakeClock, mutate: Mutator | None = None) -> None:
        self.code = code
        self.clock = clock
        self.mutate = mutate
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        data = body["params"]["message"]["parts"][0]["data"]
        result = good_result(self.code, data, self.clock)
        envelope = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {"kind": "message", "role": "agent", "messageId": "m1",
                       "parts": [{"kind": "data", "data": result}]},
        }
        if self.mutate is not None:
            replaced = self.mutate(envelope, result)
            if isinstance(replaced, httpx.Response):
                return replaced
        return httpx.Response(200, json=envelope)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
