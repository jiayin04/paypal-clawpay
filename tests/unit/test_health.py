from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.routes.health as health_module
import app.integrations.supabase.client as supabase_client_module
from app.core.config import Settings
from app.main import create_app

SERVICE_KEY = "fake-service-role-key-0123456789"
PAYPAL_SECRET = "fake-paypal-client-secret-xyz"
INTERNAL_TOKEN = "fake-internal-service-token-abc-0123456789"  # >= 32 chars (startup check)
SUPABASE_URL = "https://fakeproject.supabase.co"


def configured_settings() -> Settings:
    return Settings(
        _env_file=None,
        supabase_url=SUPABASE_URL,
        supabase_service_role_key=SERVICE_KEY,
        paypal_client_secret=PAYPAL_SECRET,
        internal_service_token=INTERNAL_TOKEN,
    )


class _FakeQuery:
    def __init__(self, error: Exception | None) -> None:
        self._error = error

    def select(self, *_: object, **__: object) -> "_FakeQuery":
        return self

    def limit(self, *_: object) -> "_FakeQuery":
        return self

    def execute(self) -> object:
        if self._error is not None:
            raise self._error
        return object()


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.queries = 0

    def table(self, _: str) -> _FakeQuery:
        self.queries += 1
        return _FakeQuery(self._error)


def _assert_no_secrets(text: str) -> None:
    for value in (SERVICE_KEY, PAYPAL_SECRET, INTERNAL_TOKEN, SUPABASE_URL, "fakeproject"):
        assert value not in text


def test_health_ok_without_secrets() -> None:
    client = TestClient(create_app(configured_settings()))
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "clawpay-middleware"
    _assert_no_secrets(resp.text)


def test_ready_not_configured_never_creates_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*_: object, **__: object) -> None:
        pytest.fail("create_client must not be called when Supabase is not configured")

    monkeypatch.setattr(supabase_client_module, "create_client", _fail)
    client = TestClient(create_app(Settings(_env_file=None)))
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready", "checks": {"supabase": "not_configured"}}


def test_ready_error_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    error = RuntimeError(f"boom {SUPABASE_URL} key={SERVICE_KEY}")
    monkeypatch.setattr(
        supabase_client_module, "create_client", lambda *a, **k: FakeClient(error)
    )
    client = TestClient(create_app(configured_settings()))
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["supabase"] == "error"
    assert "boom" not in resp.text
    _assert_no_secrets(resp.text)


def test_ready_client_creation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_: object, **__: object) -> None:
        raise ValueError(f"invalid key {SERVICE_KEY}")

    monkeypatch.setattr(supabase_client_module, "create_client", _raise)
    client = TestClient(create_app(configured_settings()))
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["supabase"] == "error"
    _assert_no_secrets(resp.text)


def test_ready_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[tuple] = []

    def _create(*args: object, **kwargs: object) -> FakeClient:
        created.append(args)
        return FakeClient()

    monkeypatch.setattr(supabase_client_module, "create_client", _create)
    client = TestClient(create_app(configured_settings()))
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": {"supabase": "ok"}}
    # Client is cached on app.state.
    client.get("/health/ready")
    assert len(created) == 1


def test_ready_result_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(supabase_client_module, "create_client", lambda *a, **k: fake)
    clock = [1000.0]
    # Replace only the module's reference to `time`, not the global clock.
    monkeypatch.setattr(health_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    client = TestClient(create_app(configured_settings()))
    for _ in range(5):
        assert client.get("/health/ready").status_code == 200
    assert fake.queries == 1

    clock[0] += health_module.READINESS_CACHE_SECONDS + 0.1
    assert client.get("/health/ready").status_code == 200
    assert fake.queries == 2


def test_ready_error_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient(RuntimeError("down"))
    monkeypatch.setattr(supabase_client_module, "create_client", lambda *a, **k: fake)
    client = TestClient(create_app(configured_settings()))
    assert client.get("/health/ready").status_code == 503
    assert client.get("/health/ready").status_code == 503
    assert fake.queries == 1


def test_unknown_route_404() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))
    assert client.get("/does-not-exist").status_code == 404


def test_unhandled_error_returns_generic_500() -> None:
    app = create_app(Settings(_env_file=None))

    @app.get("/_test_raise")
    async def _raise() -> None:
        raise RuntimeError(f"secret detail {SERVICE_KEY}")

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/_test_raise")
    assert resp.status_code == 500
    assert resp.json() == {"error": {"code": "internal_error", "message": "Internal server error"}}
    assert SERVICE_KEY not in resp.text


def test_docs_disabled_in_production() -> None:
    client = TestClient(create_app(Settings(_env_file=None, app_env="production")))
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
