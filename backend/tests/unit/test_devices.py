"""Device registry endpoints (contract §6.1/§6.5, design §2.3, AC2.7)."""

import logging

import pytest

from app.core.security import DEVICE_TOKEN_RE, LLM_TOKEN_RE, hash_token
from tests.fakes.app import ADMIN_TOKEN, PEPPER, Harness, make_harness
from tests.fakes.keys import p256_public_pem, p384_public_pem, rsa_public_pem

REGISTER = "/api/v1/devices/register"


def _admin() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _register(h: Harness, body: dict, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    return h.client.post(REGISTER, json=body, headers=_admin() if headers is None else headers)


def _body(user_id: str, /, **overrides: object) -> dict:
    body = {"user_id": user_id, "device_name": "desk-claw", "public_key_pem": p256_public_pem(), "key_version": 1}
    body.update(overrides)
    return body


def test_admin_registers_device_with_two_tokens(caplog: pytest.LogCaptureFixture) -> None:
    h = make_harness()
    user = h.new_user()
    with caplog.at_level(logging.DEBUG):
        resp = _register(h, _body(user))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"device_id", "device_token", "llm_token", "key_algorithm", "key_version", "status"}
    assert DEVICE_TOKEN_RE.match(body["device_token"]) and LLM_TOKEN_RE.match(body["llm_token"])
    assert body["key_algorithm"] == "ecdsa-p256-sha256" and body["status"] == "active"
    row = h.store.devices[body["device_id"]]
    assert row["token_hash"] == hash_token(PEPPER, body["device_token"])
    assert row["llm_token_hash"] == hash_token(PEPPER, body["llm_token"])
    assert body["device_token"] not in str(h.store.devices) and body["llm_token"] not in str(h.store.devices)
    assert body["device_token"] not in caplog.text and body["llm_token"] not in caplog.text
    assert row["public_key"].startswith("-----BEGIN PUBLIC KEY-----")
    events = [a["event_type"] for a in h.store.audit_logs]
    assert events == ["device.registered", "device.token_issued"]
    assert h.store.audit_logs[1]["details"] == {"kinds": ["device", "llm"]}
    assert resp.headers["cache-control"] == "no-store"


def test_registered_tokens_work_for_their_kind_only() -> None:
    h = make_harness()
    body = _register(h, _body(h.new_user())).json()
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert h.mcp(body["device_token"], init).status_code == 200
    assert h.mcp(body["llm_token"], init).status_code == 401


def test_jwt_caller_forbidden_when_flag_off() -> None:
    h = make_harness()
    user = h.new_user()
    resp = _register(h, {"garbage": True}, {"Authorization": f"Bearer {h.jwt(user)}"})
    assert resp.status_code == 403
    assert resp.json()["error"] == {"code": "forbidden", "message": "Device registration requires the admin token."}


def test_owner_registration_when_flag_on() -> None:
    h = make_harness(device_owner_registration_enabled=True)
    user, other = h.new_user(), h.new_user()
    auth = {"Authorization": f"Bearer {h.jwt(user)}"}
    resp = _register(h, {"device_name": "mine", "public_key_pem": p256_public_pem()}, auth)
    assert resp.status_code == 201
    assert h.store.devices[resp.json()["device_id"]]["user_id"] == user
    assert _register(h, _body(user), auth).status_code == 201
    resp = _register(h, _body(other), auth)
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "forbidden"


def test_unauthenticated_and_bad_admin_token() -> None:
    h = make_harness()
    user = h.new_user()
    assert _register(h, _body(user), {}).status_code == 401
    assert _register(h, _body(user), {"Authorization": "Bearer wrong-admin-token"}).status_code == 401


def test_register_failure_limiter_never_blocks_valid_admin() -> None:
    h = make_harness()
    user = h.new_user()
    codes = [_register(h, _body(user), {"Authorization": "Bearer nope"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]
    assert _register(h, _body(user)).status_code == 201


@pytest.mark.parametrize(
    "pem", ["not a key", p384_public_pem(), rsa_public_pem(),
            "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"],
)
def test_invalid_public_key(pem: str) -> None:
    h = make_harness()
    resp = _register(h, _body(h.new_user(), public_key_pem=pem))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_public_key"


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_id": None},
        {"user_id": "not-a-uuid"},
        {"device_name": ""},
        {"device_name": "x" * 65},
        {"device_name": "bad\nname"},
        {"key_version": 0},
        {"key_version": 1001},
        {"key_version": "1"},
        {"public_key_pem": "x" * 1025},
        {"extra": 1},
    ],
)
def test_validation_errors(overrides: dict) -> None:
    h = make_harness()
    body = _body(h.new_user(), **overrides)
    if overrides.get("user_id", "") is None:
        body.pop("user_id")
    resp = _register(h, body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_body_too_large() -> None:
    h = make_harness()
    resp = h.client.post(REGISTER, content=b"{" + b" " * 17000 + b"}", headers={**_admin(), "Content-Type": "application/json"})
    assert resp.status_code == 413


def test_unknown_user_404() -> None:
    h = make_harness()
    resp = _register(h, _body("00000000-0000-0000-0000-000000000099"))
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "user_not_found"


def test_duplicate_key_409_and_reregister_after_revoke() -> None:
    h = make_harness()
    user = h.new_user()
    pem = p256_public_pem()
    first = _register(h, _body(user, public_key_pem=pem)).json()
    resp = _register(h, _body(h.new_user(), public_key_pem=pem))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "public_key_already_registered"
    assert h.client.delete(f"/api/v1/devices/{first['device_id']}", headers=_admin()).status_code == 200
    assert _register(h, _body(user, public_key_pem=pem)).status_code == 201


def test_concurrent_registration_race_maps_23505_to_409() -> None:
    h = make_harness()
    h.store.fail_next("register_device", "23505", "devices_active_public_key_key")
    resp = _register(h, _body(h.new_user()))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "public_key_already_registered"


def test_token_hash_collision_retried_with_fresh_tokens() -> None:
    h = make_harness()
    h.store.fail_next("register_device", "23505", "devices_token_hash_key")
    assert _register(h, _body(h.new_user())).status_code == 201
    h.store.fail_next("register_device", "23505", "devices_token_hash_key", times=2)
    assert _register(h, _body(h.new_user())).status_code == 503


def test_register_without_pepper_503() -> None:
    h = make_harness(mcp_enabled=False, device_token_pepper=None)
    resp = _register(h, _body(h.new_user()))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "device_auth_not_configured"


def test_revoke_by_owner_kills_both_tokens_and_cancels_orders() -> None:
    h = make_harness()
    user = h.new_user()
    body = _register(h, _body(user)).json()
    created = h.call_tool(body["device_token"], "create_order",
                          {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 1}]})
    order_id = created.json()["result"]["content"][0]["text"]
    assert "order_id" in order_id
    resp = h.client.delete(f"/api/v1/devices/{body['device_id']}", headers={"Authorization": f"Bearer {h.jwt(user)}"})
    assert resp.status_code == 200
    assert resp.json() == {"device_id": body["device_id"], "status": "revoked"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert h.mcp(body["device_token"], init).status_code == 401
    assert h.mcp(body["llm_token"], init).status_code == 401
    assert [o["status"] for o in h.store.orders.values()] == ["CANCELLED"]
    assert h.store.audit_events("device.revoked")


def test_revoke_foreign_device_404_and_admin_allowed() -> None:
    h = make_harness()
    owner, stranger = h.new_user(), h.new_user()
    device_id = _register(h, _body(owner)).json()["device_id"]
    resp = h.client.delete(f"/api/v1/devices/{device_id}", headers={"Authorization": f"Bearer {h.jwt(stranger)}"})
    assert resp.status_code == 404
    assert h.client.delete("/api/v1/devices/not-a-uuid", headers=_admin()).status_code == 404
    assert h.client.delete(f"/api/v1/devices/{device_id.upper()}", headers=_admin()).status_code == 200
    # Idempotent
    assert h.client.delete(f"/api/v1/devices/{device_id}", headers=_admin()).status_code == 200
    assert h.client.delete(f"/api/v1/devices/{device_id}").status_code == 401


def test_list_devices_hides_secrets() -> None:
    h = make_harness()
    user, other = h.new_user(), h.new_user()
    _register(h, _body(user))
    _register(h, _body(other))
    resp = h.client.get("/api/v1/devices", headers={"Authorization": f"Bearer {h.jwt(user)}"})
    assert resp.status_code == 200
    devices = resp.json()["devices"]
    assert len(devices) == 1
    for forbidden in ("token_hash", "llm_token_hash", "public_key", "user_id"):
        assert forbidden not in devices[0]
    assert h.client.get("/api/v1/devices").status_code == 401
