"""Server-side agent loop for clients that cannot run one (watch, voice shortcut).

The ESP-Claw device runs its own LLM loop against ``/llm/v1`` and ``/mcp``. A
watch only has a microphone, so this loop runs here instead: it calls the same
pinned upstream model through ``LLMProxyService`` (same rate limit and daily
token budget) and executes tools through ``McpServer``, so validation, the
merchant allowlist, tool rate limits and conversation events are unchanged.

It can still only search, create an order and read status/spending. Payment
needs the device's P-256 signature (button hold on the claw); the claw's
approval task picks the order up from ``GET /devices/{id}/pending-approvals``, or
a watch registered as its own device signs it (``approver="watch"``).
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from starlette.background import BackgroundTasks

from app.core.exceptions import ApiError
from app.domain.money import EXPONENTS, format_display
from app.integrations.mcp import tools as mcp_tools
from app.integrations.mcp.server import McpServer
from app.services.container import Services
from app.services.context import DeviceContext


logger = logging.getLogger(__name__)

MAX_STEPS = 5  # LLM round trips per message
MAX_REPLY_TOKENS = 400
MAX_HISTORY_MESSAGES = 24
HISTORY_TTL_SECONDS = 15 * 60
MAX_REMEMBERED_DEVICES = 256
MAX_ARGUMENT_CHARS = 8 * 1024
FALLBACK_REPLY = "Sorry, I couldn't finish that. Please try again."

SYSTEM_PROMPT = (
    "You are ClawPay, a shopping assistant the user talks to through a smartwatch. "
    "Every reply is read aloud on a tiny watch: aim for 8-16 words, never more than 24 words. "
    "Use one short sentence, or two only when needed. Plain text, no markdown, lists, greetings or filler. "
    "Answer only the current question. For spending, say the amount spent and remaining; omit the cap unless asked. "
    "Use search_products to find items; mention at most two short product names with prices. Ask before ordering if the "
    "item or quantity is unclear. create_order does not pay: after it succeeds, tell the user "
    "the total and {approve_how} The approval happens outside this chat, so ignore any "
    "next_step that mentions clawpay_request_approval. "
    "Only say a payment succeeded if get_order_status returns status paid. "
    "These are PayPal Sandbox demo orders with simulated delivery, not real retailer purchases. "
    "When the user says yes, create the cart only if one specific product was selected or offered. "
    "If more than one product is still a possible choice, ask which one. Use its exact returned SKU. "
    "Person-to-person transfers and retailer fulfilment are not available; never claim to perform them. "
    "If a tool returns an error, tell the user its message."
)
APPROVE_HOW = {
    "device": "to approve it by holding the button on their ClawPay device.",
    "watch": "to review and confirm it on their watch.",
}


def openai_tools(merchant_codes: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["inputSchema"],
            },
        }
        for t in mcp_tools.tools_for(merchant_codes)
    ]


@dataclass
class _History:
    messages: list[dict[str, Any]] = field(default_factory=list)
    touched: float = 0.0


@dataclass
class _ToolRun:
    call: dict[str, Any]  # sanitized tool call, echoed back to the model
    text: str  # tool result for the model
    payload: dict[str, Any] | None  # parsed result when the tool succeeded


class ConversationMemory:
    """Per-device chat history, in memory (single worker, like the other limits)."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, _History] = OrderedDict()

    def load(self, device_id: str, now: float) -> list[dict[str, Any]]:
        item = self._items.get(device_id)
        if item is None or now - item.touched > HISTORY_TTL_SECONDS:
            self._items.pop(device_id, None)
            return []
        return list(item.messages)

    def save(self, device_id: str, messages: list[dict[str, Any]], now: float) -> None:
        kept = messages[-MAX_HISTORY_MESSAGES:]
        # Never start on an assistant tool call or tool result cut from its user turn.
        while kept and kept[0].get("role") != "user":
            kept.pop(0)
        self._items[device_id] = _History(kept, now)
        self._items.move_to_end(device_id)
        while len(self._items) > MAX_REMEMBERED_DEVICES:
            self._items.popitem(last=False)

    def clear(self, device_id: str) -> None:
        self._items.pop(device_id, None)


class Assistant:
    def __init__(self, services: Services, memory: ConversationMemory) -> None:
        self._services = services
        self._memory = memory

    async def handle(
        self,
        device: DeviceContext,
        text: str,
        background: BackgroundTasks,
        *,
        reset: bool = False,
        approver: str = "device",
        progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        services = self._services
        if reset:
            self._memory.clear(device.device_id)
        history = self._memory.load(device.device_id, services.clock.monotonic())
        turn: list[dict[str, Any]] = [{"role": "user", "content": text}]
        tools = openai_tools(services.settings.merchant_codes)
        order: dict[str, Any] | None = None
        reply: str | None = None
        products: list[dict[str, Any]] = []
        rpc_id = 0
        system = SYSTEM_PROMPT.format(approve_how=APPROVE_HOW[approver])

        async def notify(stage: str, message: str, status: str = "working") -> None:
            if progress:
                await progress({"type": "progress", "stage": stage, "message": message, "status": status})

        for _ in range(MAX_STEPS):
            await notify("assistant", "Understanding your request…" if rpc_id == 0 else "Preparing your answer…")
            prompt = [{"role": "system", "content": system}, *history, *turn]
            try:
                message = await self._complete(device.device_id, prompt, tools)
            except ApiError:
                if len(turn) == 1:
                    raise
                # A tool already ran (maybe create_order): report what happened instead of
                # failing, so a retry does not create a second order.
                logger.warning("Assistant stopped after a tool call: upstream failed")
                break
            calls = message.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                content = message.get("content")
                reply = content.strip() if isinstance(content, str) and content.strip() else None
                break
            results = []
            for i, call in enumerate(calls):
                name = call.get("function", {}).get("name") if isinstance(call, dict) and isinstance(call.get("function"), dict) else "unknown"
                label = {
                    "search_products": "Searching merchant catalogs…",
                    "create_order": "Checking the live quote and spending limits…",
                    "get_order_status": "Checking the payment record…",
                    "get_spending_summary": "Checking today's spending…",
                }.get(name, "Checking the requested action…")
                if name == "search_products":
                    try:
                        args = json.loads(call["function"].get("arguments", "{}"))
                        if isinstance(args, dict) and args.get("merchant_id") == "channel3":
                            label = "Searching Channel3…"
                    except (ValueError, TypeError, RecursionError):
                        pass
                await notify(name, label)
                result = await self._run_tool(call, rpc_id + i + 1, device, background)
                results.append(result)
                if result.payload is None:
                    await notify(name, "The action could not be completed.", "failed")
                elif name == "search_products":
                    products = result.payload.get("results", [])[:5]
                    await notify(name, f"Found {len(products)} products with current prices.", "complete")
                elif name == "create_order":
                    await notify(name, "Quote validated. Cart is ready for your approval.", "complete")
                else:
                    await notify(name, "Verified the latest account record.", "complete")
            rpc_id += len(calls)
            # Rebuilt from what was executed, so every tool reply has a matching, well-formed call.
            turn.append({"role": "assistant", "content": message.get("content"),
                         "tool_calls": [r.call for r in results]})
            for r in results:
                turn.append({"role": "tool", "tool_call_id": r.call["id"], "content": r.text})
                if r.call["function"]["name"] in ("create_order", "get_order_status") and r.payload and isinstance(r.payload.get("order_id"), str):
                    order = {k: r.payload.get(k) for k in ("order_id", "status", "total", "merchant", "expires_at", "items", "currency", "paid_at")}

            # The validated quote already gives us the next action. Avoid another LLM
            # round trip just to restate the amount, and never let prose authorize payment.
            if (len(results) == 1 and results[0].call["function"]["name"] == "create_order"
                    and order and order.get("status") == "awaiting_device_approval"
                    and isinstance(order.get("total"), str)):
                action = "Review and confirm on your watch." if approver == "watch" else "Hold your device button to approve."
                reply = f"{order['total']}. {action}"
                break
            if len(results) == 1 and results[0].payload:
                name, payload = results[0].call["function"]["name"], results[0].payload
                if name == "get_spending_summary":
                    spent, currency = payload.get("committed_minor"), payload.get("currency")
                    if type(spent) is int and spent >= 0 and isinstance(currency, str) and currency in EXPONENTS and isinstance(payload.get("remaining"), str):
                        reply = f"Spent {format_display(spent, currency)} today. {payload['remaining']} available."
                        break
                if name == "get_order_status" and order and order.get("status") == "paid" and isinstance(order.get("total"), str):
                    reply = f"Paid {order['total']}."
                    break

        reply = reply or FALLBACK_REPLY
        turn.append({"role": "assistant", "content": reply})
        self._memory.save(device.device_id, [*history, *turn], services.clock.monotonic())
        return {
            "reply": reply,
            "order": order,
            "approval_required": bool(order and order.get("status") == "awaiting_device_approval"),
            "products": products,
        }

    async def _complete(
        self, device_id: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """One non-streaming upstream call through the proxy service (limits and budget apply)."""
        body = {"messages": messages, "tools": tools, "tool_choice": "auto", "max_completion_tokens": MAX_REPLY_TOKENS}
        try:
            response = await self._services.llm.complete(device_id, body)
        except ApiError as exc:
            if exc.status_code != 400:
                raise
            # The request is built here, not by the watch; a 400 means our transcript was
            # rejected. Drop the history so the next message starts clean.
            self._memory.clear(device_id)
            raise ApiError(502, "upstream_error", "LLM upstream rejected the request.") from None
        except RecursionError:
            raise ApiError(502, "upstream_error", "Invalid upstream response.") from None
        try:
            message = json.loads(response.body)["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError, RecursionError):
            raise ApiError(502, "upstream_error", "Invalid upstream response.") from None
        if not isinstance(message, dict):
            raise ApiError(502, "upstream_error", "Invalid upstream response.")
        return message

    async def _run_tool(
        self, call: Any, rpc_id: int, device: DeviceContext, background: BackgroundTasks
    ) -> _ToolRun:
        call = call if isinstance(call, dict) else {}
        call_id = call.get("id") if isinstance(call.get("id"), str) and call.get("id") else f"call_{rpc_id}"
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = function.get("name") if isinstance(function.get("name"), str) and function.get("name") else "unknown"
        raw_args = function.get("arguments") or "{}"
        if not isinstance(raw_args, str):
            raw_args = "{}"
        clean = {"id": call_id, "type": "function", "function": {"name": name[:64], "arguments": raw_args[:MAX_ARGUMENT_CHARS]}}

        arguments: Any = None
        if len(raw_args) <= MAX_ARGUMENT_CHARS:
            try:
                arguments = json.loads(raw_args)
            except (ValueError, RecursionError):
                pass
        if not isinstance(arguments, dict):
            clean["function"]["arguments"] = "{}"
            error = mcp_tools.error_result("invalid_arguments", "arguments must be a JSON object")
            return _ToolRun(clean, error["content"][0]["text"], None)

        rpc = {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        _, body = await McpServer(self._services).handle(rpc, device, background)
        envelope = json.loads(body or b"{}")
        if "error" in envelope:  # unknown tool / bad params at the JSON-RPC level
            message = envelope["error"].get("message", "Invalid tool call")
            return _ToolRun(clean, mcp_tools.compact({"error": "invalid_arguments", "message": message}), None)
        result = envelope["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return _ToolRun(clean, text, None)
        logger.info("Assistant tool %s ok", name)
        return _ToolRun(clean, text, json.loads(text))
