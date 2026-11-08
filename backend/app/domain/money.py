"""Money helpers. Amounts are integers in minor units (sen for MYR)."""

from __future__ import annotations

# ISO 4217 exponents for the currencies this build supports. The device ships
# the same table and refuses to sign any other currency.
EXPONENTS: dict[str, int] = {"MYR": 2, "USD": 2}

_SYMBOLS: dict[str, str] = {"MYR": "RM", "USD": "US$"}


def _split(amount_minor: int, currency: str) -> tuple[str, int, int]:
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError("amount_minor must be an int")
    try:
        exponent = EXPONENTS[currency]
    except KeyError as exc:
        raise ValueError(f"unsupported currency: {currency}") from exc
    sign = "-" if amount_minor < 0 else ""
    major, minor = divmod(abs(amount_minor), 10**exponent)
    return sign, major, minor


def to_paypal_value(amount_minor: int, currency: str) -> str:
    """PayPal decimal string, e.g. ``to_paypal_value(1380, "MYR") == "13.80"``."""
    sign, major, minor = _split(amount_minor, currency)
    exponent = EXPONENTS[currency]
    if exponent == 0:
        return f"{sign}{major}"
    return f"{sign}{major}.{minor:0{exponent}d}"


def format_display(amount_minor: int, currency: str) -> str:
    """Display string for the LLM, e.g. ``"RM13.80"`` or ``"US$5.00"``."""
    value = to_paypal_value(amount_minor, currency)
    sign = ""
    if value.startswith("-"):
        sign, value = "-", value[1:]
    return f"{sign}{_SYMBOLS.get(currency, currency + ' ')}{value}"
