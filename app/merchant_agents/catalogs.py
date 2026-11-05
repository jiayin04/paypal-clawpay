"""Deterministic demo catalogs for the mock merchant agents (MYR, sen).

Grocer and Electronics are the original demo shops. Shopee, Foodpanda and Grab
are demo agents modelled on those platforms (marketplace, food delivery,
rides/delivery); they are not connected to the real services.

``stock`` is the quantity a mock agent can reserve (None = unlimited). One item
per new merchant is sold out so the reserve-before-pay rollback can be demoed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    unit_price_minor: int
    tags: tuple[str, ...] = ()
    stock: int | None = None


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
    "shopee": {
        "name": "Shopee Demo",
        "description": "Demo marketplace merchant agent modelled on Shopee (deterministic catalog).",
        "products": (
            Product("SHP-PHONE-CASE", "Clear phone case 6.1in", 1590, ("phone", "case", "casing"), stock=50),
            Product("SHP-TUMBLER-500", "Insulated tumbler 500ml", 3290, ("tumbler", "bottle", "flask"), stock=30),
            Product("SHP-LED-STRIP", "LED strip light 5m", 2890, ("led", "light", "lamp"), stock=40),
            Product("SHP-TOTE-BAG", "Canvas tote bag", 1290, ("bag", "tote"), stock=100),
            # Sold out: reserve fails, so no payment is sent.
            Product("SHP-PLUSH-LTD", "Limited edition plush toy", 5900, ("plush", "toy"), stock=0),
        ),
    },
    "foodpanda": {
        "name": "Foodpanda Demo",
        "description": "Demo food delivery merchant agent modelled on foodpanda (deterministic menu).",
        "products": (
            Product("FPD-NASI-LEMAK", "Nasi lemak ayam goreng", 1290, ("nasi", "lemak", "rice", "chicken", "food"),
                    stock=40),
            Product("FPD-ROTI-CANAI", "Roti canai set with dhal", 650, ("roti", "canai", "breakfast", "food"),
                    stock=60),
            Product("FPD-CHAR-KUEY-TEOW", "Char kuey teow", 1450, ("noodles", "kuey", "teow", "food"), stock=40),
            Product("FPD-TEH-TARIK", "Iced teh tarik", 450, ("teh", "tea", "drink"), stock=80),
            # Sold out: reserve fails, so no payment is sent.
            Product("FPD-DURIAN-CREPE", "Musang King durian crepe", 2890, ("durian", "dessert", "crepe"), stock=0),
        ),
    },
    "grab": {
        "name": "Grab Demo",
        "description": "Demo rides and delivery merchant agent modelled on Grab (fixed demo fares).",
        "products": (
            Product("GRB-RIDE-KLCC-KLS", "GrabCar ride KLCC to KL Sentral", 1800,
                    ("ride", "car", "taxi", "klcc", "sentral"), stock=20),
            Product("GRB-RIDE-KLS-KLIA", "GrabCar ride KL Sentral to KLIA", 8500,
                    ("ride", "car", "taxi", "airport", "klia"), stock=20),
            Product("GRB-FOOD-BURGER", "GrabFood chicken burger combo", 1590, ("burger", "chicken", "food"),
                    stock=40),
            Product("GRB-EXPRESS-DOC", "GrabExpress document delivery 10km", 1500,
                    ("delivery", "express", "parcel", "courier"), stock=20),
            # Sold out: reserve fails, so no payment is sent.
            Product("GRB-RIDE-PREMIUM", "GrabCar Premium ride KLCC to Genting", 9800,
                    ("ride", "premium", "genting"), stock=0),
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
