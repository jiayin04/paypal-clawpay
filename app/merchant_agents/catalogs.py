"""Deterministic demo catalogs for the mock Grocer and Electronics agents (MYR, sen)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    unit_price_minor: int
    tags: tuple[str, ...] = ()


CATALOG_CURRENCY = "MYR"

CATALOGS: dict[str, dict[str, object]] = {
    "grocer": {
        "name": "Grocer Demo",
        "description": "Demo grocery merchant agent (deterministic catalog).",
        "products": (
            Product("GRC-MILK-1L", "Fresh milk 1L", 690, ("milk", "dairy")),
            Product("GRC-BREAD-WW", "Wholemeal bread", 450, ("bread", "bakery")),
            Product("GRC-EGGS-10", "Eggs 10-pack", 1290, ("eggs", "egg")),
            Product("GRC-RICE-5KG", "Rice 5 kg", 2690, ("rice",)),
            Product("GRC-COFFEE-250G", "Ground coffee 250g", 1890, ("coffee",)),
        ),
    },
    "electronics": {
        "name": "Electronics Demo",
        "description": "Demo electronics merchant agent (deterministic catalog).",
        "products": (
            Product("ELC-USBC-CABLE", "USB-C cable 1m", 2490, ("cable", "usb", "usb-c", "charger")),
            Product("ELC-POWERBANK", "Power bank 10000mAh", 8990, ("powerbank", "battery", "charger")),
            # Exactly RM100: the phone-approval boundary (allowed).
            Product("ELC-USBC-HUB", "USB-C hub 7-in-1", 10000, ("hub", "usb", "usb-c", "adapter")),
            # Above RM100: demo of phone_approval_unavailable.
            Product("ELC-EARBUDS", "Wireless earbuds", 12900, ("earbuds", "headphones", "audio")),
            Product("ELC-KEYBOARD", "Mechanical keyboard", 25900, ("keyboard",)),
        ),
    },
}


def products(merchant_code: str) -> tuple[Product, ...]:
    return CATALOGS[merchant_code]["products"]  # type: ignore[return-value]


def search(merchant_code: str, query: str, max_results: int) -> list[Product]:
    """Case-insensitive token match on name and tags."""
    tokens = [t for t in query.lower().replace(",", " ").split() if len(t) >= 2]
    hits: list[Product] = []
    for product in products(merchant_code):
        name = product.name.lower()
        if any(t in name or t in product.tags for t in tokens):
            hits.append(product)
    return hits[:max_results]
