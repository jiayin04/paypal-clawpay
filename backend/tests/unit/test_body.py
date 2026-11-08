from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.body import read_limited_body
from app.core.exceptions import register_exception_handlers


def _client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/echo")
    async def echo(request: Request) -> dict:
        body = await read_limited_body(request, 10)
        return {"n": len(body)}

    return TestClient(app)


def test_within_limit() -> None:
    assert _client().post("/echo", content=b"0123456789").json() == {"n": 10}


def test_declared_length_over_limit() -> None:
    resp = _client().post("/echo", content=b"0123456789A")
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


def test_streamed_body_over_limit() -> None:
    def gen():
        yield b"012345"
        yield b"6789AB"

    resp = _client().post("/echo", content=gen())
    assert resp.status_code == 413
