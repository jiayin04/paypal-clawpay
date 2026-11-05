"""Unit tests for gadget approval and denial flows (§3.3, AC3.2, AC3.3).

Covers:
- ECDSA P-256 signature verification over canonical payload bytes.
- 8 pre-checks: device mismatch (403), malformed signature (400),
  missing signature (400), garbage signature on denial (200),
  key_version mismatch (409), nonce mismatch (409), challenge consumed replay (409),
  challenge expired (410), quote expired (410), challenge not found (404).
- RPC error mapping: phone_approval_required (409) at 10001 vs 10000 ok,
  daily_limit_exceeded (409) at 50001 vs 50000 ok, reopen with same challenge.
- no_payment_method (409).
- Concurrency: two approvals for one challenge, concurrent cap limit,
  revoke vs approve race.
"""

from __future__ import annotations

from uuid import uuid4

import anyio

from tests.fakes.app import make_harness
from tests.fakes.approvals import generate_p256 as _generate_p256, create_order as _create_order, get_challenge as _get_challenge, sign_canonical as _sign_canonical

MILK2 = {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 2}]}






# ---------------------------------------------------------------------------
# Basic Success Flows
# ---------------------------------------------------------------------------


def test_valid_gadget_approval_ecdsa_p256_success() -> None:
    """Valid ECDSA P-256 signature transitions order to PAYMENT_PENDING."""
    priv_key, pub_pem = _generate_p256()
    h = make_harness()
    token, device_id, user_id = h.new_device(public_key=pub_pem, key_version=1)
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    sig = _sign_canonical(priv_key, ch["canonical"])
    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "approved",
        "signature": sig,
    }

    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["order_id"] == order_id
    assert data["approval"] == "verified"
    assert data["status"] == "payment_pending"
    assert "Approved" in data["message"]

    # Verify store state
    assert h.store.orders[order_id]["status"] == "PAYMENT_PENDING"
    assert len(h.store.approvals) == 1
    assert h.store.approvals[0]["verification_status"] == "verified"
    assert len(h.store.mandates) == 1
    assert len(h.store.payment_attempts) == 1
    attempt = list(h.store.payment_attempts.values())[0]
    assert attempt["status"] == "initiated"
    assert attempt["claimed_until"] is not None


def test_valid_gadget_denial_success() -> None:
    """Denial without signature transitions order to DENIED."""
    h = make_harness()
    token, device_id, user_id = h.new_device()
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "denied",
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["approval"] == "denied"
    assert data["status"] == "denied"
    assert h.store.orders[order_id]["status"] == "DENIED"


def test_denial_with_garbage_signature_succeeds() -> None:
    """Denial with an arbitrary signature string does not attempt verification and succeeds."""
    h = make_harness()
    token, device_id, user_id = h.new_device()
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "denied",
        "signature": "garbage_signature_that_is_never_checked",
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["approval"] == "denied"
    assert h.store.orders[order_id]["status"] == "DENIED"


# ---------------------------------------------------------------------------
# Signature Verification Failures
# ---------------------------------------------------------------------------


def test_signature_over_different_bytes_returns_422() -> None:
    """Signature over tampered payload bytes fails verification -> 422 signature_invalid."""
    priv_key, pub_pem = _generate_p256()
    h = make_harness()
    token, device_id, _ = h.new_device(public_key=pub_pem)
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    # Sign wrong content
    sig = _sign_canonical(priv_key, "TAMPERED PAYLOAD")
    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "approved",
        "signature": sig,
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "signature_invalid"


def test_signature_from_wrong_private_key_returns_422() -> None:
    """Signature from a different private key fails verification -> 422 signature_invalid."""
    _, pub_pem = _generate_p256()
    wrong_key, _ = _generate_p256()
    h = make_harness()
    token, device_id, _ = h.new_device(public_key=pub_pem)
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    sig = _sign_canonical(wrong_key, ch["canonical"])
    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "approved",
        "signature": sig,
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "signature_invalid"


def test_malformed_signature_returns_400() -> None:
    """Corrupted base64 or invalid DER signature returns 400 malformed_signature."""
    priv_key, pub_pem = _generate_p256()
    h = make_harness()
    token, device_id, _ = h.new_device(public_key=pub_pem)
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    # 70 bytes of all 'A's -> valid length regex, but not valid ASN.1 DER DSS
    bad_sig = "A" * 70
    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "approved",
        "signature": bad_sig,
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "malformed_signature"


def test_approved_missing_signature_returns_400() -> None:
    """Approved decision without a signature field returns 400 malformed_signature."""
    h = make_harness()
    token, device_id, _ = h.new_device()
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "approved",
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "malformed_signature"


# ---------------------------------------------------------------------------
# Pre-checks and RPC error codes
# ---------------------------------------------------------------------------


def test_device_mismatch_returns_403() -> None:
    """Body device_id differing from token authenticated device returns 403 device_mismatch."""
    h = make_harness()
    token, _, _ = h.new_device()
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": str(uuid4()),
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "denied",
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "device_mismatch"


def test_key_version_mismatch_returns_409() -> None:
    """key_version mismatch in body returns 409 key_version_mismatch."""
    priv_key, pub_pem = _generate_p256()
    h = make_harness()
    token, device_id, _ = h.new_device(public_key=pub_pem, key_version=1)
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": 99,  # Mismatch
        "nonce": ch["nonce"],
        "decision": "denied",
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "key_version_mismatch"


def test_nonce_mismatch_returns_409() -> None:
    """Nonce mismatch returns 409 nonce_mismatch."""
    h = make_harness()
    token, device_id, _ = h.new_device()
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": "wrong-nonce-" + "x" * 31,
        "decision": "denied",
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "nonce_mismatch"


def test_challenge_consumed_replay_returns_409() -> None:
    """Replaying an already consumed challenge returns 409 challenge_consumed."""
    priv_key, pub_pem = _generate_p256()
    h = make_harness()
    token, device_id, _ = h.new_device(public_key=pub_pem)
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    sig = _sign_canonical(priv_key, ch["canonical"])
    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "approved",
        "signature": sig,
    }
    r1 = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 200

    # Replay
    r2 = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "challenge_consumed"


def test_challenge_expired_returns_410() -> None:
    """Submitting approval after challenge expires returns 410 challenge_expired."""
    h = make_harness()
    token, device_id, _ = h.new_device()
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    # Advance clock past challenge expiry (120s default)
    h.clock.advance(130)

    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "denied",
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "challenge_expired"


def test_quote_expired_returns_410() -> None:
    """Submitting approval after quote expires returns 410 quote_expired."""
    priv_key, pub_pem = _generate_p256()
    h = make_harness()
    token, device_id, _ = h.new_device(public_key=pub_pem)
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)

    # In store, make quote expired (e.g. quote_expires_at <= now)
    h.store.orders[order_id]["quote_expires_at"] = h.store.now()
    sig = _sign_canonical(priv_key, ch["canonical"])
    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "approved",
        "signature": sig,
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "quote_expired"


def test_challenge_not_found_returns_404() -> None:
    """Non-existent challenge ID returns 404 challenge_not_found."""
    h = make_harness()
    token, device_id, _ = h.new_device()
    order_id = _create_order(h, token)

    body = {
        "challenge_id": str(uuid4()),
        "device_id": device_id,
        "key_version": 1,
        "nonce": "x" * 43,
        "decision": "denied",
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "challenge_not_found"


# ---------------------------------------------------------------------------
# Policy limits: Phone Threshold & Daily Cap (AC3.2, AC3.3)
# ---------------------------------------------------------------------------


def test_phone_threshold_boundary_ac3_2() -> None:
    """10000 minor passes phone threshold; 10001 minor returns 409 phone_approval_required."""
    priv_key, pub_pem = _generate_p256()
    h = make_harness(phone_approval_threshold_minor=10000)
    token, device_id, _ = h.new_device(public_key=pub_pem)

    # 1. 10000 minor passes
    order1 = _create_order(h, token)
    h.store.orders[order1]["total_minor"] = 10000
    ch1 = _get_challenge(h, token, order1)
    sig1 = _sign_canonical(priv_key, ch1["canonical"])
    r1 = h.client.post(
        f"/api/v1/orders/{order1}/gadget-approval",
        json={
            "challenge_id": ch1["challenge_id"],
            "device_id": device_id,
            "key_version": ch1["key_version"],
            "nonce": ch1["nonce"],
            "decision": "approved",
            "signature": sig1,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["approval"] == "verified"

    # 2. 10001 minor fails phone threshold
    order2 = _create_order(h, token)
    h.store.orders[order2]["total_minor"] = 10001
    ch2 = _get_challenge(h, token, order2)
    sig2 = _sign_canonical(priv_key, ch2["canonical"])
    r2 = h.client.post(
        f"/api/v1/orders/{order2}/gadget-approval",
        json={
            "challenge_id": ch2["challenge_id"],
            "device_id": device_id,
            "key_version": ch2["key_version"],
            "nonce": ch2["nonce"],
            "decision": "approved",
            "signature": sig2,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "phone_approval_required"


def test_daily_spending_cap_and_reopen_ac3_3() -> None:
    """50000 accepted; 50001 returns 409 daily_limit_exceeded with challenge remaining open.

    When capacity frees, submitting the same challenge succeeds.
    """
    priv_key, pub_pem = _generate_p256()
    h = make_harness(daily_spending_limit_minor=50000)
    token, device_id, user_id = h.new_device(public_key=pub_pem)

    # Create order first (total = 1000 minor)
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)
    sig = _sign_canonical(priv_key, ch["canonical"])
    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "approved",
        "signature": sig,
    }

    # Order total is 1380 minor. Set existing spending so committed + total = 48621 + 1380 = 50001 > 50000
    blocking_record = {
        "user_id": user_id,
        "order_id": str(uuid4()),
        "amount_minor": 48621,
        "currency": "MYR",
        "status": "committed",
        "spending_day": h.store.now().date(),
    }
    h.store.spending_records.append(blocking_record)

    # 1. Approval fails with 409 daily_limit_exceeded
    r1 = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 409
    assert r1.json()["error"]["code"] == "daily_limit_exceeded"

    # Challenge is still open (unconsumed)
    assert h.store.approval_challenges[ch["challenge_id"]]["consumed_at"] is None

    # 2. Release/free capacity (e.g. reduce blocking record to 48620, sum = 50000)
    blocking_record["amount_minor"] = 48620

    # 3. Re-submit same challenge -> now succeeds!
    r2 = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["approval"] == "verified"


def test_no_active_payment_method_returns_409() -> None:
    """Approval without an active payment method returns 409 no_payment_method."""
    priv_key, pub_pem = _generate_p256()
    h = make_harness(order_require_payment_method=False)
    # User without payment method
    user_id = h.store.add_user()
    token, device_id, _ = h.new_device(user_id=user_id, public_key=pub_pem)
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)
    sig = _sign_canonical(priv_key, ch["canonical"])

    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "approved",
        "signature": sig,
    }
    r = h.client.post(
        f"/api/v1/orders/{order_id}/gadget-approval",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_payment_method"
    # Challenge remains unconsumed
    assert h.store.approval_challenges[ch["challenge_id"]]["consumed_at"] is None


# ---------------------------------------------------------------------------
# Concurrency Tests (AC3.2, AC3.3)
# ---------------------------------------------------------------------------


def test_concurrent_approvals_single_challenge_race() -> None:
    """Two concurrent approvals racing on the same challenge: exactly one succeeds."""
    priv_key, pub_pem = _generate_p256()
    h = make_harness()
    token, device_id, _ = h.new_device(public_key=pub_pem)
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)
    sig = _sign_canonical(priv_key, ch["canonical"])
    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "approved",
        "signature": sig,
    }

    results: list[int] = []

    async def _submit() -> None:
        async with anyio.create_task_group() as tg:
            async def _call() -> None:
                # Use in-process client
                r = h.client.post(
                    f"/api/v1/orders/{order_id}/gadget-approval",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
                results.append(r.status_code)

            tg.start_soon(_call)
            tg.start_soon(_call)

    anyio.run(_submit)

    assert len(results) == 2
    assert 200 in results
    assert 409 in results  # second gets challenge_consumed


def test_concurrent_orders_exceeding_cap_race_ac3_3() -> None:
    """Two orders that together exceed daily spending limit: exactly one succeeds."""
    priv_key, pub_pem = _generate_p256()
    # Totals are forced below without changing the items; a merchant agent would
    # (correctly) refuse to reserve such an order, so the handoff is off here.
    h = make_harness(daily_spending_limit_minor=50000, phone_approval_threshold_minor=50000,
                     merchant_fulfillment_enabled=False)
    token, device_id, user_id = h.new_device(public_key=pub_pem)

    # Order 1 (30000 minor)
    order1 = _create_order(h, token)
    h.store.orders[order1]["total_minor"] = 30000
    ch1 = _get_challenge(h, token, order1)
    sig1 = _sign_canonical(priv_key, ch1["canonical"])
    body1 = {
        "challenge_id": ch1["challenge_id"],
        "device_id": device_id,
        "key_version": ch1["key_version"],
        "nonce": ch1["nonce"],
        "decision": "approved",
        "signature": sig1,
    }

    # Order 2 (30000 minor)
    order2 = _create_order(h, token)
    h.store.orders[order2]["total_minor"] = 30000
    ch2 = _get_challenge(h, token, order2)
    sig2 = _sign_canonical(priv_key, ch2["canonical"])
    body2 = {
        "challenge_id": ch2["challenge_id"],
        "device_id": device_id,
        "key_version": ch2["key_version"],
        "nonce": ch2["nonce"],
        "decision": "approved",
        "signature": sig2,
    }

    results: list[int] = []

    async def _race() -> None:
        async with anyio.create_task_group() as tg:
            async def _c1() -> None:
                r = h.client.post(
                    f"/api/v1/orders/{order1}/gadget-approval",
                    json=body1,
                    headers={"Authorization": f"Bearer {token}"},
                )
                results.append(r.status_code)

            async def _c2() -> None:
                r = h.client.post(
                    f"/api/v1/orders/{order2}/gadget-approval",
                    json=body2,
                    headers={"Authorization": f"Bearer {token}"},
                )
                results.append(r.status_code)

            tg.start_soon(_c1)
            tg.start_soon(_c2)

    anyio.run(_race)

    assert len(results) == 2
    assert sorted(results) == [200, 409]

    # Total spending reserved in store does not exceed 50000
    total_reserved = sum(
        r["amount_minor"]
        for r in h.store.spending_records
        if r["user_id"] == user_id and r["status"] in ("committed", "reserved")
    )
    assert total_reserved == 30000


def test_concurrent_revoke_vs_approve_race() -> None:
    """Revoking the device while an approval is submitted: exactly one wins, never inconsistent."""
    priv_key, pub_pem = _generate_p256()
    h = make_harness()
    token, device_id, user_id = h.new_device(public_key=pub_pem)
    order_id = _create_order(h, token)
    ch = _get_challenge(h, token, order_id)
    sig = _sign_canonical(priv_key, ch["canonical"])
    body = {
        "challenge_id": ch["challenge_id"],
        "device_id": device_id,
        "key_version": ch["key_version"],
        "nonce": ch["nonce"],
        "decision": "approved",
        "signature": sig,
    }

    admin_jwt = h.jwt(user_id)
    approval_status: list[int] = []
    revoke_status: list[int] = []

    async def _race() -> None:
        async with anyio.create_task_group() as tg:
            async def _approve() -> None:
                r = h.client.post(
                    f"/api/v1/orders/{order_id}/gadget-approval",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
                approval_status.append(r.status_code)

            async def _revoke() -> None:
                r = h.client.delete(
                    f"/api/v1/devices/{device_id}",
                    headers={"Authorization": f"Bearer {admin_jwt}"},
                )
                revoke_status.append(r.status_code)

            tg.start_soon(_approve)
            tg.start_soon(_revoke)

    anyio.run(_race)

    # Revocation always returns 200
    assert revoke_status == [200]
    # Approval either succeeds (200) before revocation, or fails (401/403)
    assert approval_status[0] in (200, 401, 403)


def test_signature_failure_is_persisted_in_audit():
    priv,pem=_generate_p256()
    h=make_harness(); token,did,uid=h.new_device(public_key=pem)
    oid=_create_order(h,token); ch=_get_challenge(h,token,oid)
    bad,_=_generate_p256()
    r=h.client.post(f'/api/v1/orders/{oid}/gadget-approval',headers={'Authorization':'Bearer '+token},json={
        'challenge_id':ch['challenge_id'],'device_id':did,'key_version':1,'nonce':ch['nonce'],
        'decision':'approved','signature':_sign_canonical(bad,ch['canonical'])})
    assert r.status_code==422
    assert h.store.audit_events('approval.signature_failed')
