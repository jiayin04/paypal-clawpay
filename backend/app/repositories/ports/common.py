"""Common persistence contracts; no driver dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RpcResult:
    ok: bool
    reason: str
    data: Mapping[str, Any] = field(default_factory=dict)
