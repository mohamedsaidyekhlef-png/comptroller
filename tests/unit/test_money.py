import pytest
from hypothesis import given
from hypothesis import strategies as st

from comptroller.core.money import CurrencyMismatch, Money, MoneyError

micros = st.integers(min_value=-(10**15), max_value=10**15)
money = st.builds(Money, micros, st.sampled_from(["USD", "EUR", "JPY"]))


def test_parse_is_exact() -> None:
    assert Money.parse("0.62", "USD").micros == 620_000
    assert Money.parse("35", "USD").micros == 35_000_000
    assert Money.parse("0.000001", "USD").micros == 1


def test_parse_rejects_excess_precision() -> None:
    with pytest.raises(MoneyError):
        Money.parse("0.0000001", "USD")


def test_parse_rejects_float() -> None:
    with pytest.raises(MoneyError):
        Money.parse(0.1, "USD")  # type: ignore[arg-type]


def test_rejects_bad_currency() -> None:
    for bad in ["usd", "US", "USDT", "12A", ""]:
        with pytest.raises(MoneyError):
            Money(0, bad)  # type: ignore[arg-type]


def test_rejects_bool_micros() -> None:
    with pytest.raises(MoneyError):
        Money(True, "USD")  # type: ignore[arg-type]


def test_cross_currency_operations_raise() -> None:
    usd = Money.parse("1", "USD")
    eur = Money.parse("1", "EUR")
    ops = [
        lambda: usd + eur,
        lambda: usd - eur,
        lambda: usd < eur,
        lambda: usd >= eur,
    ]
    for op in ops:
        with pytest.raises(CurrencyMismatch):
            op()


def test_multiplication_rounds_half_up() -> None:
    assert (Money(3, "USD") * "1.15").micros == 3  # type: ignore[arg-type]
    assert (Money.parse("0.62", "USD") * "1.15").micros == 713_000


def test_clamp_min_zero() -> None:
    assert Money.parse("-5", "USD").clamp_min_zero().is_zero
    assert Money.parse("5", "USD").clamp_min_zero().micros == 5_000_000


@given(money)
def test_no_precision_loss_through_string(m: Money) -> None:
    assert Money.parse(str(m.as_decimal()), m.currency) == m


@given(money, money)
def test_addition_is_commutative(a: Money, b: Money) -> None:
    if a.currency != b.currency:
        return
    assert a + b == b + a


@given(money, money)
def test_add_then_subtract_is_identity(a: Money, b: Money) -> None:
    if a.currency != b.currency:
        return
    assert (a + b) - b == a
