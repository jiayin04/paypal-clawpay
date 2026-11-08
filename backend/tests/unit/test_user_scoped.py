"""User-scoped PostgREST client (design §1.1): headers, http_client, always closed."""

import warnings

import httpx
import pytest

from app.core.exceptions import ServiceUnavailable, Unauthorized
from app.repositories.user_scoped import make_user_reader_factory
from tests.fakes.app import PUBLISHABLE_KEY, make_settings
from tests.fakes.keys import SUPABASE_URL

JWT = "user.jwt.value"


class _Recorder:
    def __init__(self, status: int = 200, body: object = None) -> None:
        self.requests: list[httpx.Request] = []
        self.clients: list[httpx.Client] = []
        self.status = status
        self.body = body if body is not None else []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    def factory(self) -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(self.handler), timeout=10)
        self.clients.append(client)
        return client


@pytest.mark.anyio
async def test_headers_columns_and_close() -> None:
    rec = _Recorder(body=[{"id": "d1", "device_name": "x"}])
    factory = make_user_reader_factory(make_settings(), http_client_factory=rec.factory)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        async with factory(JWT) as reader:
            rows = await reader.list_devices()
    assert rows == [{"id": "d1", "device_name": "x"}]
    request = rec.requests[0]
    assert str(request.url).startswith(f"{SUPABASE_URL}/rest/v1/devices")
    assert request.headers["apikey"] == PUBLISHABLE_KEY
    assert request.headers["authorization"] == f"Bearer {JWT}"
    assert request.headers["accept"] == "application/json"
    assert request.headers["content-type"] == "application/json"
    select = request.url.params["select"]
    assert "token_hash" not in select and "public_key" not in select
    assert rec.clients[0].is_closed


@pytest.mark.anyio
async def test_client_closed_when_read_raises() -> None:
    rec = _Recorder(status=500, body={"code": "XX000", "message": "boom"})
    factory = make_user_reader_factory(make_settings(), http_client_factory=rec.factory)
    with pytest.raises(ServiceUnavailable):
        async with factory(JWT) as reader:
            await reader.list_orders(limit=5, before=None)
    assert rec.clients[0].is_closed


@pytest.mark.anyio
async def test_order_detail_not_found() -> None:
    rec = _Recorder(body=[])
    factory = make_user_reader_factory(make_settings(), http_client_factory=rec.factory)
    async with factory(JWT) as reader:
        assert await reader.get_order_detail("00000000-0000-0000-0000-000000000001") is None
    assert len(rec.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('code,expected', [('PGRST301',Unauthorized),('XX000',ServiceUnavailable)])
async def test_dashboard_error_mapping(code,expected):
    rec = _Recorder(status=401 if code == 'PGRST301' else 500, body={'code':code,'message':'hidden'})
    factory = make_user_reader_factory(make_settings(),http_client_factory=rec.factory)
    with pytest.raises(expected):
        async with factory(JWT) as reader:
            await reader.list_conversations(limit=20,before=None)
    assert rec.clients[-1].is_closed
