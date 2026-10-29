"""Test harness: app wired to the in-memory store, fake clock and fake JWKS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import generate_device_token, generate_llm_token, hash_token
from app.main import create_app
from app.services.container import Services, build_services
from tests.fakes.clock import FakeClock
from tests.fakes.keys import JwtSigner, SUPABASE_URL
from tests.fakes.store import InMemoryStore


PEPPER = "test-pepper-0123456789-abcdefghijklmnop"
ADMIN_TOKEN = "test-admin-token-0123456789-abcdefghijkl"
PUBLISHABLE_KEY = "sb_publishable_test_key_value"

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-11-25",
}


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "supabase_url": SUPABASE_URL,
        "supabase_service_role_key": "fake-service-role-key-0123456789",
        "supabase_publishable_key": PUBLISHABLE_KEY,
        "mcp_enabled": True,
        "device_token_pepper": PEPPER,
        "internal_service_token": ADMIN_TOKEN,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@dataclass
class Harness:
    app: FastAPI
    client: TestClient
    store: InMemoryStore
    services: Services
    clock: FakeClock
    signer: JwtSigner
    settings: Settings

    def new_user(self, *, payment_method: bool = True) -> str:
        user_id = self.store.add_user()
        if payment_method:
            self.store.add_payment_method(user_id)
        return user_id

    def new_device(
        self,
        user_id: str | None = None,
        *,
        status: str = "active",
        public_key: str | None = None,
        key_version: int = 1,
    ) -> tuple[str, str, str]:
        """Returns (device_token, device_id, user_id)."""
        user_id = user_id or self.new_user()
        token = generate_device_token()
        llm = generate_llm_token()
        device_id = self.store.add_device(
            user_id,
            token_hash=hash_token(PEPPER, token),
            llm_token_hash=hash_token(PEPPER, llm),
            status=status,
            public_key=public_key,
            key_version=key_version,
        )
        return token, device_id, user_id

    def jwt(self, user_id: str, /, **claims: Any) -> str:
        return self.signer.token(user_id, **claims)

    # -- MCP helpers ---------------------------------------------------------------

    def mcp(self, token: str | None, message: Any, headers: dict[str, str] | None = None,
            path: str = "/mcp") -> httpx.Response:
        h = dict(MCP_HEADERS)
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        h.update(headers or {})
        return self.client.post(path, json=message, headers=h)

    def call_tool(self, token: str, name: str, arguments: Any, rpc_id: int = 2) -> httpx.Response:
        """Replay cap_mcp_client: fresh initialize -> initialized -> tools/call."""
        init = self.mcp(token, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "esp-claw", "version": "x"}}})
        assert init.status_code == 200, init.text
        assert "mcp-session-id" not in init.headers
        note = self.mcp(token, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert note.status_code == 202 and note.content == b""
        return self.mcp(token, {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})


def make_harness(
    *,
    store: InMemoryStore | None = None,
    merchant_transports: dict[str, httpx.AsyncBaseTransport] | None = None,
    paypal_transport: httpx.AsyncBaseTransport | None = None,
    paypal_gateway: Any = None,
    paypal_agent_transport: httpx.AsyncBaseTransport | None = None,
    llm_client: httpx.AsyncClient | None = None,
    raise_server_exceptions: bool = True,
    **settings_overrides: Any,
) -> Harness:
    clock = store.clock if store else FakeClock()
    store = store or InMemoryStore(clock)
    settings = make_settings(**settings_overrides)
    signer = JwtSigner()
    services = build_services(
        settings,
        clock=clock,
        repos=store.repositories(),
        user_reader=store.user_reader_factory(),
        key_resolver=signer.resolver,
        merchant_transports=merchant_transports,
        paypal_gateway=paypal_gateway,
        paypal_transport=paypal_transport,
        paypal_agent_transport=paypal_agent_transport,
        llm_client=llm_client,
    )
    app = create_app(settings, services=services, start_background=False)
    client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
    return Harness(app, client, store, services, clock, signer, settings)


def tool_payload(response: httpx.Response) -> tuple[bool, Any]:
    """Parse a tools/call response -> (isError, parsed text JSON)."""
    import json

    body = response.json()
    result = body["result"]
    assert "structuredContent" not in result
    assert len(result["content"]) == 1 and result["content"][0]["type"] == "text"
    return result["isError"], json.loads(result["content"][0]["text"])
