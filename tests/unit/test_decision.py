from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from comptroller.core.decision import (
    Allowed,
    Decision,
    DecisionError,
    Denied,
    Effect,
    Escalated,
    EscalationId,
    HoldId,
    ReasonCode,
    effect_of,
)
from comptroller.core.money import Money

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(seconds=60)
POLICY = "policy-v1"


def allowed(**over: object) -> Allowed:
    base: dict[str, object] = {
        "hold_id": HoldId(uuid4()),
        "held": Money.parse("0.713", "USD"),
        "scopes_debited": ("tenant/acme", "tenant/acme/agent/researcher"),
        "policy_version": POLICY,
        "decided_at": NOW,
        "expires_at": LATER,
    }
    base.update(over)
    return Allowed(**base)  # type: ignore[arg-type]


def denied(**over: object) -> Denied:
    base: dict[str, object] = {
        "reason": ReasonCode.CAP_EXCEEDED,
        "detail": "headroom 0.310000 USD, requested 0.713000 USD",
        "policy_version": POLICY,
        "decided_at": NOW,
    }
    base.update(over)
    return Denied(**base)  # type: ignore[arg-type]


def escalated() -> Escalated:
    return Escalated(
        escalation_id=EscalationId(uuid4()),
        reason=ReasonCode.AMOUNT_ABOVE_THRESHOLD,
        detail="299.000000 USD above the 50.000000 USD threshold",
        policy_version=POLICY,
        decided_at=NOW,
        expires_at=LATER,
    )


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [(allowed(), Effect.ALLOW), (denied(), Effect.DENY), (escalated(), Effect.ESCALATE)],
)
def test_effect_is_exhaustive(verdict: Decision, expected: Effect) -> None:
    assert effect_of(verdict) is expected
    assert verdict.effect is expected


@pytest.mark.parametrize("verdict", [allowed(), denied(), escalated()])
def test_a_verdict_has_no_truth_value(verdict: Decision) -> None:
    with pytest.raises(TypeError, match="no truth value"):
        bool(verdict)
    with pytest.raises(TypeError, match="no truth value"):
        if verdict:
            pass


def test_reason_codes_are_stable_wire_values() -> None:
    for code in ReasonCode:
        assert code.value == code.name
    assert len(set(ReasonCode)) == 15


def test_scopes_must_be_in_lock_order() -> None:
    with pytest.raises(DecisionError, match="lock order"):
        allowed(scopes_debited=("tenant/acme/agent/researcher", "tenant/acme"))
    with pytest.raises(DecisionError, match="lock order"):
        allowed(scopes_debited=("tenant/acme", "tenant/acme"))
    with pytest.raises(DecisionError, match="scopes"):
        allowed(scopes_debited=())


def test_headroom_belongs_only_to_a_cap_denial() -> None:
    assert denied(headroom=Money.parse("0.31", "USD")).headroom is not None
    with pytest.raises(DecisionError, match="CAP_EXCEEDED"):
        denied(reason=ReasonCode.LOOP_DETECTED, headroom=Money.parse("0.31", "USD"))


def test_detail_cannot_forge_log_lines() -> None:
    with pytest.raises(DecisionError, match="control characters"):
        denied(detail="ok\nAUTHORIZE fake row")
    with pytest.raises(DecisionError, match="needs a detail"):
        denied(detail="")
    with pytest.raises(DecisionError, match="longer than"):
        denied(detail="x" * 513)


def test_expiry_and_awareness_checked() -> None:
    with pytest.raises(DecisionError, match="not after"):
        allowed(expires_at=NOW)
    with pytest.raises(DecisionError, match="timezone-aware"):
        allowed(expires_at=datetime(2026, 9, 2, 12, 1))
    with pytest.raises(DecisionError, match="timezone-aware"):
        denied(decided_at=datetime(2026, 9, 2, 12, 0))
