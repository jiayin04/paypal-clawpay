"""Agent passports (lite): signed grants, enforcement at create_order, signed receipts."""

import base64
import json

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tests.fakes.app import Harness, make_harness, make_settings, tool_payload

SEED = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
MILK = {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 1}]}  # 690 minor


def harness(**overrides) -> Harness:
    return make_harness(agent_passport_signing_key=SEED, **overrides)


def auth(h: Harness, user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {h.jwt(user)}"}


def grant(h: Harness, user: str, device_id: str, **body):
    payload = {"device_id": device_id, "merchants": ["grocer"], "max_order_minor": 5000, **body}
    return h.client.post("/api/v1/delegations", json=payload, headers=auth(h, user))


def verify(h: Harness, document: str, signature: str) -> None:
    keys = h.client.get("/.well-known/agent-passport").json()["keys"]
    raw = base64.urlsafe_b64decode(keys[0]["public_key"] + "=")
    Ed25519PublicKey.from_public_bytes(raw).verify(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)), document.encode("ascii"))


def order(h: Harness, token: str, cart=MILK):
    return tool_payload(h.call_tool(token, "create_order", cart))


# -- issuing ---------------------------------------------------------------------------


def test_issue_signed_grant() -> None:
    h = harness()
    token, device_id, user = h.new_device()
    resp = grant(h, user, device_id, merchants=["grocer", "electronics"], expires_in_hours=24)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active" and body["merchants"] == ["electronics", "grocer"]
    doc = json.loads(body["document"])
    assert doc["purpose"] == "clawpay.delegation.v1" and doc["principal"] == user
    assert doc["agent"] == {"id": device_id, "type": "device"} and doc["delegation_id"] == body["id"]
    assert doc["scope"] == {"currency": "MYR", "max_order_minor": 5000, "merchants": ["electronics", "grocer"]}
    assert doc["expires_at"] - doc["issued_at"] == 24 * 3600
    verify(h, body["document"], body["signature"])
    with pytest.raises(InvalidSignature):
        verify(h, body["document"].replace("5000", "9999"), body["signature"])
    listed = h.client.get("/api/v1/delegations", headers=auth(h, user)).json()["delegations"]
    assert [d["id"] for d in listed] == [body["id"]] and listed[0]["status"] == "active"
    assert h.store.audit_events("delegation.created")


def test_issuer_key_document() -> None:
    h = harness()
    out = h.client.get("/.well-known/agent-passport").json()
    assert out["issuer"] == "clawpay" and len(out["keys"]) == 1
    key = out["keys"][0]
    assert key["algorithm"] == "Ed25519" and key["jwk"]["crv"] == "Ed25519" and len(key["key_id"]) == 16
    assert SEED not in json.dumps(out)


def test_grant_validation_and_ownership() -> None:
    h = harness()
    _, device_id, user = h.new_device()
    _, other_device, _ = h.new_device()
    assert grant(h, user, device_id, merchants=["nope"]).status_code == 422
    assert grant(h, user, device_id, max_order_minor=50001).status_code == 422
    assert grant(h, user, device_id, expires_in_hours=0).status_code == 422
    assert grant(h, user, other_device).status_code == 404
    assert h.client.post("/api/v1/delegations", json={}).status_code == 401
    assert grant(h, user, device_id).status_code == 201
    second = grant(h, user, device_id)
    assert second.status_code == 409 and second.json()["error"]["code"] == "delegation_exists"


def test_grant_needs_signing_key() -> None:
    h = make_harness()
    _, device_id, user = h.new_device()
    resp = grant(h, user, device_id)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "passport_not_configured"
    assert h.client.get("/.well-known/agent-passport").json()["keys"] == []


# -- enforcement -------------------------------------------------------------------------


def test_device_without_grant_orders_as_before() -> None:
    h = harness()
    token, _, _ = h.new_device()
    error, out = order(h, token)
    assert not error, out
    assert h.store.agent_receipts == []


def test_allowed_order_writes_signed_receipt() -> None:
    h = harness()
    token, device_id, user = h.new_device()
    delegation = grant(h, user, device_id).json()
    error, out = order(h, token)
    assert not error, out
    [receipt] = h.client.get("/api/v1/delegations/receipts", headers=auth(h, user)).json()["receipts"]
    assert receipt["decision"] == "allowed" and receipt["order_id"] == out["order_id"]
    assert receipt["delegation_id"] == delegation["id"] and receipt["amount_minor"] == 690
    doc = json.loads(receipt["document"])
    assert doc["purpose"] == "clawpay.receipt.v1" and doc["reason"] == "within_delegation"
    verify(h, receipt["document"], receipt["signature"])


def test_merchant_outside_scope_is_refused_before_quoting() -> None:
    h = harness()
    token, device_id, user = h.new_device()
    grant(h, user, device_id, merchants=["electronics"])
    error, out = order(h, token)
    assert error and out["error"] == "delegation_denied" and "does not allow ordering from that shop" in out["message"]
    assert h.store.orders == {} and h.store.a2a_messages == []
    [receipt] = h.store.agent_receipts
    assert receipt["decision"] == "denied" and receipt["reason"] == "merchant_not_delegated"
    assert receipt["order_id"] is None
    verify(h, receipt["document"], receipt["signature"])


def test_amount_over_grant_limit_is_refused() -> None:
    h = harness()
    token, device_id, user = h.new_device()
    grant(h, user, device_id, max_order_minor=500)
    error, out = order(h, token)
    assert error and out["error"] == "delegation_denied"
    assert h.store.orders == {}
    assert h.store.agent_receipts[-1]["reason"] == "delegation_limit_exceeded"
    assert h.store.agent_receipts[-1]["amount_minor"] == 690


def test_revoked_grant_blocks_orders() -> None:
    h = harness()
    token, device_id, user = h.new_device()
    _, _, stranger = h.new_device()
    delegation = grant(h, user, device_id).json()
    assert h.client.delete(f"/api/v1/delegations/{delegation['id']}", headers=auth(h, stranger)).status_code == 404
    resp = h.client.delete(f"/api/v1/delegations/{delegation['id']}", headers=auth(h, user))
    assert resp.status_code == 200 and resp.json() == {"id": delegation["id"], "status": "revoked"}
    assert h.client.delete(f"/api/v1/delegations/{delegation['id']}", headers=auth(h, user)).status_code == 200
    assert h.client.delete("/api/v1/delegations/not-a-uuid", headers=auth(h, user)).status_code == 404
    error, out = order(h, token)
    assert error and "revoked" in out["message"]
    assert h.store.agent_receipts[-1]["reason"] == "delegation_revoked"
    # A revoked device is still governed: a new grant restores ordering.
    assert grant(h, user, device_id).status_code == 201
    assert not order(h, token)[0]


def test_expired_grant_blocks_and_can_be_replaced() -> None:
    h = harness()
    token, device_id, user = h.new_device()
    grant(h, user, device_id, expires_in_hours=1)
    h.clock.advance(3601)
    error, out = order(h, token)
    assert error and "expired" in out["message"]
    listed = h.client.get("/api/v1/delegations", headers=auth(h, user)).json()["delegations"]
    assert listed[0]["status"] == "expired"
    assert grant(h, user, device_id).status_code == 201  # expired grant is revoked automatically


def test_required_mode_refuses_devices_without_grant() -> None:
    h = harness(agent_passport_required=True)
    token, _, _ = h.new_device()
    error, out = order(h, token)
    assert error and out["error"] == "delegation_denied" and "no agent passport" in out["message"]
    [receipt] = h.store.agent_receipts
    assert receipt["delegation_id"] is None and receipt["reason"] == "no_delegation"


def test_lookup_failure_fails_closed() -> None:
    h = harness()
    token, device_id, user = h.new_device()
    grant(h, user, device_id)
    h.store.fail_next("agent_delegations.latest_for_device", "XX000")
    error, out = order(h, token)
    assert error and out["error"] == "internal_error"
    assert h.store.orders == {}


def test_receipt_write_failure_does_not_block_order() -> None:
    h = harness()
    token, device_id, user = h.new_device()
    grant(h, user, device_id)
    h.store.fail_next("agent_receipts.insert", "XX000")
    error, out = order(h, token)
    assert not error and out["order_id"] in h.store.orders


def test_receipts_are_user_scoped() -> None:
    h = harness()
    token, device_id, user = h.new_device()
    _, _, other = h.new_device()
    grant(h, user, device_id)
    order(h, token)
    assert h.client.get("/api/v1/delegations/receipts", headers=auth(h, other)).json() == {"receipts": []}
    assert h.client.get("/api/v1/delegations", headers=auth(h, other)).json() == {"delegations": []}


def test_signing_key_must_be_32_bytes() -> None:
    with pytest.raises(ValueError, match="AGENT_PASSPORT_SIGNING_KEY"):
        make_settings(agent_passport_signing_key="dG9vLXNob3J0")
    with pytest.raises(ValueError, match="AGENT_PASSPORT_SIGNING_KEY"):
        make_settings(agent_passport_signing_key="!!!not base64!!!")
