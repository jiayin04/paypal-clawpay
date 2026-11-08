"""OpenAI-compatible proxy with isolated credentials and per-device KL-day budgets."""
import asyncio
import json
import logging
from typing import Any

import httpx
from fastapi.responses import Response, StreamingResponse

from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import ApiError, RateLimited
from app.core.security import SlidingWindowLimiter
from app.schemas.llm import shape_request


logger = logging.getLogger(__name__)

class LLMProxyService:
    def __init__(self, settings: Settings, clock: Clock, client: httpx.AsyncClient) -> None:
        self.settings,self.clock,self.client = settings,clock,client
        self.limiter = SlidingWindowLimiter(settings.llm_proxy_requests_per_minute,monotonic=clock.monotonic)
        self._day = None
        self._usage: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def _budget(self, device_id: str, amount: int = 0, check: bool = False) -> None:
        from zoneinfo import ZoneInfo
        today = self.clock.utc_now().astimezone(ZoneInfo('Asia/Kuala_Lumpur')).date()
        async with self._lock:
            if self._day != today:
                self._day,self._usage = today,{}
            used = self._usage.get(device_id,0)
            if check and used >= self.settings.llm_proxy_daily_token_limit:
                raise ApiError(429,'budget_exceeded','Daily token budget exceeded.')
            self._usage[device_id] = used + amount

    @staticmethod
    def _tokens(data: Any) -> int | None:
        usage = data.get('usage') if isinstance(data,dict) else None
        value = usage.get('total_tokens') if isinstance(usage,dict) else None
        return value if type(value) is int and value >= 0 else None

    async def complete(self, device_id: str, body: Any) -> Response:
        s = self.settings
        if not all((s.llm_proxy_upstream_base_url,s.llm_proxy_upstream_api_key,s.llm_proxy_model)):
            raise ApiError(503,'proxy_not_configured','LLM proxy is not configured.')
        retry = await self.limiter.acquire(device_id)
        if retry is not None:
            raise RateLimited(retry)
        shaped, wants_usage = shape_request(body,s)
        await self._budget(device_id,check=True)
        fallback = max(shaped.get('max_tokens',0),shaped.get('max_completion_tokens',0))
        request = self.client.build_request('POST',s.llm_proxy_upstream_base_url+'/chat/completions',
                    json=shaped,headers={'Authorization':'Bearer '+s.llm_proxy_upstream_api_key.get_secret_value(),'Accept-Encoding':'identity'},timeout=s.llm_proxy_timeout_seconds)
        try:
            upstream = await self.client.send(request,stream=True)
        except httpx.HTTPError:
            raise ApiError(502,'upstream_error','LLM upstream is unavailable.') from None
        if upstream.status_code != 200:
            try:
                raw = await upstream.aread()
                message = None
                if upstream.status_code == 400:
                    try:
                        value = json.loads(raw).get('error',{}).get('message')
                        if isinstance(value,str) and len(value) <= 300:
                            message = value
                    except (ValueError,AttributeError):
                        pass
                if upstream.status_code == 429:
                    raise ApiError(429,'rate_limited','Upstream rate limit exceeded.')
                if upstream.status_code == 400:
                    raise ApiError(400,'invalid_request_error',message or 'Invalid upstream request.')
                logger.warning('LLM upstream status=%d',upstream.status_code)
                raise ApiError(502,'upstream_error','LLM upstream is unavailable.')
            finally:
                await upstream.aclose()
        if not shaped.get('stream'):
            try:
                raw = await upstream.aread()
                tokens = self._tokens(json.loads(raw))
            except (httpx.HTTPError,ValueError):
                await self._budget(device_id,fallback)
                raise ApiError(502,'upstream_error','Invalid upstream response.') from None
            finally:
                await upstream.aclose()
            await self._budget(device_id,tokens if tokens is not None else fallback)
            return Response(raw,media_type='application/json',headers={'Cache-Control':'no-store'})

        async def relay():
            tokens = None
            buffer = b''
            drop_separator = False
            def parse(line):
                nonlocal tokens
                if not line.startswith(b'data: '):
                    return False
                try:
                    data = json.loads(line[6:])
                except (ValueError,UnicodeError):
                    return False
                found = self._tokens(data)
                if found is not None:
                    tokens = found
                return isinstance(data,dict) and data.get('choices') == [] and 'usage' in data
            try:
                # Raw relay is byte-exact; MockTransport may return already-read content.
                chunks = upstream.aiter_bytes() if upstream.is_stream_consumed else upstream.aiter_raw()
                async for chunk in chunks:
                    buffer += chunk
                    if wants_usage:
                        yield chunk
                    while b'\n' in buffer:
                        line,buffer = buffer.split(b'\n',1)
                        line = line.rstrip(b'\r')
                        usage_only = parse(line)
                        if not wants_usage:
                            if usage_only:
                                drop_separator = True
                                continue
                            if drop_separator and not line:
                                drop_separator = False
                                continue
                            drop_separator = False
                            yield line+b'\n'
                if buffer:
                    usage_only = parse(buffer)
                    if not wants_usage and not usage_only:
                        yield buffer+b'\n'
            except httpx.HTTPError as exc:
                logger.warning('LLM stream ended: %s',type(exc).__name__)
            finally:
                # Accounting and connection cleanup must survive client disconnects.
                import anyio
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
                    await self._budget(device_id,tokens if tokens is not None else fallback)
        return StreamingResponse(relay(),media_type='text/event-stream',headers={'Cache-Control':'no-store'})
