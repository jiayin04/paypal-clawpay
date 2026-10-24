"""A2A data contracts for merchant responses (untrusted input, strict types)."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
NAME_MAX = 80
MAX_UNIT_PRICE = 10_000_000


def _clean_name(value: str) -> str:
    cleaned = _CONTROL.sub("", value).strip()
    if not cleaned or len(cleaned) > NAME_MAX:
        raise ValueError(f"name must be 1-{NAME_MAX} chars")
    return cleaned


Sku = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9-]{1,64}$")]
ProductName = Annotated[str, AfterValidator(_clean_name)]
UnitPrice = Annotated[int, Field(ge=1, le=MAX_UNIT_PRICE)]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)


class SearchProduct(_Strict):
    sku: Sku
    name: ProductName
    unit_price_minor: UnitPrice
    currency: Currency


class SearchResponse(_Strict):
    type: Literal["clawpay.search_response.v1"]
    correlation_id: str
    merchant_id: str
    products: list[dict]


class QuoteLine(_Strict):
    sku: Sku
    name: ProductName
    qty: Annotated[int, Field(ge=1, le=1000)]
    unit_price_minor: UnitPrice
    line_total_minor: int


class QuoteResponse(_Strict):
    type: Literal["clawpay.quote_response.v1"]
    correlation_id: str
    merchant_id: str
    merchant_name: str = Field(max_length=200)
    quote_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    currency: Currency
    items: list[QuoteLine] = Field(min_length=1, max_length=50)
    subtotal_minor: int
    total_minor: int
    expires_at: str = Field(max_length=64)


class MerchantErrorResponse(_Strict):
    type: Literal["clawpay.error.v1"]
    correlation_id: str
    code: Literal["unknown_sku", "currency_not_supported", "bad_request"]
    sku: str | None = Field(None, max_length=64)
