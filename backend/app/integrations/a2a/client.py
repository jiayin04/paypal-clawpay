"""Minimal A2A JSON-RPC client: ``message/send`` with one data part.

The response is untrusted. This module only enforces transport rules (status,
content type, streamed 64 KB cap, timeout, JSON-RPC envelope); the merchant
service validates the business content.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import anyio
import httpx

from app.core.observability import outbound_headers


MAX_RESPONSE_BYTES = 64 * 1024

Failure = Literal["timeout", "unavailable", "rejected"]


class A2AError(Exception):
    def __init__(self, outcome: Failure, reason: str) -> None:
        super().__init__(f"{outcome}: {reason}")
        self.outcome = outcome
        self.reason = reason


def build_envelope(correlation_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": correlation_id,
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "data", "data": data}],
            }
        },
    }


async def _post(
    client: httpx.AsyncClient, url: str, envelope: dict[str, Any], headers: dict[str, str] | None = None
) -> bytes:
    async with client.stream(
        "POST",
        url,
        json=envelope,
        headers={"Accept": "application/json", **outbound_headers(), **(headers or {})},
    ) as response:
        if response.status_code != 200:
            raise A2AError("unavailable", f"http_{response.status_code}")
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise A2AError("rejected", "content_type")
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise A2AError("rejected", "too_large")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise A2AError("rejected", "too_large")
            chunks.append(chunk)
        return b"".join(chunks)


async def send_message(
    client: httpx.AsyncClient,
    url: str,
    correlation_id: str,
    data: dict[str, Any],
    timeout: float,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send one request and return the first ``data`` part of the agent message."""
    envelope = build_envelope(correlation_id, data)
    try:
        with anyio.fail_after(timeout):
            raw = await _post(client, url, envelope, headers)
    except TimeoutError:
        raise A2AError("timeout", "timeout") from None
    except httpx.TimeoutException:
        raise A2AError("timeout", "timeout") from None
    except httpx.HTTPError:
        raise A2AError("unavailable", "connect") from None
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise A2AError("rejected", "invalid_json") from None
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        raise A2AError("rejected", "jsonrpc")
    if body.get("id") != correlation_id:
        raise A2AError("rejected", "rpc_id_mismatch")
    if "error" in body:
        raise A2AError("unavailable", "rpc_error")
    result = body.get("result")
    if not isinstance(result, dict) or result.get("kind") != "message":
        raise A2AError("rejected", "no_result")
    parts = result.get("parts")
    if not isinstance(parts, list):
        raise A2AError("rejected", "no_data_part")
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "data" and isinstance(part.get("data"), dict):
            return part["data"]
    raise A2AError("rejected", "no_data_part")
