"""Exact monetary arithmetic. No floats anywhere in this project."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, NewType

Micros = NewType("Micros", int)
"""10^-6 of one currency unit. LLM pricing is sub-cent; cents would drift."""

SCALE: Final = 6


class MoneyError(ValueError):
    """Base class for all monetary errors."""


class CurrencyMismatch(MoneyError):
    def __init__(self, left: str, right: str) -> None:
        super().__init__(f"cannot combine {left} with {right}")
        self.left = left
        self.right = right


@dataclass(frozen=True, slots=True)
class Money:
    """An exact, immutable, single-currency amount.

    Cross-currency arithmetic raises rather than coercing: every conversion
    must be an explicit, rate-stamped ledger entry, never a silent cast.
    """

    micros: Micros
    currency: str

    def __post_init__(self) -> None:
        c = self.currency
        if len(c) != 3 or not c.isalpha() or not c.isupper():
            raise MoneyError(f"currency must be ISO-4217 uppercase alpha, got {c!r}")
        if not isinstance(self.micros, int) or isinstance(self.micros, bool):
            raise MoneyError(f"micros must be int, got {type(self.micros).__name__}")

    # -- construction ----------------------------------------------------

    @classmethod
    def parse(cls, amount: str, currency: str) -> Money:
        """Parse a decimal *string*. Rejects float input: "0.1" is exact, 0.1 is not."""
        if not isinstance(amount, str):
            raise MoneyError(f"amount must be str, got {type(amount).__name__}")
        try:
            scaled = Decimal(amount).scaleb(SCALE)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise MoneyError(f"unparseable amount {amount!r}") from exc
        if scaled != scaled.to_integral_value():
            raise MoneyError(f"{amount!r} exceeds {SCALE} decimal places")
        return cls(Micros(int(scaled)), currency)

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(Micros(0), currency)

    # -- arithmetic ------------------------------------------------------

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(self.currency, other.currency)

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(Micros(self.micros + other.micros), self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(Micros(self.micros - other.micros), self.currency)

    def __mul__(self, factor: int | str | Decimal) -> Money:
        """Scale by an exact factor, rounding half-up. Used for safety margins."""
        d = factor if isinstance(factor, Decimal) else Decimal(factor)
        product = (Decimal(self.micros) * d).quantize(Decimal(1), rounding="ROUND_HALF_UP")
        return Money(Micros(int(product)), self.currency)

    def __neg__(self) -> Money:
        return Money(Micros(-self.micros), self.currency)

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.micros < other.micros

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.micros <= other.micros

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.micros > other.micros

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.micros >= other.micros

    # -- predicates and rendering ---------------------------------------

    @property
    def is_zero(self) -> bool:
        return self.micros == 0

    @property
    def is_positive(self) -> bool:
        return self.micros > 0

    def clamp_min_zero(self) -> Money:
        return self if self.micros >= 0 else Money.zero(self.currency)

    def as_decimal(self) -> Decimal:
        return Decimal(self.micros).scaleb(-SCALE)

    def __str__(self) -> str:
        return f"{self.as_decimal():.6f} {self.currency}"

    def __repr__(self) -> str:
        return f"Money.parse({str(self.as_decimal())!r}, {self.currency!r})"
