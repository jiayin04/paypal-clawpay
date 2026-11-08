"""PayPal agent: a separate A2A service that owns the PayPal Sandbox credentials.

ClawPay decides *whether* a payment may happen (signature, policy, spending
reservation, idempotency key). This agent only performs the PayPal call it is
told to, with the request id it is given, and reports PayPal's answer. It never
retries with a new request id, never decides policy and is never reachable by
the device LLM (it is not mounted on the main app and requires a bearer token).

Run it standalone (bind to 127.0.0.1 or a private network):

    uv run uvicorn --factory app.paypal_agent.app:paypal_agent_app --host 127.0.0.1 --port 8201 --no-access-log

with PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET and PAYPAL_AGENT_TOKEN in its
environment, then set on ClawPay: PAYPAL_AGENT_ENDPOINT=http://127.0.0.1:8201/a2a
and the same PAYPAL_AGENT_TOKEN.
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.observability import RequestContextMiddleware, bind
from app.core.security import constant_time_equals
from app.integrations.paypal import (
    PayPalApiError,
    PayPalAuthError,
    PayPalClient,
    PayPalGateway,
    PayPalService,
    PayPalUnavailable,
)
from app.schemas import paypal_agent as contract


logger = logging.getLogger(__name__)

AGENT_VERSION = "0.1.0"
MAX_BODY_BYTES = 512 * 1024


def _rpc_error(rpc_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})


def _rpc_result(rpc_id: Any, data: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "kind": "message",
                "role": "agent",
                "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "data", "data": data}],
            },
        }
    )


def _error(kind: str, exc: BaseException | None = None) -> dict[str, Any]:
    """Error payload. Only PayPal's own status/name/issue/debug_id are passed on,
    never exception messages (they can contain provider text)."""
    out: dict[str, Any] = {"kind": kind}
    if isinstance(exc, PayPalApiError):
        out.update(
            status=exc.status,
            name=str(exc.name)[:127] if exc.name else None,
            issue=str(exc.issue)[:127] if exc.issue else None,
            debug_id=str(exc.debug_id)[:127] if exc.debug_id else None,
        )
    return out


def _handlers(gateway: PayPalGateway) -> dict[str, Callable[[Any], Awaitable[dict[str, Any]]]]:
    async def ensure_access_token(_: contract.NoParams) -> dict[str, Any]:
        await gateway.ensure_access_token()
        return {}  # the access token itself never leaves the agent

    async def create_setup_token(p: contract.SetupTokenParams) -> dict[str, Any]:
        token = await gateway.create_setup_token(return_url=p.return_url, cancel_url=p.cancel_url,
                                                 request_id=p.request_id)
        return contract.setup_token_to_dict(token)

    async def get_setup_token(p: contract.SetupTokenIdParams) -> dict[str, Any]:
        return contract.setup_token_to_dict(await gateway.get_setup_token(p.setup_token_id))

    async def create_payment_token(p: contract.PaymentTokenParams) -> dict[str, Any]:
        token = await gateway.create_payment_token(setup_token_id=p.setup_token_id, request_id=p.request_id)
        return contract.payment_token_to_dict(token)

    async def delete_payment_token(p: contract.PaymentTokenIdParams) -> dict[str, Any]:
        await gateway.delete_payment_token(p.payment_token_id)
        return {}

    async def create_order_with_vault(p: contract.CreateOrderParams) -> dict[str, Any]:
        order = await gateway.create_order_with_vault(
            request_id=p.request_id, order_ref=p.order_ref, invoice_id=p.invoice_id,
            description=p.description, amount_minor=p.amount_minor, currency=p.currency, vault_id=p.vault_id,
        )
        return contract.provider_order_to_dict(order)

    async def capture_order(p: contract.CaptureParams) -> dict[str, Any]:
        order = await gateway.capture_order(provider_order_id=p.provider_order_id, request_id=p.request_id)
        return contract.provider_order_to_dict(order)

    async def get_order(p: contract.OrderIdParams) -> dict[str, Any]:
        return contract.provider_order_to_dict(await gateway.get_order(p.provider_order_id))

    async def verify_webhook(p: contract.VerifyWebhookParams) -> dict[str, Any]:
        try:
            raw = base64.b64decode(p.raw_body_b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("raw_body_b64 is not valid base64") from None
        verified = await gateway.verify_webhook(headers=p.headers, raw_body=raw, webhook_id=p.webhook_id)
        return {"verified": bool(verified)}

    return {
        "ensure_access_token": ensure_access_token,
        "create_setup_token": create_setup_token,
        "get_setup_token": get_setup_token,
        "create_payment_token": create_payment_token,
        "delete_payment_token": delete_payment_token,
        "create_order_with_vault": create_order_with_vault,
        "capture_order": capture_order,
        "get_order": get_order,
        "verify_webhook": verify_webhook,
    }


def create_paypal_agent_app(
    settings: Settings | None = None,
    *,
    gateway: PayPalGateway | None = None,
    paypal_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    token = settings.paypal_agent_token
    if token is None:
        raise RuntimeError("PAYPAL_AGENT_TOKEN must be set for the PayPal agent")
    if gateway is None:
        if not (settings.paypal_client_id and settings.paypal_client_secret):
            raise RuntimeError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set for the PayPal agent")
        gateway = PayPalService(PayPalClient(settings, transport=paypal_transport))
    expected = token.get_secret_value()
    handlers = _handlers(gateway)

    app = FastAPI(title="ClawPay PayPal agent (A2A)", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestContextMiddleware, record_metrics=False)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "clawpay-paypal-agent", "version": AGENT_VERSION}

    @app.get("/.well-known/agent-card.json")
    async def agent_card(request: Request) -> dict[str, Any]:
        return {
            "name": "ClawPay PayPal agent",
            "description": "Executes pre-approved PayPal Sandbox vault payments for ClawPay. Not a public agent.",
            "url": str(request.base_url).rstrip("/") + "/a2a",
            "version": AGENT_VERSION,
            "capabilities": {},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "skills": [{"id": op, "name": op, "description": f"PayPal {op}"} for op in contract.PARAMS],
        }

    @app.post("/a2a")
    async def a2a(request: Request) -> JSONResponse:
        header = request.headers.get("authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not constant_time_equals(value.strip(), expected):
            logger.warning("PayPal agent rejected an unauthenticated request")
            return JSONResponse({"error": {"code": "unauthorized", "message": "Authentication required."}},
                                status_code=401, headers={"WWW-Authenticate": "Bearer"})
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse({"error": {"code": "payload_too_large", "message": "Too large."}}, status_code=413)
        try:
            body = await request.json()
        except Exception:
            return _rpc_error(None, -32700, "Parse error")
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid Request")
        rpc_id = body.get("id")
        if body.get("method") != "message/send":
            return _rpc_error(rpc_id, -32601, "Method not found")
        try:
            parts = body["params"]["message"]["parts"]
            data = next(p["data"] for p in parts if p.get("kind") == "data")
        except (KeyError, TypeError, StopIteration, AttributeError):
            return _rpc_error(rpc_id, -32602, "Invalid params")

        correlation_id = data.get("correlation_id") if isinstance(data, dict) else None
        operation = data.get("operation") if isinstance(data, dict) else None

        def respond(ok: bool, *, result: dict[str, Any] | None = None,
                    error: dict[str, Any] | None = None) -> JSONResponse:
            out: dict[str, Any] = {"type": contract.RESPONSE_TYPE, "correlation_id": correlation_id,
                                   "operation": operation, "ok": ok}
            if ok:
                out["result"] = result or {}
            else:
                out["error"] = error
            return _rpc_result(rpc_id, out)

        try:
            req = contract.AgentRequest.model_validate(data)
            params: BaseModel = contract.PARAMS[req.operation].model_validate(req.params)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()})
            logger.warning("PayPal agent bad request: %s", ", ".join(fields[:5]))
            if operation not in contract.PARAMS:
                operation = "ensure_access_token"  # keep the response schema valid
            return respond(False, error={"kind": "bad_request"})

        bind(order_id=getattr(params, "order_ref", None))
        try:
            result = await handlers[req.operation](params)
        except PayPalApiError as exc:
            logger.info("PayPal agent %s -> PayPal %s %s", req.operation, exc.status, exc.issue or exc.name)
            return respond(False, error=_error("api", exc))
        except PayPalAuthError:
            logger.warning("PayPal agent %s -> OAuth failed", req.operation)
            return respond(False, error=_error("auth"))
        except PayPalUnavailable:
            logger.warning("PayPal agent %s -> PayPal unavailable", req.operation)
            return respond(False, error=_error("unavailable"))
        except Exception as exc:
            logger.error("PayPal agent %s failed: %s", req.operation, type(exc).__name__)
            return respond(False, error=_error("invalid"))
        logger.info("PayPal agent %s ok", req.operation)
        return respond(True, result=result)

    return app


def paypal_agent_app() -> FastAPI:
    """``uvicorn --factory`` entry point: reads the agent's own environment / .env."""
    settings = get_settings()
    configure_logging(settings)
    return create_paypal_agent_app(settings)
