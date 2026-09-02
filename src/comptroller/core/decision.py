"""The verdict returned by authorize().

DENY and ESCALATE are values, not exceptions. An exception is a control-flow
signal that callers routinely swallow; a value has to be inspected. Only an
engine fault raises, and STRICT mode converts that to a denial.

None of the three has a truth value. bool(decision) raises, because
"if authorize(...)" would treat a denial as permission.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType, NoReturn, assert_never
from uuid import UUID

from comptroller.core.money import Money

HoldId = NewType("HoldId", UUID)
EscalationId = NewType("EscalationId", UUID)

_MAX_DETAIL = 512


class DecisionError(ValueError):
    """A verdict was constructed in a state the ledger cannot record."""


class Effect(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ESCALATE = "ESCALATE"


class ReasonCode(StrEnum):
    """Stable wire values. These are written to ledger rows, so a member is
    added or deprecated, never renamed."""

    CAP_EXCEEDED = "CAP_EXCEEDED"
    SCOPE_UNKNOWN = "SCOPE_UNKNOWN"
    LOOP_DETECTED = "LOOP_DETECTED"
    VELOCITY_EXCEEDED = "VELOCITY_EXCEEDED"
    MERCHANT_NOT_ALLOWED = "MERCHANT_NOT_ALLOWED"
    NOVEL_COUNTERPARTY = "NOVEL_COUNTERPARTY"
    AMOUNT_ABOVE_THRESHOLD = "AMOUNT_ABOVE_THRESHOLD"
    KIND_NOT_PERMITTED = "KIND_NOT_PERMITTED"
    MANDATE_REQUIRED = "MANDATE_REQUIRED"
    MANDATE_INVALID = "MANDATE_INVALID"
    MANDATE_REPLAYED = "MANDATE_REPLAYED"
    MANDATE_NOT_BOUND = "MANDATE_NOT_BOUND"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    PRICE_UNKNOWN = "PRICE_UNKNOWN"
    ENGINE_FAULT = "ENGINE_FAULT"


def _check_detail(detail: str) -> None:
    if not detail:
        raise DecisionError("refused: a verdict needs a detail string")
    if len(detail) > _MAX_DETAIL:
        raise DecisionError(f"refused: detail longer than {_MAX_DETAIL} characters")
    if not detail.isprintable():
        raise DecisionError("refused: detail contains control characters")


def _check_window(decided_at: datetime, expires_at: datetime) -> None:
    for label, value in (("decided_at", decided_at), ("expires_at", expires_at)):
        if value.tzinfo is None or value.utcoffset() is None:
            raise DecisionError(f"refused: {label} must be timezone-aware")
    if expires_at <= decided_at:
        raise DecisionError("refused: expires_at is not after decided_at")


class _NoTruthValue:
    __slots__ = ()

    def __bool__(self) -> NoReturn:
        raise TypeError("a decision has no truth value; match on Allowed, Denied or Escalated")


@dataclass(frozen=True, slots=True)
class Allowed(_NoTruthValue):
    """A hold exists. The action may execute exactly once, then capture."""

    hold_id: HoldId
    held: Money
    scopes_debited: tuple[str, ...]
    policy_version: str
    decided_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _check_window(self.decided_at, self.expires_at)
        _check_detail(self.policy_version)
        if self.held.micros < 0:
            raise DecisionError("refused: negative hold")
        if not self.scopes_debited:
            raise DecisionError("refused: an allow must name the scopes it debited")
        ordered = tuple(sorted(set(self.scopes_debited)))
        if ordered != self.scopes_debited:
            raise DecisionError(
                "refused: scopes_debited must be sorted and unique, it is the lock order"
            )

    @property
    def effect(self) -> Effect:
        return Effect.ALLOW


@dataclass(frozen=True, slots=True)
class Denied(_NoTruthValue):
    """No hold was taken and nothing was reserved. The action must not run."""

    reason: ReasonCode
    detail: str
    policy_version: str
    decided_at: datetime
    rule_id: str | None = None
    headroom: Money | None = None

    def __post_init__(self) -> None:
        _check_detail(self.detail)
        _check_detail(self.policy_version)
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise DecisionError("refused: decided_at must be timezone-aware")
        if self.headroom is not None and self.reason is not ReasonCode.CAP_EXCEEDED:
            raise DecisionError("refused: headroom only belongs on a CAP_EXCEEDED denial")

    @property
    def effect(self) -> Effect:
        return Effect.DENY


@dataclass(frozen=True, slots=True)
class Escalated(_NoTruthValue):
    """A human, holding a signing key, decides. Still no hold."""

    escalation_id: EscalationId
    reason: ReasonCode
    detail: str
    policy_version: str
    decided_at: datetime
    expires_at: datetime
    rule_id: str | None = None

    def __post_init__(self) -> None:
        _check_detail(self.detail)
        _check_detail(self.policy_version)
        _check_window(self.decided_at, self.expires_at)

    @property
    def effect(self) -> Effect:
        return Effect.ESCALATE


Decision = Allowed | Denied | Escalated


def effect_of(decision: Decision) -> Effect:
    """Exhaustive by construction: adding a fourth verdict without handling it
    here fails type checking rather than falling through to a default."""
    match decision:
        case Allowed():
            return Effect.ALLOW
        case Denied():
            return Effect.DENY
        case Escalated():
            return Effect.ESCALATE
        case _:
            assert_never(decision)
