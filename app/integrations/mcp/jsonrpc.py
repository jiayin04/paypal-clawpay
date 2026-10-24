"""JSON-RPC 2.0 shape rules and response builders for the MCP endpoint."""

from __future__ import annotations

import json
from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

RpcId = str | int | None


def dumps(obj: Any) -> bytes:
    """Compact UTF-8 JSON (the device's MCP client has a small buffer)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def error(rpc_id: RpcId, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def result(rpc_id: RpcId, value: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": value}


def valid_id(value: Any) -> bool:
    return (isinstance(value, str) or isinstance(value, int)) and not isinstance(value, bool)


def check_shape(message: Any) -> dict[str, Any] | None:
    """Return a JSON-RPC error response if ``message`` is not a valid single request."""
    if not isinstance(message, dict):
        return error(None, INVALID_REQUEST, "Invalid Request")
    rpc_id = message.get("id") if valid_id(message.get("id")) else None
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return error(rpc_id, INVALID_REQUEST, "Invalid Request")
    if "id" in message and not valid_id(message["id"]):
        return error(None, INVALID_REQUEST, "Invalid Request")
    return None
