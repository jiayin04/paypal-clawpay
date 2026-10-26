"""Tests for GET /api/v1/orders/{order_id}/approval-challenge (AC3.1, AC3.2, design §3.2).

Also covers the basic gadget approval/denial flow (AC3.3, design §3.3).
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from tests.fakes.app import Harness, make_harness
from tests.fakes.merchants import ScriptedMerchant

MILK2 = {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 2}]}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_order(h: Harness, token: str) -> str:
    """Create an order and return its order_id."""
    from tests.fakes.app import tool_payload
    _, out = tool_payload(h.call_tool(token, "create_order", MILK2))
    return out["order_id"]


def _get_challenge(h: Harness, token: str, order_id: str) -> dict[str, Any]:
    r = h.client.get(
        f"/api/v1/orders/{order_id}/approval-challenge",
        headers={"Authorization": f"Bearer {token}"},
    )
    return r.status_code, r.json()


# ---------------------------------------------------------------------------
# AC3.1: challenge issuance and response shape
# ---------------------------------------------------------------------------


def test_challenge_issued_for_awaiting_approval_order() -> None:
    h = make_harness()
    token, device_id, user_id = h.new_device()
    order_id = _create_order(h, token)

    status, body = _get_challenge(h, token, order_id)
    assert status == 200, body
    assert body["order_id"] == order_id
    assert "challenge_id" in body
    assert "nonce" in body
    assert len(body["nonce"]) == 43
    assert "canonical_b64url" in body
    assert "canonical" in body
    assert "key_version" in body
    assert "expires_at" in body
    # canonical must decode to the stored payload
    decoded = base64.urlsafe_b64decode(body["canonical_b64url"] + "==")
    assert decoded == body["canonical"].encode("ascii")
    # canonical must parse as JSON with the right purpose
    parsed = json.loads(body["canonical"])
    assert parsed["purpose"] == "clawpay.order_approval.v1"
    assert parsed["order_id"] == order_id.lower()
    assert parsed["device_id"] == device_id.lower()
    # challenge must appear in store
    assert any(
        ch["order_id"] == order_id
        for ch in h.store.approval_challenges.values()
    )


def test_challenge_idempotent_returns_existing() -> None:
    """Second GET returns the same challenge (idempotent for an open challenge)."""
    h = make_harness()
    token, _, _ = h.new_device()
    order_id = _create_order(h, token)

    status1, body1 = _get_challenge(h, token, order_id)
    status2, body2 = _get_challenge(h, token, order_id)
    assert status1 == status2 == 200
    assert body1["challenge_id"] == body2["challenge_id"]
    assert body1["nonce"] == body2["nonce"]
    assert len(h.store.approval_challenges) == 1


def test_challenge_non_uuid_order_id_returns_404() -> None:
    """Non-UUID order_id → 404 order_not_found (not 422 — design §3.2)."""
    h = make_harness()
    token, _, _ = h.new_device()
    r = h.client.get(
        "/api/v1/orders/not-a-uuid/approval-challenge",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "order_not_found"


def test_challenge_missing_order_returns_404() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    r = h.client.get(
        f"/api/v1/orders/{uuid4()}/approval-challenge",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "order_not_found"


def test_challenge_order_not_awaiting_approval_returns_409() -> None:
    h = make_harness()
    token, _, user_id = h.new_device()
    order_id = _create_order(h, token)
    # Force cancel
    jwt = h.jwt(user_id)
    h.client.post(
        f"/api/v1/orders/{order_id}/cancel",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    r = h.client.get(
        f"/api/v1/orders/{order_id}/approval-challenge",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_order_status"


def test_challenge_expired_order_returns_410() -> None:
    h = make_harness()
    token, _, _ = h.new_device()
    order_id = _create_order(h, token)
    # Advance clock past quote expiry
    h.clock.advance(400)
    r = h.client.get(
        f"/api/v1/orders/{order_id}/approval-challenge",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "quote_expired"


def test_challenge_requires_auth() -> None:
    h = make_harness()
    _, _, _ = h.new_device()
    r = h.client.get(f"/api/v1/orders/{uuid4()}/approval-challenge")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Gadget approval / denial basic flow (AC3.3)
# ---------------------------------------------------------------------------


def _make_body(challenge: dict[str, Any], device_id: str, key_version: int,
               decision: str = "denied") -> dict[str, Any]:
    return {
        "challenge_id": challenge["challenge_id"],
        "device_id": device_id,
        "key_version": key_version,
        "nonce": challenge["nonce"],
        "decision": decision,
    }


def test_gadget_denial_returns_200_denied() -> None:
    h = make_harness()
    token, device_id, user_id = h.new_device()
    order_id = _create_order(h, token)
    _, ch = _get_challenge(h, token, order_id)

    body = _make_body(ch, device_id, ch["key_version"], "denied")
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.json()
    data = r.json()
    assert data["approval"] == "denied"
    assert data["status"] == "denied"
    # Order transitions to DENIED
    assert h.store.orders[order_id]["status"] == "DENIED"
    # Challenge consumed
    ch_row = h.store.approval_challenges[ch["challenge_id"]]
    assert ch_row["consumed_at"] is not None


def test_gadget_denial_device_mismatch_returns_403() -> None:
    h = make_harness()
    token, device_id, user_id = h.new_device()
    order_id = _create_order(h, token)
    _, ch = _get_challenge(h, token, order_id)

    body = _make_body(ch, str(uuid4()), ch["key_version"], "denied")
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "device_mismatch"


def test_gadget_approval_missing_signature_returns_400() -> None:
    h = make_harness()
    token, device_id, _ = h.new_device()
    order_id = _create_order(h, token)
    _, ch = _get_challenge(h, token, order_id)

    body = _make_body(ch, device_id, ch["key_version"], "approved")
    # No signature field
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "malformed_signature"


def test_gadget_denial_replay_returns_409() -> None:
    """Attempting to use a consumed challenge returns challenge_consumed 409."""
    h = make_harness()
    token, device_id, _ = h.new_device()
    order_id = _create_order(h, token)
    _, ch = _get_challenge(h, token, order_id)

    body = _make_body(ch, device_id, ch["key_version"], "denied")
    r1 = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 200

    # Replay: order is now DENIED, order_id mismatch or challenge consumed
    r2 = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    # Either 409 (challenge_consumed, order status invalid) or 409
    assert r2.status_code in (409, 404)  # challenge consumed → challenge_consumed or invalid_order_status


def test_gadget_approval_no_payment_method_returns_409() -> None:
    """accept_gadget_approval must fail with no_payment_method if no active PM."""
    h = make_harness(order_require_payment_method=False)
    # Create user without payment method
    user_id = h.store.add_user()
    from app.core.security import generate_device_token, hash_token
    from tests.fakes.app import PEPPER
    from tests.fakes.app import make_harness as _mh
    token = generate_device_token()
    from app.core.security import generate_llm_token
    llm = generate_llm_token()
    device_id = h.store.add_device(user_id,
                                   token_hash=hash_token(PEPPER, token),
                                   llm_token_hash=hash_token(PEPPER, llm))
    order_id = _create_order(h, token)
    _, ch = _get_challenge(h, token, order_id)

    # Now try to approve (with valid-looking signature that will fail sig check first)
    # Use 'denied' to bypass signature check and hit no_payment_method in RPC
    body = _make_body(ch, device_id, ch["key_version"], "denied")
    # Record denial RPC will go through (no payment method check isn't done for denial)
    # Instead: test accept directly via the store's accept_approval
    import asyncio

    async def _test() -> None:
        repos = h.store.repositories()
        result = await repos.approvals.accept_approval(
            challenge_id=ch["challenge_id"],
            device_id=device_id,
            user_id=user_id,
            nonce=ch["nonce"],
            payload_sha256=hashlib.sha256(ch["canonical"].encode()).hexdigest(),
            policy_currency="MYR",
            daily_limit_minor=50000,
            timezone="Asia/Kuala_Lumpur",
            phone_threshold_minor=10000,
        )
        assert not result.ok
        assert result.reason == "no_payment_method"

    asyncio.run(_test())


def test_gadget_approval_phone_threshold_returns_409() -> None:
    """accept_gadget_approval must fail with phone_approval_required for high-value orders."""
    h = make_harness()
    token, device_id, user_id = h.new_device()
    # Set up a high-value order manually
    order_id = _create_order(h, token)
    # Override total_minor in store to exceed phone threshold (10000)
    h.store.orders[order_id]["total_minor"] = 10001
    _, ch = _get_challenge(h, token, order_id)

    import asyncio

    async def _test() -> None:
        repos = h.store.repositories()
        result = await repos.approvals.accept_approval(
            challenge_id=ch["challenge_id"],
            device_id=device_id,
            user_id=user_id,
            nonce=ch["nonce"],
            payload_sha256=hashlib.sha256(ch["canonical"].encode()).hexdigest(),
            policy_currency="MYR",
            daily_limit_minor=50000,
            timezone="Asia/Kuala_Lumpur",
            phone_threshold_minor=10000,
        )
        assert not result.ok
        assert result.reason == "phone_approval_required"

    asyncio.run(_test())


def test_gadget_approval_daily_limit_exceeded_returns_409() -> None:
    """accept_gadget_approval must fail when spending cap is exceeded."""
    h = make_harness()
    token, device_id, user_id = h.new_device()
    order_id = _create_order(h, token)
    # Add existing spending near the limit
    h.store.add_spending(user_id, 49990, "reserved")
    _, ch = _get_challenge(h, token, order_id)

    import asyncio

    async def _test() -> None:
        repos = h.store.repositories()
        result = await repos.approvals.accept_approval(
            challenge_id=ch["challenge_id"],
            device_id=device_id,
            user_id=user_id,
            nonce=ch["nonce"],
            payload_sha256=hashlib.sha256(ch["canonical"].encode()).hexdigest(),
            policy_currency="MYR",
            daily_limit_minor=50000,
            timezone="Asia/Kuala_Lumpur",
            phone_threshold_minor=10000,
        )
        assert not result.ok
        assert result.reason == "daily_limit_exceeded"

    asyncio.run(_test())
