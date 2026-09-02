from __future__ import annotations

import re
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from comptroller.core.identity import Principal
from comptroller.core.intent import (
    Estimate,
    EstimateBasis,
    Intent,
    IntentError,
    IntentKind,
    Resource,
    canonical_json,
)
from comptroller.core.money import Money

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


def principal(agent: str = "researcher") -> Principal:
    return Principal.from_credential({"tenant": "acme", "agent": agent}, run_id="7f3a")


def estimate(amount: str = "0.62", factor: str = "1.15") -> Estimate:
    return Estimate(
        amount=Money.parse(amount, "USD"),
        basis=EstimateBasis.PRICEBOOK,
        safety_factor=factor,
        pricebook_version="2026-09-01",
    )


def intent(**over: object) -> Intent:
    base: dict[str, object] = {
        "intent_id": uuid4(),
        "principal": principal(),
        "kind": IntentKind.LLM_INFERENCE,
        "resource": Resource.build("openai", "gpt-4o", {"max_tokens": 512}),
        "estimate": estimate(),
        "created_at": NOW,
    }
    base.update(over)
    return Intent(**base)  # type: ignore[arg-type]


def test_fingerprint_ignores_key_order() -> None:
    a = Resource.build("openai", "gpt-4o", {"a": 1, "b": 2})
    b = Resource.build("openai", "gpt-4o", {"b": 2, "a": 1})
    assert a == b
    assert intent(resource=a).fingerprint == intent(resource=b).fingerprint


def test_fingerprint_ignores_attempt_identity() -> None:
    first = intent()
    second = intent(
        intent_id=uuid4(),
        principal=principal("buyer"),
        created_at=datetime(2027, 1, 1, tzinfo=UTC),
        estimate=estimate(amount="99.00"),
        trace_id="run-2",
    )
    assert first.fingerprint == second.fingerprint
    assert HEX64.match(first.fingerprint)


@pytest.mark.parametrize(
    "changed",
    [
        {"kind": IntentKind.TOOL_CALL},
        {"resource": Resource.build("openai", "gpt-4o-mini", {"max_tokens": 512})},
        {"resource": Resource.build("anthropic", "gpt-4o", {"max_tokens": 512})},
        {"resource": Resource.build("openai", "gpt-4o", {"max_tokens": 513})},
    ],
)
def test_fingerprint_tracks_the_action(changed: dict[str, object]) -> None:
    assert intent().fingerprint != intent(**changed).fingerprint


def test_non_canonical_args_rejected() -> None:
    with pytest.raises(IntentError, match="canonical"):
        Resource(provider="openai", name="gpt-4o", args_canonical='{"b":1,"a":2}')
    with pytest.raises(IntentError, match="canonical"):
        Resource(provider="openai", name="gpt-4o", args_canonical='{"a": 1}')


@pytest.mark.parametrize("bad", ["openai\n", "openai\r", "\topenai", "../admin", "", "-x"])
def test_resource_segment_injection_rejected(bad: str) -> None:
    with pytest.raises(IntentError):
        Resource.build(bad, "gpt-4o")


def test_non_json_args_rejected() -> None:
    with pytest.raises(IntentError, match="finite"):
        canonical_json({"t": float("nan")})
    with pytest.raises(IntentError, match="non-string key"):
        canonical_json({1: "x"})  # type: ignore[dict-item]
    with pytest.raises(IntentError, match="not JSON data"):
        canonical_json({"o": object()})


def test_arg_depth_bounded() -> None:
    nested: dict[str, object] = {"k": "v"}
    for _ in range(12):
        nested = {"k": nested}
    with pytest.raises(IntentError, match="nests deeper"):
        canonical_json(nested)


def test_hold_is_estimate_times_factor() -> None:
    assert intent().hold_amount == Money.parse("0.713", "USD")


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(IntentError, match="timezone-aware"):
        intent(created_at=datetime(2026, 9, 2, 12, 0))


@pytest.mark.parametrize("factor", ["0.9", "0", "4", "1.0000001", "abc", "1,15", ""])
def test_safety_factor_bounds(factor: str) -> None:
    with pytest.raises(IntentError):
        estimate(factor=factor)


def test_pricebook_version_paired_with_basis() -> None:
    with pytest.raises(IntentError, match="without a pricebook version"):
        Estimate(amount=Money.parse("1", "USD"), basis=EstimateBasis.PRICEBOOK)
    with pytest.raises(IntentError, match="non-PRICEBOOK"):
        Estimate(
            amount=Money.parse("1", "USD"),
            basis=EstimateBasis.DECLARED,
            pricebook_version="2026-09-01",
        )


def test_fixed_price_is_never_padded() -> None:
    with pytest.raises(IntentError, match="must not be padded"):
        Estimate(
            amount=Money.parse("299", "USD"),
            basis=EstimateBasis.FIXED,
            safety_factor="1.15",
        )


def test_payment_requires_a_fixed_price() -> None:
    with pytest.raises(IntentError, match="fixed price"):
        intent(kind=IntentKind.PAYMENT, estimate=estimate(amount="299", factor="1"))
    ok = intent(
        kind=IntentKind.PAYMENT,
        estimate=Estimate(amount=Money.parse("299", "USD"), basis=EstimateBasis.FIXED),
    )
    assert ok.hold_amount == Money.parse("299", "USD")


def test_intent_is_frozen() -> None:
    subject = intent()
    with pytest.raises(FrozenInstanceError):
        subject.kind = IntentKind.PAYMENT  # type: ignore[misc]


@given(st.dictionaries(st.text(min_size=1, max_size=8), st.integers(), max_size=6))
def test_canonicalisation_is_idempotent(args: dict[str, int]) -> None:
    once = canonical_json(args)
    assert canonical_json(dict(reversed(list(args.items())))) == once
    assert HEX64.match(
        Resource.build("p", "n", args).args_canonical
        and intent(resource=Resource.build("p", "n", args)).fingerprint
    )
