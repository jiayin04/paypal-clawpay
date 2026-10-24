"""Strict argument models for the four MCP tools (schemas in contracts.md §4).

Strict mode: ``"5"`` is not an integer. UUIDs are pattern-checked strings
normalised to lowercase (strict mode would reject a JSON string for ``UUID``).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.devices import UuidStr

MerchantCode = Literal["grocer", "electronics"]


class _ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchProductsArgs(_ToolArgs):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    max_results: Annotated[int, Field(ge=1, le=10)] = 5
    merchant_id: MerchantCode | None = None


class OrderLineArgs(_ToolArgs):
    sku: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    qty: Annotated[int, Field(ge=1, le=20)]


class CreateOrderArgs(_ToolArgs):
    merchant_id: MerchantCode
    items: Annotated[list[OrderLineArgs], Field(min_length=1, max_length=10)]
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None


class GetOrderStatusArgs(_ToolArgs):
    order_id: UuidStr


class GetSpendingSummaryArgs(_ToolArgs):
    period: Literal["today"] = "today"
