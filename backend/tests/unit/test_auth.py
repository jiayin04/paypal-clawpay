"""JWT verification (design §1.4) and device-token auth limiter (§1.3)."""

import time
from typing import Annotated

import jwt
import pytest
from fastapi import Depends

from app.api.dependencies import require_device, require_user
from app.core.security import generate_device_token, generate_llm_token
from app.services.context import DeviceContext, UserContext
from tests.fakes.app import make_harness
from tests.fakes.keys import ISSUER


def _with_probe_routes(h):  # type: ignore[no-untyped-def]
    @h.app.get("/_probe/device")
    async def probe_device(device: Annotated[DeviceContext, Depends(require_device)]) -> dict:
        return {"device_id": device.device_id}

    @h.app.get("/_probe/user")
    async def probe_user(user: Annotated[UserContext, Depends(require_user)]) -> dict:
        return {"user_id": user.user_id}

    return h


def _get(h, path: str, token: str | None, **headers: str):  # type: ignore[no-untyped-def]
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return h.client.get(path, headers=headers)


# --- JWT ----------------------------------------------------------------------------


def test_valid_jwt_accepted() -> None:
    h = _with_probe_routes(make_harness())
    user = h.new_user()
    resp = _get(h, "/_probe/user", h.jwt(user))
    assert resp.status_code == 200
    assert resp.json() == {"user_id": user}


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "anon"},
        {"iss": "https://evil.example/auth/v1"},
        {"exp": int(time.time()) - 3600},
        {"role": "anon"},
        {"sub": "not-a-uuid"},
        {"exp": None},
    ],
)
def test_bad_claims_rejected(claims: dict) -> None:
    h = _with_probe_routes(make_harness())
    resp = _get(h, "/_probe/user", h.jwt(h.new_user(), **claims))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    assert resp.headers["www-authenticate"] == "Bearer"


def test_alg_none_and_hs256_without_secret_rejected() -> None:
    h = _with_probe_routes(make_harness())
    user = h.new_user()
    claims = {"sub": user, "aud": "authenticated", "iss": ISSUER, "role": "authenticated",
              "exp": int(time.time()) + 600}
    none_token = jwt.encode(claims, key=None, algorithm="none")
    hs_token = jwt.encode(claims, "x" * 32, algorithm="HS256")
    for token in (none_token, hs_token, "garbage", generate_device_token()):
        assert _get(h, "/_probe/user", token).status_code == 401


def test_jwt_signed_by_other_key_rejected() -> None:
    h = _with_probe_routes(make_harness())
    other = make_harness()
    assert _get(h, "/_probe/user", other.jwt(h.new_user())).status_code == 401


def test_hs256_legacy_secret_path() -> None:
    secret = "legacy-jwt-secret-" * 3
    h = _with_probe_routes(make_harness(supabase_jwt_secret=secret))
    user = h.new_user()
    claims = {"sub": user, "aud": "authenticated", "iss": ISSUER, "role": "authenticated",
              "exp": int(time.time()) + 600}
    assert _get(h, "/_probe/user", jwt.encode(claims, secret, algorithm="HS256")).status_code == 200
    # An ES256 token is not accepted when the project uses the legacy secret.
    assert _get(h, "/_probe/user", h.jwt(user)).status_code == 401


def test_dashboard_not_configured_before_jwt_check() -> None:
    h = _with_probe_routes(make_harness(supabase_publishable_key=None))
    user = h.new_user()
    for token in (h.jwt(user), "garbage"):
        resp = _get(h, "/_probe/user", token)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "dashboard_not_configured"


def test_jwks_failure_is_503() -> None:
    from app.services import auth_service

    h = _with_probe_routes(make_harness())

    def failing(_: str) -> None:
        raise auth_service.AuthUnavailable()

    h.services.auth._resolver = failing
    resp = _get(h, "/_probe/user", h.jwt(h.new_user()))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "auth_unavailable"


# --- device tokens and limiter ---------------------------------------------------------


def test_device_token_auth_and_kinds() -> None:
    h = _with_probe_routes(make_harness())
    token, device_id, _ = h.new_device()
    assert _get(h, "/_probe/device", token).json() == {"device_id": device_id}
    for bad in (None, "cpd_short", generate_device_token(), generate_llm_token(), f"{token}x"):
        resp = _get(h, "/_probe/device", bad)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"


def test_revoked_and_pending_devices_rejected_and_audited_once() -> None:
    h = _with_probe_routes(make_harness())
    revoked, revoked_id, _ = h.new_device(status="revoked")
    pending, _, _ = h.new_device(status="pending")
    assert _get(h, "/_probe/device", pending).status_code == 401
    for _ in range(3):
        assert _get(h, "/_probe/device", revoked).status_code == 401
    events = h.store.audit_events("device.revoked_token_used")
    assert len(events) == 1 and events[0]["entity_id"] == revoked_id


def test_no_cache_revocation_applies_next_request() -> None:
    h = _with_probe_routes(make_harness())
    token, device_id, _ = h.new_device()
    assert _get(h, "/_probe/device", token).status_code == 200
    h.store.devices[device_id]["status"] = "revoked"
    assert _get(h, "/_probe/device", token).status_code == 401


def test_malformed_tokens_never_block_valid_device() -> None:
    h = _with_probe_routes(make_harness())
    token, _, _ = h.new_device()
    for _ in range(25):
        assert _get(h, "/_probe/device", "garbage").status_code in (401, 429)
    assert _get(h, "/_probe/device", "garbage").status_code == 429
    assert _get(h, "/_probe/device", token).status_code == 200


def test_unknown_well_formed_tokens_hit_429_while_valid_succeeds() -> None:
    h = _with_probe_routes(make_harness())
    token, _, _ = h.new_device()
    statuses = [_get(h, "/_probe/device", generate_device_token()).status_code for _ in range(25)]
    assert statuses[:20] == [401] * 20
    assert set(statuses[20:]) == {429}
    resp = _get(h, "/_probe/device", generate_device_token())
    assert resp.status_code == 429 and int(resp.headers["retry-after"]) >= 1
    assert _get(h, "/_probe/device", token).status_code == 200


def test_limiter_keyed_by_rightmost_forwarded_ip() -> None:
    h = _with_probe_routes(make_harness(client_ip_header="x-forwarded-for-last"))
    for _ in range(21):
        _get(h, "/_probe/device", "garbage", **{"X-Forwarded-For": "1.1.1.1, 203.0.113.5"})
    # Same real client with a different spoofed leftmost entry is still limited.
    assert _get(h, "/_probe/device", "garbage", **{"X-Forwarded-For": "9.9.9.9, 203.0.113.5"}).status_code == 429
    # A different rightmost (real) client is not.
    assert _get(h, "/_probe/device", "garbage", **{"X-Forwarded-For": "1.1.1.1, 203.0.113.6"}).status_code == 401


def test_cf_connecting_ip_used() -> None:
    h = _with_probe_routes(make_harness(client_ip_header="cf-connecting-ip"))
    for _ in range(21):
        _get(h, "/_probe/device", "garbage", **{"CF-Connecting-IP": "198.51.100.1"})
    assert _get(h, "/_probe/device", "garbage", **{"CF-Connecting-IP": "198.51.100.1"}).status_code == 429
    assert _get(h, "/_probe/device", "garbage", **{"CF-Connecting-IP": "198.51.100.2"}).status_code == 401


def test_pepper_missing_is_503() -> None:
    h = _with_probe_routes(make_harness(mcp_enabled=False, device_token_pepper=None))
    resp = _get(h, "/_probe/device", generate_device_token())
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "device_auth_not_configured"


def test_db_failure_on_lookup_is_503() -> None:
    h = _with_probe_routes(make_harness())
    token, _, _ = h.new_device()
    h.store.fail_next("devices.find_by_token_hash", "08006")
    assert _get(h, "/_probe/device", token).status_code == 503


def test_last_seen_updated_at_most_every_60s() -> None:
    h = _with_probe_routes(make_harness())
    token, device_id, _ = h.new_device()
    _get(h, "/_probe/device", token)
    first = h.store.devices[device_id]["last_seen_at"]
    assert first == h.clock.utc_now()
    h.clock.advance(30)
    _get(h, "/_probe/device", token)
    assert h.store.devices[device_id]["last_seen_at"] == first
    h.clock.advance(31)
    _get(h, "/_probe/device", token)
    assert h.store.devices[device_id]["last_seen_at"] == h.clock.utc_now()
