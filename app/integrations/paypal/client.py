"""PayPal Sandbox HTTP client with OAuth token caching and retry logic (§3.7, AC3.4)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Mapping

import httpx

from app.core.config import Settings
from app.core.metrics import METRICS
from app.integrations.paypal.errors import PayPalApiError, PayPalAuthError, PayPalUnavailable


logger = logging.getLogger(__name__)

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"

_VERSION_SEGMENT = re.compile(r"^v\d+$")


def operation_label(method: str, path: str) -> str:
    """Metric label without provider ids: ``POST v2/checkout/orders/{id}/capture``."""
    segments = [
        s if _VERSION_SEGMENT.match(s) or not any(c.isdigit() for c in s) else "{id}"
        for s in path.strip("/").split("/")
    ]
    return f"{method.upper()} {'/'.join(segments)}"


def _outcome(status: int) -> str:
    return f"{status // 100}xx"


class PayPalClient:
    """HTTP client for PayPal Sandbox APIs.

    Features:
    - Base URL https://api-m.sandbox.paypal.com (live endpoints rejected).
    - OAuth client_credentials token caching with asyncio.Lock, refreshing when expires_in < 60s.
    - Automatic 401 retry-once with fresh access token.
    - Strict timeouts from settings.paypal_http_timeout_seconds.
    - Security: Never logs request or response bodies; logs PayPal-Debug-Id on failure.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
    ) -> None:
        self._client_id = settings.paypal_client_id
        if settings.paypal_client_secret is not None:
            self._client_secret = (
                settings.paypal_client_secret.get_secret_value()
                if hasattr(settings.paypal_client_secret, "get_secret_value")
                else str(settings.paypal_client_secret)
            )
        else:
            self._client_secret = None
        self._timeout = float(getattr(settings, "paypal_http_timeout_seconds", 10.0))
        self._base_url = (base_url or SANDBOX_BASE_URL).rstrip("/")

        # Live safety check
        if "api-m.paypal.com" in self._base_url and "sandbox" not in self._base_url:
            raise ValueError(f"Live PayPal endpoint rejected: {self._base_url}")

        self._transport = transport
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        )

    def clear_token_cache(self) -> None:
        """Clear cached OAuth token (called on 401)."""
        self._token = None
        self._token_expires_at = 0.0

    async def ensure_access_token(self) -> str:
        """Get or refresh OAuth access token using client credentials."""
        now = time.time()
        if self._token is not None and now < self._token_expires_at:
            return self._token

        async with self._lock:
            # Double-check inside lock
            now = time.time()
            if self._token is not None and now < self._token_expires_at:
                return self._token

            if not self._client_id or not self._client_secret:
                raise PayPalAuthError("PayPal client_id and secret must be configured")

            url = f"{self._base_url}/v1/oauth2/token"
            started = time.perf_counter()
            try:
                async with self._http_client() as client:
                    resp = await client.post(
                        url,
                        auth=(self._client_id, self._client_secret),
                        data={"grant_type": "client_credentials"},
                        headers={"Accept": "application/json", "Accept-Language": "en_US"},
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                METRICS.paypal_requests.inc("POST v1/oauth2/token", "network_error")
                logger.warning("PayPal OAuth token network error: %s", type(exc).__name__)
                raise PayPalUnavailable(f"PayPal OAuth unreachable: {type(exc).__name__}") from exc
            except Exception as exc:
                raise PayPalAuthError(f"PayPal OAuth request failed: {exc}") from exc

            METRICS.paypal_requests.inc("POST v1/oauth2/token", _outcome(resp.status_code))
            METRICS.paypal_duration.observe(time.perf_counter() - started, "POST v1/oauth2/token")
            if resp.status_code != 200:
                debug_id = resp.headers.get("PayPal-Debug-Id") or resp.headers.get("paypal-debug-id")
                logger.warning("PayPal OAuth failed status=%d debug_id=%s", resp.status_code, debug_id)
                error = PayPalUnavailable if resp.status_code >= 500 else PayPalAuthError
                raise error(f"OAuth failed with status {resp.status_code}")

            try:
                data = resp.json()
                token = data["access_token"]
                expires_in = int(data.get("expires_in", 3600))
            except Exception as exc:
                raise PayPalAuthError(f"Invalid OAuth response JSON: {exc}") from exc

            self._token = token
            # Expire 60s early for safety
            self._token_expires_at = now + max(expires_in - 60, 0)
            return self._token

    def _parse_error(self, resp: httpx.Response) -> PayPalApiError:
        """Parse error details from response; captures debug_id and issue; never logs bodies."""
        debug_id = resp.headers.get("PayPal-Debug-Id") or resp.headers.get("paypal-debug-id")
        name = "UNKNOWN_ERROR"
        issue: str | None = None
        details: list[dict[str, Any]] = []

        try:
            data = resp.json()
            if isinstance(data, dict):
                name = data.get("name") or data.get("error") or "API_ERROR"
                raw_details = data.get("details")
                if isinstance(raw_details, list):
                    details = raw_details
                    if len(details) > 0 and isinstance(details[0], dict):
                        issue = details[0].get("issue")
                debug_id = data.get("debug_id") or debug_id
        except Exception:
            pass

        logger.warning(
            "PayPal API error status=%d name=%s issue=%s debug_id=%s",
            resp.status_code,
            name,
            issue,
            debug_id,
        )
        return PayPalApiError(
            status=resp.status_code,
            name=name,
            issue=issue,
            debug_id=debug_id,
            details=details,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        params: Mapping[str, Any] | None = None,
        retry_401: bool = True,
    ) -> httpx.Response:
        """Execute authenticated request with 401 retry-once."""
        token = await self.ensure_access_token()
        url = f"{self._base_url}/{path.lstrip('/')}"
        req_headers = dict(headers or {})
        req_headers["Authorization"] = f"Bearer {token}"
        req_headers.setdefault("Accept", "application/json")
        operation = operation_label(method, path)
        started = time.perf_counter()

        try:
            async with self._http_client() as client:
                resp = await client.request(
                    method=method,
                    url=url,
                    headers=req_headers,
                    json=json,
                    content=content,
                    params=params,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            METRICS.paypal_requests.inc(operation, "network_error")
            logger.warning("PayPal %s timed out or network error: %s", operation, type(exc).__name__)
            raise PayPalUnavailable(f"PayPal request failed: {type(exc).__name__}") from exc
        except Exception as exc:
            raise PayPalUnavailable(f"PayPal unexpected client error: {exc}") from exc

        METRICS.paypal_requests.inc(operation, _outcome(resp.status_code))
        METRICS.paypal_duration.observe(time.perf_counter() - started, operation)

        # Handle 401: clear token and retry once
        if resp.status_code == 401 and retry_401:
            logger.info("PayPal returned 401; clearing token cache and retrying once")
            self.clear_token_cache()
            return await self.request(
                method,
                path,
                headers=headers,
                json=json,
                content=content,
                params=params,
                retry_401=False,
            )

        if resp.status_code >= 400:
            raise self._parse_error(resp)

        return resp
