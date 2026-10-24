"""Stateless MCP JSON-RPC dispatch (design §2.1). No session state is kept:
every tool call arrives as a fresh initialize -> notifications/initialized -> tools/call.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from starlette.background import BackgroundTasks

from app.domain.money import format_display
from app.integrations.mcp import jsonrpc
from app.integrations.mcp import tools as mcp_tools
from app.services.container import Services
from app.services.context import DeviceContext
from app.services.conversation_service import call_summary, response_summary
from app.services.errors import ToolError
from app.services.merchant_service import A2ALog, MerchantFailure

logger = logging.getLogger(__name__)

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
DEFAULT_VERSION = "2025-11-25"
SERVER_INFO = {"name": "clawpay", "version": "0.1.0"}


class McpServer:
    def __init__(self, services: Services) -> None:
        self._services = services

    async def handle(
        self, message: dict[str, Any], device: DeviceContext, background: BackgroundTasks
    ) -> tuple[int, bytes | None]:
        """Return (HTTP status, body). 202 with no body for notifications."""
        method: str = message["method"]
        if method.startswith("notifications/"):
            return 202, None
        if "id" not in message:
            # Nothing is executed: no tool, no rate-limit slot, no conversation event.
            return 200, jsonrpc.dumps(jsonrpc.error(None, jsonrpc.INVALID_REQUEST, "id required"))
        rpc_id = message["id"]
        params = message.get("params")

        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if requested in SUPPORTED_VERSIONS else DEFAULT_VERSION
            return 200, jsonrpc.dumps(
                jsonrpc.result(
                    rpc_id,
                    {
                        "protocolVersion": version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": SERVER_INFO,
                    },
                )
            )
        if method == "ping":
            return 200, jsonrpc.dumps(jsonrpc.result(rpc_id, {}))
        if method == "tools/list":
            return 200, jsonrpc.dumps(jsonrpc.result(rpc_id, {"tools": mcp_tools.TOOLS}))
        if method == "tools/call":
            return 200, await self._tools_call(rpc_id, params, device, background)
        return 200, jsonrpc.dumps(jsonrpc.error(rpc_id, jsonrpc.METHOD_NOT_FOUND, "Method not found"))

    async def _tools_call(
        self,
        rpc_id: jsonrpc.RpcId,
        params: Any,
        device: DeviceContext,
        background: BackgroundTasks,
    ) -> bytes:
        def rpc_error(message: str) -> bytes:
            return jsonrpc.dumps(jsonrpc.error(rpc_id, jsonrpc.INVALID_PARAMS, message))

        def envelope(result: dict[str, Any]) -> bytes:
            return jsonrpc.dumps(jsonrpc.result(rpc_id, result))

        if not isinstance(params, dict):
            return rpc_error("Invalid params")
        name = params.get("name")
        if not isinstance(name, str) or name not in mcp_tools.TOOL_NAMES:
            return rpc_error("Unknown tool")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return rpc_error("arguments must be an object")

        services = self._services
        retry = await services.limiters.mcp_tool_calls.acquire(device.device_id)
        if retry is not None:
            return envelope(mcp_tools.error_result("rate_limited"))

        arguments = dict(arguments)
        for key in mcp_tools.STRIPPED_IDENTITY_KEYS:
            if arguments.pop(key, None) is not None:
                logger.debug("Ignored %s in %s arguments", key, name)

        started = services.clock.monotonic()
        try:
            args = mcp_tools.ARG_MODELS[name].model_validate(arguments)
        except ValidationError as exc:
            message = mcp_tools.validation_message(exc)
            self._record(background, device, name, arguments, None, "invalid_arguments", started)
            return envelope(mcp_tools.error_result("invalid_arguments", message))

        call_args = args.model_dump(exclude_none=True)
        try:
            if name == "search_products":
                payload = await self._search(args, device, background)
                self._record(background, device, name, call_args, payload, None, started)
                return mcp_tools.fit_search_response(rpc_id, payload)
            if name == "create_order":
                payload = await services.orders.create_order(
                    device,
                    merchant_id=args.merchant_id,
                    items=args.items,
                    currency=args.currency,
                    background=background,
                )
            elif name == "get_order_status":
                payload = await services.orders.get_order_status(device, args.order_id)
            else:
                payload = await services.spending.tool_summary(device.user_id)
        except ToolError as exc:
            self._record(background, device, name, call_args, None, exc.code, started)
            return envelope(mcp_tools.error_result(exc.code, exc.message))
        except Exception:
            logger.exception("Tool %s failed", name)
            self._record(background, device, name, call_args, None, "internal_error", started)
            return envelope(mcp_tools.error_result("internal_error"))
        self._record(background, device, name, call_args, payload, None, started)
        return envelope(mcp_tools.tool_result(payload, is_error=False))

    async def _search(
        self, args: Any, device: DeviceContext, background: BackgroundTasks
    ) -> dict[str, Any]:
        services = self._services
        log = A2ALog(user_id=device.user_id)
        try:
            products = await services.merchants.search(
                args.query, args.max_results, args.merchant_id, log
            )
        except MerchantFailure:
            raise ToolError("merchant_unavailable") from None
        finally:
            # Best-effort, after the response; search rows never carry an order id.
            background.add_task(services.merchants.write_log, log, None)
        if not products:
            return {"results": [], "message": "No matching products."}
        return {
            "results": [
                {
                    "sku": p.sku,
                    "name": p.name,
                    "merchant_id": p.merchant_id,
                    "price_minor": p.unit_price_minor,
                    "price": format_display(p.unit_price_minor, p.currency),
                    "currency": p.currency,
                }
                for p in products
            ]
        }

    def _record(
        self,
        background: BackgroundTasks,
        device: DeviceContext,
        tool: str,
        args: dict[str, Any],
        payload: dict[str, Any] | None,
        error_code: str | None,
        started: float,
    ) -> None:
        latency = int((self._services.clock.monotonic() - started) * 1000)
        order_id = payload.get("order_id") if payload else None
        background.add_task(
            self._services.conversations.record_tool_call,
            device,
            tool,
            call_summary(tool, args),
            response_summary(
                tool,
                is_error=error_code is not None,
                error_code=error_code,
                result=payload,
                latency_ms=latency,
            ),
            order_id if isinstance(order_id, str) else None,
        )
