import hashlib

import pytest

from app.domain.canonical import (
    build_approval_payload,
    canonical_json,
    cart_sha256,
    payload_sha256,
    sanitize_merchant_name,
)


def test_canonical_golden_bytes() -> None:
    payload = {
        "purpose": "clawpay.order_approval.v1",
        "amount_minor": 1380,
        "currency": "MYR",
        "merchant_name": "Grocer Demo",
        "nonce": "abc",
        "expires_at": 1792850700,
    }
    out = canonical_json(payload)
    assert out == (
        b'{"amount_minor":1380,"currency":"MYR","expires_at":1792850700,'
        b'"merchant_name":"Grocer Demo","nonce":"abc","purpose":"clawpay.order_approval.v1"}'
    )
    assert out.isascii() and b"\\" not in out


def test_canonical_escapes_non_ascii() -> None:
    assert canonical_json({"n": "caf\u00e9"}) == b'{"n":"caf\\u00e9"}'


def test_cart_hash_order_independent_and_golden() -> None:
    a = [{"sku": "B", "qty": 1, "unit_price_minor": 450}, {"sku": "A", "qty": 2, "unit_price_minor": 690}]
    b = list(reversed(a))
    expected = hashlib.sha256(
        b'[{"qty":2,"sku":"A","unit_price_minor":690},{"qty":1,"sku":"B","unit_price_minor":450}]'
    ).hexdigest()
    assert cart_sha256(a) == cart_sha256(b) == expected
    assert cart_sha256([{"sku": "A", "qty": 3, "unit_price_minor": 690}]) != expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('Grocer "Demo"', "Grocer Demo"),
        ("Back\\slash Shop", "Backslash Shop"),
        ("Caf\u00e9  \u2603  Mart", "Caf Mart"),
        ("A" * 50, "A" * 40),
        ("", "grocer"),
        ('"\\\u00e9', "grocer"),
        (None, "grocer"),
        ("Tom & Jerry's (KL) Pte. Ltd., Co-op", "Tom & Jerry's (KL) Pte. Ltd., Co-op"),
    ],
)
def test_sanitize_merchant_name(raw: str | None, expected: str) -> None:
    out = sanitize_merchant_name(raw, "grocer")
    assert out == expected
    assert canonical_json({"n": out}).count(b"\\") == 0


# ---------------------------------------------------------------------------
# Phase 3 golden tests: build_approval_payload and payload_sha256
# ---------------------------------------------------------------------------

_CART_SHA = hashlib.sha256(
    b'[{"qty":2,"sku":"GRC-MILK-1L","unit_price_minor":690}]'
).hexdigest()

_ORDER_ID = "12345678-1234-1234-1234-1234567890ab"
_DEVICE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_NONCE = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # 43 chars


def test_build_approval_payload_golden() -> None:
    """Fixed input → byte-exact expected output; no backslash; ASCII only."""
    out = build_approval_payload(
        amount_minor=1380,
        cart_sha256=_CART_SHA,
        currency="MYR",
        device_id=_DEVICE_ID,
        expires_at=1792850700,
        key_version=1,
        merchant_id="grocer",
        merchant_name="Grocer Demo",
        nonce=_NONCE,
        order_id=_ORDER_ID,
    )
    assert out.isascii(), "payload must be pure ASCII"
    assert b"\\" not in out, "no JSON escapes expected"
    # Keys must be sorted: amount_minor, cart_sha256, currency, device_id,
    # expires_at, key_version, merchant_id, merchant_name, nonce, order_id, purpose
    assert out.startswith(b'{"amount_minor":')
    assert b'"purpose":"clawpay.order_approval.v1"' in out
    assert b'"merchant_id":"grocer"' in out  # merchant_code, not UUID
    assert b'"key_version":1' in out
    # Re-parse and verify round-trip
    import json
    parsed = json.loads(out)
    assert parsed["purpose"] == "clawpay.order_approval.v1"
    assert parsed["amount_minor"] == 1380
    assert parsed["currency"] == "MYR"
    assert parsed["device_id"] == _DEVICE_ID.lower()
    assert parsed["order_id"] == _ORDER_ID.lower()
    assert parsed["nonce"] == _NONCE


def test_payload_sha256_golden() -> None:
    out = build_approval_payload(
        amount_minor=1380,
        cart_sha256=_CART_SHA,
        currency="MYR",
        device_id=_DEVICE_ID,
        expires_at=1792850700,
        key_version=1,
        merchant_id="grocer",
        merchant_name="Grocer Demo",
        nonce=_NONCE,
        order_id=_ORDER_ID,
    )
    digest = payload_sha256(out)
    assert digest == hashlib.sha256(out).hexdigest()
    assert len(digest) == 64  # SHA-256 hex


def test_build_approval_payload_merchant_name_sanitized() -> None:
    """Non-ASCII merchant name must be sanitized so no backslash in output."""
    out = build_approval_payload(
        amount_minor=100,
        cart_sha256="abc",
        currency="MYR",
        device_id=_DEVICE_ID,
        expires_at=1792850700,
        key_version=1,
        merchant_id="grocer",
        merchant_name="Caf\u00e9 Shop",
        nonce=_NONCE,
        order_id=_ORDER_ID,
    )
    assert out.isascii()
    assert b"\\" not in out
    import json
    parsed = json.loads(out)
    assert parsed["merchant_name"] == "Caf Shop"  # non-ASCII stripped

