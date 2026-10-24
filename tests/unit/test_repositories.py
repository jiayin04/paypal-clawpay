"""Repository layer: deadlock retry (design §1.1) and PostgREST error mapping."""

from types import SimpleNamespace

import pytest
from postgrest.exceptions import APIError

from app.core.exceptions import ServiceUnavailable
from app.repositories import errors as errors_mod
from app.repositories.errors import RepositoryError, call_rpc_with_retry
from app.repositories.supabase_repos import SupabaseDeviceRepository, SupabaseOrderRepository
from tests.fakes.store import InMemoryStore


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(errors_mod, "RETRY_DELAY_SECONDS", 0)


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["40P01", "40001"])
async def test_retry_once_then_success(code: str) -> None:
    store = InMemoryStore()
    repos = store.repositories()
    user = store.add_user()
    store.fail_next("spending_summary", code)
    row = await call_rpc_with_retry("spending_summary", lambda: repos.spending.summary(user, "Asia/Kuala_Lumpur"))
    assert row.committed_minor == 0
    assert store.calls.count("spending_summary") == 2


@pytest.mark.anyio
async def test_second_deadlock_is_503() -> None:
    store = InMemoryStore()
    repos = store.repositories()
    store.fail_next("spending_summary", "40P01", times=2)
    with pytest.raises(ServiceUnavailable) as exc_info:
        await call_rpc_with_retry("spending_summary", lambda: repos.spending.summary("u", "Asia/Kuala_Lumpur"))
    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "service_unavailable"


@pytest.mark.anyio
async def test_non_retryable_error_propagates_without_retry() -> None:
    store = InMemoryStore()
    repos = store.repositories()
    store.fail_next("spending_summary", "23505", "some_key")
    with pytest.raises(RepositoryError) as exc_info:
        await call_rpc_with_retry("spending_summary", lambda: repos.spending.summary("u", "Asia/Kuala_Lumpur"))
    assert exc_info.value.constraint == "some_key"
    assert store.calls.count("spending_summary") == 1


class _FakeRpc:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name: str, params: dict) -> "_FakeRpc":
        self.calls.append((name, params))
        return self

    def execute(self) -> object:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return SimpleNamespace(data=self.outcome)


@pytest.mark.anyio
async def test_supabase_rpc_maps_api_error_with_constraint() -> None:
    err = APIError({"code": "23505", "message": 'duplicate key value violates unique constraint "devices_active_public_key_key"'})
    client = _FakeRpc(err)
    repo = SupabaseDeviceRepository(lambda: client)  # type: ignore[arg-type]
    with pytest.raises(RepositoryError) as exc_info:
        await repo.register(user_id="u", device_name="d", public_key="k", key_version=1, token_hash="t",
                            llm_token_hash="l", actor_type="admin", actor_id=None)
    assert exc_info.value.code == "23505"
    assert exc_info.value.constraint == "devices_active_public_key_key"
    assert client.calls[0][0] == "register_device"
    assert set(client.calls[0][1]) == {"p_user_id", "p_device_name", "p_public_key", "p_key_version",
                                       "p_token_hash", "p_llm_token_hash", "p_actor_type", "p_actor_id"}


@pytest.mark.anyio
async def test_supabase_rpc_unknown_error_is_repository_error(caplog: pytest.LogCaptureFixture) -> None:
    client = _FakeRpc(RuntimeError("https://secret.supabase.co key=abc"))
    repo = SupabaseOrderRepository(lambda: client)  # type: ignore[arg-type]
    with pytest.raises(RepositoryError):
        await repo.expire("o1")
    assert "secret.supabase.co" not in caplog.text


@pytest.mark.anyio
async def test_supabase_create_order_params() -> None:
    from datetime import UTC, datetime

    client = _FakeRpc([{"ok": True, "reason": "created", "order_id": "o1", "deduplicated": False}])
    repo = SupabaseOrderRepository(lambda: client)  # type: ignore[arg-type]
    result = await repo.create_from_quote(
        user_id="u", device_id="d", merchant_code="grocer", currency="MYR",
        quote_expires_at=datetime(2026, 1, 1, tzinfo=UTC), quote_reference="q", cart_sha256="a" * 64,
        items=[{"sku": "S", "name": "n", "qty": 1, "unit_price_minor": 5}], total_minor=5,
    )
    assert result.ok and result.data == {"order_id": "o1", "deduplicated": False}
    name, params = client.calls[0]
    assert name == "create_order_from_quote"
    assert params["p_items"] == [{"sku": "S", "name": "n", "qty": 1, "unit_price_minor": 5}]
    assert params["p_quote_expires_at"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.anyio
async def test_db_not_configured_is_503() -> None:
    from app.core.config import Settings
    from app.services.container import supabase_repositories

    repos = supabase_repositories(Settings(_env_file=None))
    with pytest.raises(ServiceUnavailable):
        await repos.devices.find_by_token_hash("x")
