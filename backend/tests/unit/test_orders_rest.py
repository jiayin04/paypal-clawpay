"""Dashboard order REST (design §2.7, AC2.10)."""

from typing import Any

from tests.fakes.app import Harness, make_harness, tool_payload

MILK = {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 1}]}
FORBIDDEN_KEYS = {"token", "token_hash", "llm_token_hash", "vault_token_reference", "provider_customer_reference",
                  "payload", "nonce", "canonical", "public_key", "idempotency_key", "cart_sha256"}


def _keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


def _order(h: Harness) -> tuple[str, str, str]:
    token, _, user = h.new_device()
    _, out = tool_payload(h.call_tool(token, "create_order", MILK))
    return out["order_id"], user, token


def _auth(h: Harness, user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {h.jwt(user)}"}


def test_list_and_detail_user_scoped() -> None:
    h = make_harness()
    order_id, user, _ = _order(h)
    other_order, other_user, _ = _order(h)
    resp = h.client.get("/api/v1/orders", headers=_auth(h, user))
    assert resp.status_code == 200
    orders = resp.json()["orders"]
    assert [o["id"] for o in orders] == [order_id]
    assert orders[0]["status"] == "awaiting_device_approval"
    detail = h.client.get(f"/api/v1/orders/{order_id}", headers=_auth(h, user))
    assert detail.status_code == 200
    body = detail.json()
    assert body["merchant"] == "Grocer Demo" and body["items"][0]["product_reference"] == "GRC-MILK-1L"
    assert not (_keys(body) & FORBIDDEN_KEYS)
    assert not (_keys(resp.json()) & FORBIDDEN_KEYS)
    foreign = h.client.get(f"/api/v1/orders/{other_order}", headers=_auth(h, user))
    assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "order_not_found"
    assert h.client.get("/api/v1/orders/not-a-uuid", headers=_auth(h, user)).status_code == 404


def test_list_limit_validation() -> None:
    h = make_harness()
    user = h.new_user()
    assert h.client.get("/api/v1/orders?limit=0", headers=_auth(h, user)).status_code == 422
    assert h.client.get("/api/v1/orders?limit=101", headers=_auth(h, user)).status_code == 422
    assert h.client.get("/api/v1/orders?limit=100", headers=_auth(h, user)).status_code == 200


def test_cancel_flow() -> None:
    h = make_harness()
    order_id, user, _ = _order(h)
    other_order, _, _ = _order(h)
    resp = h.client.post(f"/api/v1/orders/{order_id}/cancel", headers=_auth(h, user))
    assert resp.status_code == 200 and resp.json() == {"order_id": order_id, "status": "cancelled"}
    again = h.client.post(f"/api/v1/orders/{order_id}/cancel", headers=_auth(h, user))
    assert again.status_code == 409 and again.json()["error"]["code"] == "invalid_order_status"
    assert h.client.post(f"/api/v1/orders/{other_order}/cancel", headers=_auth(h, user)).status_code == 404
    assert h.client.post("/api/v1/orders/xyz/cancel", headers=_auth(h, user)).status_code == 404
    assert h.store.audit_events("order.cancelled")


def test_cancel_after_payment_started_409() -> None:
    h = make_harness()
    order_id, user, _ = _order(h)
    h.store.set_order_status(order_id, "APPROVED")
    h.store.set_order_status(order_id, "PAYMENT_PENDING")
    resp = h.client.post(f"/api/v1/orders/{order_id}/cancel", headers=_auth(h, user))
    assert resp.status_code == 409


def test_cancel_works_without_publishable_key() -> None:
    h = make_harness(supabase_publishable_key=None)
    order_id, user, _ = _order(h)
    assert h.client.get("/api/v1/orders", headers=_auth(h, user)).status_code == 503
    assert h.client.post(f"/api/v1/orders/{order_id}/cancel", headers=_auth(h, user)).status_code == 200


def test_dashboard_not_configured_even_with_valid_jwt() -> None:
    h = make_harness(supabase_publishable_key=None)
    user = h.new_user()
    for path in ("/api/v1/orders", "/api/v1/devices", "/api/v1/orders/00000000-0000-0000-0000-000000000001"):
        resp = h.client.get(path, headers=_auth(h, user))
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "dashboard_not_configured"


def test_order_routes_require_jwt() -> None:
    h = make_harness()
    order_id, _, device_token = _order(h)
    for method, path in (("get", "/api/v1/orders"), ("get", f"/api/v1/orders/{order_id}"),
                         ("post", f"/api/v1/orders/{order_id}/cancel")):
        assert getattr(h.client, method)(path).status_code == 401
        # A device token is not a user JWT.
        assert getattr(h.client, method)(path, headers={"Authorization": f"Bearer {device_token}"}).status_code == 401


def test_repository_error_on_rest_is_503() -> None:
    h = make_harness(raise_server_exceptions=False)
    order_id, user, _ = _order(h)
    h.store.fail_next("cancel_order", "08006")
    resp = h.client.post(f"/api/v1/orders/{order_id}/cancel", headers=_auth(h, user))
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "service_unavailable"
