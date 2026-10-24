import hashlib

import pytest

from app.domain.canonical import canonical_json, cart_sha256, sanitize_merchant_name


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
