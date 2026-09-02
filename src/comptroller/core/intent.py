"""Intent: the proposal of one billable action.

An Intent is inert data. Constructing one authorizes nothing; it is what an
enforcement point hands to authorize(). Two fields carry the weight:

principal   supplied by the enforcement point from a verified credential,
            never read from anything a model produced.
fingerprint a hash of the action itself, deliberately excluding the intent
            id, the timestamp, the principal and the amount, so that a retry
            storm looks identical on every attempt and a mandate can be bound
            to an action rather than to one attempt at it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from comptroller.core.identity import Principal
from comptroller.core.money import Money

FINGERPRINT_VERSION = "v1"

# \A and \Z, not ^ and $: ^...$ accepts a trailing newline, which is how a
# newline reached the scope path in the identity validator.
_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_FACTOR_RE = re.compile(r"\A[0-9]{1,2}(\.[0-9]{1,6})?\Z")
_MAX_ARG_DEPTH = 8
_MAX_SAFETY_FACTOR = Decimal("3")


class IntentError(ValueError):
    """An intent was refused before any policy rule ran."""


class IntentKind(StrEnum):
    LLM_INFERENCE = "LLM_INFERENCE"
    TOOL_CALL = "TOOL_CALL"
    API_PURCHASE = "API_PURCHASE"
    PAYMENT = "PAYMENT"


class EstimateBasis(StrEnum):
    PRICEBOOK = "PRICEBOOK"
    DECLARED = "DECLARED"
    FIXED = "FIXED"


def _validate_json(value: object, path: str, depth: int) -> None:
    if depth > _MAX_ARG_DEPTH:
        raise IntentError(f"refused: {path} nests deeper than {_MAX_ARG_DEPTH}")
    if value is None or isinstance(value, bool | str | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise IntentError(f"refused: {path} is not a finite number")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise IntentError(f"refused: {path} has a non-string key {key!r}")
            _validate_json(item, f"{path}.{key}", depth + 1)
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]", depth + 1)
        return
    raise IntentError(f"refused: {path} is {type(value).__name__}, not JSON data")


def canonical_json(args: Mapping[str, object] | None) -> str:
    """Sorted keys, no whitespace, ASCII only, no NaN. Byte-identical for
    equal inputs, which is what makes the fingerprint comparable."""
    if args is None:
        return "{}"
    _validate_json(args, "args", 0)
    return json.dumps(
        args, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


@dataclass(frozen=True, slots=True)
class Resource:
    """What is being bought: a model, a tool, an endpoint, a payee."""

    provider: str
    name: str
    args_canonical: str = "{}"

    def __post_init__(self) -> None:
        for label, segment in (("provider", self.provider), ("name", self.name)):
            if not _NAME_RE.match(segment):
                raise IntentError(f"refused: invalid {label} {segment!r}")
        try:
            parsed = json.loads(self.args_canonical)
        except json.JSONDecodeError as exc:
            raise IntentError("refused: args_canonical is not JSON") from exc
        if not isinstance(parsed, dict):
            raise IntentError("refused: args_canonical must be a JSON object")
        if canonical_json(parsed) != self.args_canonical:
            raise IntentError("refused: args_canonical is not canonical, use Resource.build")

    @classmethod
    def build(cls, provider: str, name: str, args: Mapping[str, object] | None = None) -> Self:
        return cls(provider=provider, name=name, args_canonical=canonical_json(args))


@dataclass(frozen=True, slots=True)
class Estimate:
    """What the action is expected to cost, and how much that guess is padded.

    The hold is amount * safety_factor. A known price is never padded, so
    FIXED forbids a factor other than 1.
    """

    amount: Money
    basis: EstimateBasis
    safety_factor: str = "1"
    pricebook_version: str | None = None

    def __post_init__(self) -> None:
        if self.amount.micros < 0:
            raise IntentError("refused: negative estimate")
        if not _FACTOR_RE.match(self.safety_factor):
            raise IntentError(f"refused: malformed safety factor {self.safety_factor!r}")
        factor = Decimal(self.safety_factor)
        if factor < 1:
            raise IntentError("refused: a safety factor below 1 would under-reserve")
        if factor > _MAX_SAFETY_FACTOR:
            raise IntentError(f"refused: safety factor above {_MAX_SAFETY_FACTOR}")
        if self.basis is EstimateBasis.PRICEBOOK and not self.pricebook_version:
            raise IntentError("refused: PRICEBOOK estimate without a pricebook version")
        if self.basis is not EstimateBasis.PRICEBOOK and self.pricebook_version:
            raise IntentError("refused: pricebook version on a non-PRICEBOOK estimate")
        if self.basis is EstimateBasis.FIXED and factor != 1:
            raise IntentError("refused: a fixed price must not be padded")

    @property
    def hold_amount(self) -> Money:
        return self.amount * self.safety_factor


@dataclass(frozen=True, slots=True)
class Intent:
    intent_id: UUID
    principal: Principal
    kind: IntentKind
    resource: Resource
    estimate: Estimate
    created_at: datetime
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise IntentError("refused: created_at must be timezone-aware")
        if self.trace_id is not None and not _NAME_RE.match(self.trace_id):
            raise IntentError(f"refused: invalid trace id {self.trace_id!r}")
        if self.kind is IntentKind.PAYMENT and self.estimate.basis is not EstimateBasis.FIXED:
            raise IntentError("refused: a payment needs a fixed price, not an estimate")

    @classmethod
    def new(
        cls,
        *,
        principal: Principal,
        kind: IntentKind,
        resource: Resource,
        estimate: Estimate,
        trace_id: str | None = None,
    ) -> Self:
        """created_at is read from this process, never from the caller. An
        agent that could set its own clock could dodge a daily window."""
        return cls(
            intent_id=uuid4(),
            principal=principal,
            kind=kind,
            resource=resource,
            estimate=estimate,
            created_at=datetime.now(UTC),
            trace_id=trace_id,
        )

    @property
    def fingerprint(self) -> str:
        payload = "|".join(
            (
                FINGERPRINT_VERSION,
                self.kind.value,
                self.resource.provider,
                self.resource.name,
                self.resource.args_canonical,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def hold_amount(self) -> Money:
        return self.estimate.hold_amount
