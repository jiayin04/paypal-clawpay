"""MCP tool registry: names, descriptions and input schemas exactly as contracts.md §4.

There is deliberately no approve, pay, capture, refund or transfer tool: the
LLM can only search, build an order, and read status/spending. Approval needs
an ECDSA signature from the device's private C task.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from app.integrations.mcp import jsonrpc
from app.schemas.mcp import (
    CreateOrderArgs,
    GetOrderStatusArgs,
    GetSpendingSummaryArgs,
    SearchProductsArgs,
)


MAX_RESPONSE_BYTES = 3900  # device MCP client buffer is 4096 bytes

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_products",
        "description": (
            "Search merchant catalogs. Show the user names and prices. "
            "Use sku and merchant_id with create_order."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 100},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                "merchant_id": {"type": "string", "enum": ["grocer", "electronics"]},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_order",
        "description": (
            "Create an order from one merchant. This does not pay. Then call "
            "clawpay_request_approval with the order_id and tell the user to check "
            "the device screen and hold the button."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "merchant_id": {"type": "string", "enum": ["grocer", "electronics"]},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "sku": {"type": "string", "maxLength": 64},
                            "qty": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                        "required": ["sku", "qty"],
                        "additionalProperties": False,
                    },
                },
                "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
            },
            "required": ["merchant_id", "items"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_order_status",
        "description": (
            "Get the authoritative status of an order. Only tell the user a payment "
            "succeeded if status is paid."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"order_id": {"type": "string", "format": "uuid"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_spending_summary",
        "description": "Get today's spending against the daily limit.",
        "inputSchema": {
            "type": "object",
            "properties": {"period": {"type": "string", "enum": ["today"], "default": "today"}},
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOLS)

ARG_MODELS: dict[str, type[BaseModel]] = {
    "search_products": SearchProductsArgs,
    "create_order": CreateOrderArgs,
    "get_order_status": GetOrderStatusArgs,
    "get_spending_summary": GetSpendingSummaryArgs,
}

# Tool error code -> what the LLM should tell the user. One table so it is tested.
ERROR_MESSAGES: dict[str, str] = {
    "invalid_arguments": "The request was not valid.",
    "rate_limited": "Too many requests. Wait a minute and try again.",
    "merchant_unavailable": "The shops are not responding. Try again shortly.",
    "unknown_sku": "The shop does not sell that item. Search again for the right item.",
    "quote_invalid": "The shop returned an invalid quote. Try again shortly.",
    "currency_not_supported": "That currency is not supported.",
    "no_payment_method": "Link a PayPal account in the ClawPay setup page first.",
    "daily_limit_exceeded": "This order would go over today's spending limit.",
    "phone_approval_unavailable": (
        "Orders above RM100 need phone approval, which is not available yet."
    ),
    "not_found": "Order not found.",
    "internal_error": "Something went wrong. Try again later.",
}

STRIPPED_IDENTITY_KEYS = ("device_id", "user_id")


def compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def tool_result(obj: Any, *, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": compact(obj)}], "isError": is_error}


def error_result(code: str, message: str | None = None) -> dict[str, Any]:
    return tool_result(
        {"error": code, "message": message or ERROR_MESSAGES.get(code, ERROR_MESSAGES["internal_error"])},
        is_error=True,
    )


def validation_message(exc: ValidationError) -> str:
    err = exc.errors()[0]
    field = ".".join(str(p) for p in err.get("loc", ())) or "arguments"
    return f"{field}: {err.get('msg', 'invalid')}"[:200]


def fit_search_response(rpc_id: jsonrpc.RpcId, payload: dict[str, Any]) -> bytes:
    """Serialize; drop trailing results until the whole response fits the cap."""
    results = list(payload.get("results", []))
    body = dict(payload)
    while True:
        body["results"] = results
        encoded = jsonrpc.dumps(jsonrpc.result(rpc_id, tool_result(body, is_error=False)))
        if len(encoded) <= MAX_RESPONSE_BYTES or not results:
            return encoded
        results = results[:-1]
        body["truncated"] = True
