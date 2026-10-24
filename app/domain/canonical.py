"""Canonical JSON bytes, cart hash and merchant-name sanitiser (contracts.md §5).

The approval payload builder is added in Phase 3.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

_NAME_DISALLOWED = re.compile(r"[^A-Za-z0-9 .,&'()-]")
_SPACES = re.compile(r" {2,}")
MERCHANT_NAME_MAX = 40


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def cart_sha256(lines: Iterable[Mapping[str, Any]]) -> str:
    """SHA-256 over ``[{"qty","sku","unit_price_minor"}]`` sorted by sku."""
    items = sorted(
        (
            {"qty": int(line["qty"]), "sku": str(line["sku"]), "unit_price_minor": int(line["unit_price_minor"])}
            for line in lines
        ),
        key=lambda item: item["sku"],
    )
    return hashlib.sha256(canonical_json(items)).hexdigest()


def sanitize_merchant_name(name: str | None, fallback: str) -> str:
    """Printable ASCII subset only, so the signed bytes never contain JSON escapes."""
    cleaned = _NAME_DISALLOWED.sub("", name or "")
    cleaned = _SPACES.sub(" ", cleaned).strip()[:MERCHANT_NAME_MAX].strip()
    return cleaned or fallback
