"""Device approval test helpers; software keys never leave memory."""
from __future__ import annotations

import base64
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tests.fakes.app import Harness, tool_payload


MILK2 = {"merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 2}]}

def generate_p256() -> tuple[ec.EllipticCurvePrivateKey, str]:
    """Generate in-memory P-256 key pair and return (private_key, public_pem)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return priv, pub_pem


def create_order(h: Harness, token: str, payload: dict[str, Any] = MILK2) -> str:
    """Create an order via MCP tool and return its order_id."""
    _, out = tool_payload(h.call_tool(token, "create_order", payload))
    return out["order_id"]


def get_challenge(h: Harness, token: str, order_id: str) -> dict[str, Any]:
    """GET /api/v1/orders/{order_id}/approval-challenge."""
    r = h.client.get(
        f"/api/v1/orders/{order_id}/approval-challenge",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def sign_canonical(priv_key: ec.EllipticCurvePrivateKey, canonical_text: str) -> str:
    """Sign canonical_text with ECDSA P-256 SHA-256 and return base64url-DER without padding."""
    der = priv_key.sign(canonical_text.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    return base64.urlsafe_b64encode(der).rstrip(b"=").decode("ascii")
