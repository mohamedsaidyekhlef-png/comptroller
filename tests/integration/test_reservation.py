from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg import Connection

from comptroller.core.decision import Allowed, Denied, ReasonCode
from comptroller.core.identity import Principal
from comptroller.core.intent import Estimate, EstimateBasis, Intent, IntentKind, Resource
from comptroller.core.money import Money
from comptroller.store.invariants import assert_healthy, violations
from comptroller.store.postgres import (
    StoreError,
    capture,
    reap_expired,
    release,
    reserve,
)

from .conftest import DSN, requires_db

pytestmark = requires_db

TENANT = "tenant/acme"
AGENT = "tenant/acme/agent/researcher"
RUN = "tenant/acme/agent/researcher/run/7f3a"


def intent_for(amount: str, *, run: str = "7f3a", tool: str = "gpt-4o") -> Intent:
    return Intent.new(
        principal=Principal.from_credential(
            {"tenant": "acme", "agent": "researcher"}, run_id=run
        ),
        kind=IntentKind.LLM_INFERENCE,
        resource=Resource.build("openai", tool, {"max_tokens": 512}),
        estimate=Estimate(
            amount=Money.parse(amount, "USD"),
            basis=EstimateBasis.FIXED,
        ),
    )


def window(db: Connection[Any], scope: str) -> dict[str, int]:
    cur = db.cursor()
    cur.execute(
        """
        SELECT spent_micros, reserved_micros, overage_micros
        FROM budget_window WHERE scope_path = %s
        """,
        (scope,),
    )
    row = cur.fetchone()
    if row is None:
        return {"spent": 0, "reserved": 0, "overage": 0}
    return {"spent": row[0], "reserved": row[1], "overage": row[2]}


def test_reserve_then_capture_moves_reserved_to_spent(db: Connection[Any], budget: Any) -> None:
    budget(AGENT, "1.00")
    decision = reserve(db, intent_for("0.40"))
    assert isinstance(decision, Allowed)
    assert window(db, AGENT) == {"spent": 0, "reserved": 400_000, "overage": 0}

    result = capture(db, decision.hold_id, Money.parse("0.37", "USD"))
    assert result.captured == Money.parse("0.37", "USD")
    assert result.overage == Money.parse("0", "USD")
    assert window(db, AGENT) == {"spent": 370_000, "reserved": 0, "overage": 0}
    assert_healthy(db)


def test_denial_leaves_nothing_debited(db: Connection[Any], budget: Any) -> None:
    budget(AGENT, "0.31")
    decision = reserve(db, intent_for("0.62"))
    assert isinstance(decision, Denied)
    assert decision.reason is ReasonCode.CAP_EXCEEDED
    assert decision.headroom == Money.parse("0.31", "USD")
    assert window(db, AGENT) == {"spent": 0, "reserved": 0, "overage": 0}

    cur = db.cursor()
    cur.execute("SELECT count(*) FROM hold")
    assert cur.fetchone()[0] == 0  # type: ignore[index]
    assert_healthy(db)


def test_partial_chain_failure_unwinds_the_parent(db: Connection[Any], budget: Any) -> None:
    """The tenant has room, the run does not. The tenant must not be left debited."""
    budget(TENANT, "500.00")
    budget(AGENT, "40.00")
    budget(RUN, "0.10")

    decision = reserve(db, intent_for("2.00"))
    assert isinstance(decision, Denied)
    for scope in (TENANT, AGENT, RUN):
        assert window(db, scope)["reserved"] == 0
    assert_healthy(db)


def test_no_budget_configured_fails_closed(db: Connection[Any]) -> None:
    decision = reserve(db, intent_for("0.01"))
    assert isinstance(decision, Denied)
    assert decision.reason is ReasonCode.SCOPE_UNKNOWN


def test_currency_mismatch_denied(db: Connection[Any], budget: Any) -> None:
    budget(AGENT, "10.00", currency="EUR")
    decision = reserve(db, intent_for("1.00"))
    assert isinstance(decision, Denied)
    assert decision.reason is ReasonCode.PRICE_UNKNOWN


def test_authorize_is_idempotent_per_intent(db: Connection[Any], budget: Any) -> None:
    budget(AGENT, "1.00")
    subject = intent_for("0.40")
    first = reserve(db, subject)
    second = reserve(db, subject)
    assert isinstance(first, Allowed) and isinstance(second, Allowed)
    assert first.hold_id == second.hold_id
    assert window(db, AGENT)["reserved"] == 400_000  # debited once, not twice


def test_capture_is_idempotent(db: Connection[Any], budget: Any) -> None:
    budget(AGENT, "1.00")
    decision = reserve(db, intent_for("0.40"))
    assert isinstance(decision, Allowed)
    first = capture(db, decision.hold_id, Money.parse("0.37", "USD"))
    second = capture(db, decision.hold_id, Money.parse("0.37", "USD"))
    assert not first.replayed and second.replayed
    assert window(db, AGENT)["spent"] == 370_000
    assert_healthy(db)


def test_overage_is_recorded_not_absorbed(db: Connection[Any], budget: Any) -> None:
    """Actual above the hold must not silently breach the cap."""
    budget(AGENT, "1.00")
    decision = reserve(db, intent_for("0.40"))
    assert isinstance(decision, Allowed)
    result = capture(db, decision.hold_id, Money.parse("0.55", "USD"))

    assert result.captured == Money.parse("0.40", "USD")
    assert result.overage == Money.parse("0.15", "USD")
    state = window(db, AGENT)
    assert state["spent"] == 400_000
    assert state["overage"] == 150_000

    cur = db.cursor()
    cur.execute("SELECT count(*) FROM ledger WHERE entry_type = 'BUDGET_BREACH'")
    assert cur.fetchone()[0] == 1  # type: ignore[index]
    assert_healthy(db)


def test_release_returns_the_reservation(db: Connection[Any], budget: Any) -> None:
    budget(AGENT, "1.00")
    decision = reserve(db, intent_for("0.40"))
    assert isinstance(decision, Allowed)
    assert release(db, decision.hold_id) is True
    assert release(db, decision.hold_id) is False  # idempotent
    assert window(db, AGENT) == {"spent": 0, "reserved": 0, "overage": 0}
    assert_healthy(db)


def test_reaper_frees_abandoned_holds(db: Connection[Any], budget: Any) -> None:
    budget(AGENT, "1.00")
    decision = reserve(db, intent_for("0.40"), ttl=timedelta(milliseconds=1))
    assert isinstance(decision, Allowed)
    time.sleep(0.05)
    assert reap_expired(db) == 1
    assert window(db, AGENT)["reserved"] == 0
    assert reap_expired(db) == 0
    assert_healthy(db)


def test_capture_after_expiry_is_all_overage_when_cap_is_gone(
    db: Connection[Any], budget: Any
) -> None:
    """A late capture must never push spent past the cap."""
    budget(AGENT, "1.00")
    slow = reserve(db, intent_for("1.00", tool="gpt-4o"), ttl=timedelta(milliseconds=1))
    assert isinstance(slow, Allowed)
    time.sleep(0.05)
    reap_expired(db)

    other = reserve(db, intent_for("1.00", tool="gpt-4o-mini"))
    assert isinstance(other, Allowed)
    capture(db, other.hold_id, Money.parse("1.00", "USD"))

    capture(db, slow.hold_id, Money.parse("1.00", "USD"))
    state = window(db, AGENT)
    assert state["spent"] == 1_000_000
    assert state["overage"] == 1_000_000
    assert not violations(db)


def test_hundred_agents_one_budget(db: Connection[Any], budget: Any) -> None:
    """The whole point. 100 threads, $10.00 cap, $0.25 each: 40 win, 60 lose."""
    budget(AGENT, "10.00")

    def attempt(n: int) -> str:
        with psycopg.connect(DSN, autocommit=True) as conn:
            decision = reserve(conn, intent_for("0.25", run=f"r{n:04d}"))
            return type(decision).__name__

    with ThreadPoolExecutor(max_workers=24) as pool:
        outcomes = list(pool.map(attempt, range(100)))

    assert outcomes.count("Allowed") == 40
    assert outcomes.count("Denied") == 60
    assert window(db, AGENT)["reserved"] == 10_000_000
    assert not violations(db)


def test_replay_while_pending_returns_the_same_hold(db: Connection[Any], budget: Any) -> None:
    """Authorising the same intent twice must not cut a second hold."""
    budget(AGENT, "1.00")
    intent = intent_for("0.40")

    first = reserve(db, intent)
    second = reserve(db, intent)

    assert isinstance(first, Allowed)
    assert isinstance(second, Allowed)
    assert second.hold_id == first.hold_id
    assert window(db, AGENT) == {"spent": 0, "reserved": 400_000, "overage": 0}


def test_reauthorising_a_captured_intent_is_denied(db: Connection[Any], budget: Any) -> None:
    """A resolved hold is not a licence to execute again."""
    budget(AGENT, "1.00")
    intent = intent_for("0.40")

    allowed = reserve(db, intent)
    assert isinstance(allowed, Allowed)
    capture(db, allowed.hold_id, Money.parse("0.37", "USD"))

    again = reserve(db, intent)
    assert isinstance(again, Denied)
    assert again.reason is ReasonCode.INTENT_ALREADY_RESOLVED
    assert "CAPTURED" in again.detail
    assert window(db, AGENT) == {"spent": 370_000, "reserved": 0, "overage": 0}
    assert violations(db) == []


def test_capture_refuses_a_foreign_currency(db: Connection[Any], budget: Any) -> None:
    """Micros are meaningless without their currency."""
    budget(AGENT, "1.00")
    allowed = reserve(db, intent_for("0.40"))
    assert isinstance(allowed, Allowed)

    with pytest.raises(StoreError, match="cannot capture"):
        capture(db, allowed.hold_id, Money.parse("0.37", "EUR"))

    assert window(db, AGENT) == {"spent": 0, "reserved": 400_000, "overage": 0}
    assert_healthy(db)
