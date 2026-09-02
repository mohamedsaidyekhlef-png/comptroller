"""Principals. The security boundary of the entire system lives in this file."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

_SEGMENT: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class IdentityError(ValueError):
    """Raised on any malformed identity segment."""


def _validate(name: str, value: str) -> str:
    if not isinstance(value, str) or not _SEGMENT.match(value):
        raise IdentityError(f"invalid {name} segment: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting. Derived ONLY from a verified credential.

    SECURITY INVARIANT: no code path may construct a Principal from data
    that originated in an LLM response, a tool result, or a request body.
    An agent processing untrusted content must never be able to name itself.
    Construct exclusively via `from_credential`; tests/unit/test_identity.py
    asserts that no other call site exists anywhere in src/.
    """

    tenant: str
    agent: str
    run_id: str
    task_id: str | None = None

    def __post_init__(self) -> None:
        _validate("tenant", self.tenant)
        _validate("agent", self.agent)
        _validate("run_id", self.run_id)
        if self.task_id is not None:
            _validate("task_id", self.task_id)

    @classmethod
    def from_credential(
        cls,
        claims: dict[str, object],
        *,
        run_id: str,
        task_id: str | None = None,
    ) -> Principal:
        """Build from *already-verified* credential claims.

        The caller must verify the credential signature before calling this.
        Signature verification is not this function's job; refusing
        payload-sourced identity is.
        """
        tenant = claims.get("tenant")
        agent = claims.get("agent")
        if not isinstance(tenant, str) or not isinstance(agent, str):
            raise IdentityError("credential must carry string 'tenant' and 'agent' claims")
        return cls(tenant=tenant, agent=agent, run_id=run_id, task_id=task_id)

    # -- hierarchy -------------------------------------------------------

    @property
    def scope_path(self) -> str:
        """Canonical, lexicographically sortable, hierarchical path.

        Sortability is load-bearing: reservations acquire budget rows in
        sorted path order, which is how deadlock is *prevented* rather
        than merely detected.
        """
        path = f"tenant/{self.tenant}/agent/{self.agent}/run/{self.run_id}"
        return f"{path}/task/{self.task_id}" if self.task_id else path

    def ancestors(self) -> tuple[str, ...]:
        """Every scope path this principal is subject to, root first.

        A cap at any level binds, so authorization must satisfy all of them.
        """
        parts = self.scope_path.split("/")
        return tuple("/".join(parts[: i + 2]) for i in range(0, len(parts), 2))

    def __str__(self) -> str:
        return self.scope_path
