"""Offline demo backend for the watch client: no Supabase, no PayPal, no paid LLM.

Runs the real app (auth, /api/v1/assistant/messages, MCP tools, mock merchants)
on the in-memory test store, with a keyword "LLM" standing in for the upstream
model so the whole tool loop can be exercised from the watch simulator:

    uv run python -m scripts.assistant_demo_server            # port 8000
    uv run python -m scripts.assistant_demo_server --real-llm # use LLM_PROXY_* from the environment

FISH_AUDIO_* from ``.env`` or the environment enables real Fish Audio speech
(paid API); without a key /speech returns 503 and watch replies remain text-only.

It prints a ready-to-use ``cpd_`` device token. Nothing persists after exit.
Development only: it imports the test fakes and must never be deployed.
"""

from __future__ import annotations

import argparse
import json
import os
import re

import httpx
import uvicorn
from dotenv import dotenv_values

from app.core.clock import SystemClock
from app.core.security import generate_device_token, generate_llm_token, hash_token
from app.main import create_app
from app.services.container import build_services
from tests.fakes.app import ADMIN_TOKEN, PEPPER, make_settings
from tests.fakes.keys import JwtSigner
from tests.fakes.paypal import FakePayPalTransport
from tests.fakes.store import InMemoryStore


def _tool_call(name: str, arguments: dict, n: int) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call_{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def _say(text: str) -> dict:
    return {"role": "assistant", "content": text}


def keyword_llm(body: dict) -> dict:
    """Pick the next step from the latest user message and tool results (enough for a demo)."""
    messages = body["messages"]
    last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
    text = messages[last_user]["content"].lower()
    results = [json.loads(m["content"]) for m in messages[last_user:] if m["role"] == "tool"]
    step = len(results)
    if step and "error" in results[-1]:
        return _say(results[-1]["message"])

    if re.search(r"\b(spent|spend|spending|budget|limit)\b", text):
        if not step:
            return _tool_call("get_spending_summary", {}, 1)
        return _say(f"You have {results[-1].get('remaining', 'some')} left today.")

    wants_order = re.search(r"\b(buy|order|get me|purchase)\b", text) is not None
    query = re.sub(r"\b(please|buy|order|get me|purchase|find|search|for|some|a|an|the|me|two|one|three|\d+)\b", " ", text)
    query = " ".join(query.split()) or "milk"
    digits = re.search(r"\b\d+\b", text)
    words = text.split()
    qty = int(digits.group()) if digits else 2 if "two" in words else 3 if "three" in words else 1

    if step == 0:
        return _tool_call("search_products", {"query": query, "max_results": 3}, 1)
    found = results[0].get("results", [])
    if not found:
        return _say(f"I couldn't find {query}.")
    top = found[0]
    if not wants_order:
        options = ", ".join(f"{p['name']} for {p['price']}" for p in found[:2])
        return _say(f"I found {options}.")
    if step == 1:
        return _tool_call("create_order", {"merchant_id": top["merchant_id"],
                                           "items": [{"sku": top["sku"], "qty": min(qty, 20)}]}, 2)
    order = results[1]
    how = ("Tap Approve on your watch to pay." if "Approve button on their watch" in messages[0]["content"]
           else "Hold the button on your ClawPay to pay.")
    return _say(f"Order ready: {order['total']} at {order['merchant']}. {how}")


def _fake_upstream(request: httpx.Request) -> httpx.Response:
    message = keyword_llm(json.loads(request.content))
    return httpx.Response(200, json={"choices": [{"message": message}], "usage": {"total_tokens": 1}})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--real-llm", action="store_true")
    args = parser.parse_args()

    if args.real_llm:
        llm_settings = {k: os.environ[k.upper()] for k in (
            "llm_proxy_upstream_base_url", "llm_proxy_upstream_api_key", "llm_proxy_model")}
        llm_client = None
    else:
        llm_settings = {"llm_proxy_upstream_base_url": "https://keyword-llm.local/v1",
                        "llm_proxy_upstream_api_key": "offline", "llm_proxy_model": "keyword-demo"}
        llm_client = httpx.AsyncClient(transport=httpx.MockTransport(_fake_upstream))

    # Only the Fish Audio settings are taken from .env; nothing else (Supabase, real PayPal) is used.
    env = {**dotenv_values(".env"), **os.environ}
    fish = {k.lower(): v for k, v in env.items() if k.startswith("FISH_AUDIO_") and v}

    clock = SystemClock()
    store = InMemoryStore(clock)  # type: ignore[arg-type]
    # Fake PayPal Sandbox, so a signed approval (claw button or watch) completes as "paid".
    settings = make_settings(app_env="development", paypal_client_id="demo", paypal_client_secret="demo-secret",
                             **llm_settings, **fish)
    services = build_services(settings, clock=clock, repos=store.repositories(),
                              user_reader=store.user_reader_factory(), key_resolver=JwtSigner().resolver,
                              llm_client=llm_client, paypal_transport=FakePayPalTransport())
    app = create_app(settings, services=services, start_background=False)

    user_id = store.add_user()
    store.add_payment_method(user_id)
    token = generate_device_token()
    device_id = store.add_device(user_id, token_hash=hash_token(PEPPER, token),
                                 llm_token_hash=hash_token(PEPPER, generate_llm_token()))
    print(f"\nClawPay assistant demo on http://{args.host}:{args.port}  (LLM: "
          f"{'real upstream' if args.real_llm else 'offline keyword stand-in'}; speech: "
          f"{'Fish Audio' if settings.fish_audio_api_key else 'unavailable (text replies only)'})")
    print(f"device_id    {device_id}\ndevice token {token}")
    print("Register a watch as its own approver (after launching the watch app once):\n"
          f"  uv run python -m scripts.register_watch --base-url http://{args.host}:{args.port} "
          f"--user-id {user_id} --admin-token {ADMIN_TOKEN} --simulator <watch-udid>\n", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
