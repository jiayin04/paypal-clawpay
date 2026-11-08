import pytest

from app.domain.money import EXPONENTS, format_display, to_paypal_value


def test_exponents() -> None:
    assert EXPONENTS == {"MYR": 2, "USD": 2}


@pytest.mark.parametrize(
    ("minor", "currency", "paypal", "display"),
    [
        (1380, "MYR", "13.80", "RM13.80"),
        (0, "MYR", "0.00", "RM0.00"),
        (5, "MYR", "0.05", "RM0.05"),
        (10000, "MYR", "100.00", "RM100.00"),
        (48620, "MYR", "486.20", "RM486.20"),
        (500, "USD", "5.00", "US$5.00"),
    ],
)
def test_formats(minor: int, currency: str, paypal: str, display: str) -> None:
    assert to_paypal_value(minor, currency) == paypal
    assert format_display(minor, currency) == display


def test_unsupported_currency_and_types() -> None:
    with pytest.raises(ValueError):
        to_paypal_value(100, "EUR")
    with pytest.raises(TypeError):
        to_paypal_value(13.8, "MYR")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        to_paypal_value(True, "MYR")  # type: ignore[arg-type]
