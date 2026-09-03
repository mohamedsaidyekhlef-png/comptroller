"""Reservation against Postgres.

Three things here are deliberate and worth reading before changing anything.

Lock order. Every reservation debits windows in sorted scope order, root
first, which is the order Principal.ancestors already returns. Two agents
racing on an overlapping scope chain therefore contend in the same sequence
and cannot deadlock by holding each other s rows.

Isolation. READ COMMITTED is sufficient. The conditional UPDATE re-evaluates
its WHERE clause after waiting for a lock, so the loser of a race sees the
winner s committed reservation and matches zero rows. SERIALIZABLE would add
retry handling for no additional safety here.

All or nothing. A denial rolls the whole transaction back, so a reservation
that fails on the fourth window leaves no trace on the first three. The DENY
ledger row is written afterwards in its own transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import dict_row

from comptroller.core.decision import (
    Allowed,
    Decision,
    Denied,
    HoldId,
    ReasonCode,
)
from comptroller.core.intent import Intent
from comptroller.core.money import Money

DEFAULT_TTL = timedelta(seconds=60)

# The server-side expression that pins a window. Never a client timestamp.
_WINDOW_START = (
    "(CASE WHEN w.window_kind = 'DAY' "
    "THEN date_trunc('day', now()) ELSE 'epoch'::timestamptz END)"
)


def money_from_micros(micros: int, currency: str) -> Money:
    """Rebuild a Money from a stored integer without touching the frozen core."""
    sign = "-" if micros < 0 else ""
    absolute = abs(micros)
    return Money.parse(f"{sign}{absolute // 1_000_000}.{absolute % 1_000_000:06d}", currency)


class StoreError(RuntimeError):
    """The engine could not reach a verdict. STRICT mode turns this into a denial."""


class _Refuse(Exception):
    """Internal: aborts the transaction so nothing stays debited."""

    def __init__(self, reason: ReasonCode, detail: str, headroom: int | None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.headroom = headroom


@dataclass(frozen=True, slots=True)
class CaptureResult:
    hold_id: HoldId
    captured: Money
    overage: Money
    replayed: bool


@dataclass(frozen=True, slots=True)
class WindowKey:
    scope_path: str
    window_kind: str
    window_start: datetime

    def as_json(self) -> dict[str, str]:
        return {
            "scope_path": self.scope_path,
            "window_kind": self.window_kind,
            "window_start": self.window_start.isoformat(),
        }


def _materialise_windows(conn: Connection[Any], scopes: list[str]) -> None:
    """Create the current window row for every configured budget in the chain."""
    conn.execute(
        """
        INSERT INTO budget_window
            (scope_path, window_kind, window_start, cap_micros, currency)
        SELECT p.scope_path, p.window_kind,
               CASE WHEN p.window_kind = 'DAY'
                    THEN date_trunc('day', now()) ELSE 'epoch'::timestamptz END,
               p.cap_micros, p.currency
        FROM budget_policy p
        WHERE p.scope_path = ANY(%s)
        ON CONFLICT DO NOTHING
        """,
        (scopes,),
    )


def _applicable_windows(conn: Connection[Any], scopes: list[str]) -> list[dict[str, Any]]:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        f"""
        SELECT w.scope_path, w.window_kind::text AS window_kind,
               w.window_start, w.currency
        FROM budget_window w
        WHERE w.scope_path = ANY(%s)
          AND w.window_start = {_WINDOW_START}
        ORDER BY w.scope_path, w.window_kind
        """,
        (scopes,),
    )
    return list(cur.fetchall())


def _replay(conn: Connection[Any], intent_id: UUID) -> Allowed | None:
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(
        """
        SELECT hold_id, held_micros, currency, policy_version,
               created_at, expires_at, debited
        FROM hold WHERE intent_id = %s
        """,
        (intent_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    debited = cast(list[dict[str, str]], row["debited"])
    return Allowed(
        hold_id=HoldId(cast(UUID, row["hold_id"])),
        held=money_from_micros(int(row["held_micros"]), str(row["currency"])),
        scopes_debited=tuple(sorted({str(d["scope_path"]) for d in debited})),
        policy_version=str(row["policy_version"]),
        decided_at=cast(datetime, row["created_at"]),
        expires_at=cast(datetime, row["expires_at"]),
    )


def reserve(
    conn: Connection[Any],
    intent: Intent,
    *,
    policy_version: str = "policy-v0",
    ttl: timedelta = DEFAULT_TTL,
) -> Decision:
    """Reserve intent.hold_amount across every configured scope, or nothing.

    Returns Allowed with a hold, or Denied. Raises StoreError only when the
    engine itself fails, which the caller must treat as a denial in STRICT mode.
    """
    scopes = list(intent.principal.ancestors())
    held = intent.hold_amount
    held_micros = held.micros

    try:
        with conn.transaction():
            replayed = _replay(conn, intent.intent_id)
            if replayed is not None:
                return replayed

            _materialise_windows(conn, scopes)
            windows = _applicable_windows(conn, scopes)
            if not windows:
                raise _Refuse(
                    ReasonCode.SCOPE_UNKNOWN,
                    f"no budget configured for any scope in {intent.principal.scope_path}",
                    None,
                )

            mismatched = [w for w in windows if str(w["currency"]) != held.currency]
            if mismatched:
                raise _Refuse(
                    ReasonCode.PRICE_UNKNOWN,
                    f"budget {mismatched[0]['scope_path']} is denominated in "
                    f"{mismatched[0]['currency']!s}, intent is in {held.currency}",
                    None,
                )

            debited: list[WindowKey] = []
            for w in windows:  # already in canonical lock order
                key = WindowKey(
                    scope_path=str(w["scope_path"]),
                    window_kind=str(w["window_kind"]),
                    window_start=cast(datetime, w["window_start"]),
                )
                cur = conn.cursor(row_factory=dict_row)
                cur.execute(
                    """
                    UPDATE budget_window
                    SET reserved_micros = reserved_micros + %s
                    WHERE scope_path = %s AND window_kind = %s::window_kind
                      AND window_start = %s
                      AND spent_micros + reserved_micros + %s <= cap_micros
                    RETURNING cap_micros - spent_micros - reserved_micros AS headroom
                    """,
                    (
                        held_micros,
                        key.scope_path,
                        key.window_kind,
                        key.window_start,
                        held_micros,
                    ),
                )
                if cur.rowcount == 0:
                    head = conn.cursor(row_factory=dict_row)
                    head.execute(
                        """
                        SELECT cap_micros - spent_micros - reserved_micros AS headroom,
                               currency
                        FROM budget_window
                        WHERE scope_path = %s AND window_kind = %s::window_kind
                          AND window_start = %s
                        """,
                        (key.scope_path, key.window_kind, key.window_start),
                    )
                    hrow = head.fetchone()
                    headroom = int(hrow["headroom"]) if hrow else 0
                    raise _Refuse(
                        ReasonCode.CAP_EXCEEDED,
                        f"{key.scope_path} {key.window_kind} headroom "
                        f"{money_from_micros(headroom, held.currency)}, "
                        f"requested {held}",
                        headroom,
                    )
                debited.append(key)

            hold_id = uuid4()
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(
                """
                INSERT INTO hold (hold_id, intent_id, scope_path, fingerprint, kind,
                                  currency, held_micros, state, policy_version,
                                  debited, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING', %s, %s::jsonb,
                        now() + %s)
                RETURNING created_at, expires_at
                """,
                (
                    hold_id,
                    intent.intent_id,
                    intent.principal.scope_path,
                    intent.fingerprint,
                    intent.kind.value,
                    held.currency,
                    held_micros,
                    policy_version,
                    json.dumps([k.as_json() for k in debited]),
                    ttl,
                ),
            )
            hrow = cur.fetchone()
            if hrow is None:  # pragma: no cover
                raise StoreError("hold insert returned no row")

            conn.execute(
                """
                INSERT INTO ledger (entry_type, hold_id, intent_id, scope_path,
                                    fingerprint, amount_micros, currency, policy_version)
                VALUES ('AUTHORIZE', %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    hold_id,
                    intent.intent_id,
                    intent.principal.scope_path,
                    intent.fingerprint,
                    held_micros,
                    held.currency,
                    policy_version,
                ),
            )
            return Allowed(
                hold_id=HoldId(hold_id),
                held=held,
                scopes_debited=tuple(sorted({k.scope_path for k in debited})),
                policy_version=policy_version,
                decided_at=cast(datetime, hrow["created_at"]),
                expires_at=cast(datetime, hrow["expires_at"]),
            )

    except _Refuse as refusal:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            INSERT INTO ledger (entry_type, intent_id, scope_path, fingerprint,
                                amount_micros, currency, reason_code, detail,
                                policy_version)
            VALUES ('DENY', %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING at
            """,
            (
                intent.intent_id,
                intent.principal.scope_path,
                intent.fingerprint,
                held_micros,
                held.currency,
                refusal.reason.value,
                refusal.detail,
                policy_version,
            ),
        )
        drow = cur.fetchone()
        if drow is None:  # pragma: no cover
            raise StoreError("deny ledger insert returned no row") from refusal
        return Denied(
            reason=refusal.reason,
            detail=refusal.detail,
            policy_version=policy_version,
            decided_at=cast(datetime, drow["at"]),
            headroom=(
                money_from_micros(refusal.headroom, held.currency)
                if refusal.headroom is not None and refusal.reason is ReasonCode.CAP_EXCEEDED
                else None
            ),
        )


def capture(
    conn: Connection[Any],
    hold_id: HoldId,
    actual: Money,
    *,
    policy_version: str = "policy-v0",
) -> CaptureResult:
    """Settle a hold at its real cost.

    Spend is credited up to the amount held; anything beyond that is overage,
    counted in its own column and alarmed, never silently absorbed into a cap.
    Capturing an already-captured hold replays the stored result.
    """
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            SELECT state::text AS state, held_micros, currency,
                   debited, captured_micros, overage_micros
            FROM hold WHERE hold_id = %s FOR UPDATE
            """,
            (hold_id,),
        )
        locked = cur.fetchone()
        if locked is None:
            raise StoreError(f"no such hold {hold_id}")

        currency = str(locked["currency"])
        state = str(locked["state"])

        if state == "CAPTURED":
            return CaptureResult(
                hold_id=hold_id,
                captured=money_from_micros(int(locked["captured_micros"]), currency),
                overage=money_from_micros(int(locked["overage_micros"] or 0), currency),
                replayed=True,
            )
        if state not in ("PENDING", "EXPIRED"):
            raise StoreError(f"hold {hold_id} is {state}, capture would break P3")

        held_micros = int(locked["held_micros"])
        cur.execute(
            """
            UPDATE hold
            SET state = 'CAPTURED',
                captured_micros = LEAST(%s, held_micros),
                overage_micros  = GREATEST(0, %s - held_micros),
                resolved_at = now()
            WHERE hold_id = %s
            RETURNING captured_micros, overage_micros
            """,
            (actual.micros, actual.micros, hold_id),
        )
        written = cur.fetchone()
        if written is None:  # pragma: no cover
            raise StoreError(f"capture update lost hold {hold_id}")

        row = {
            "prev_state": state,
            "held_micros": held_micros,
            "currency": currency,
            "debited": locked["debited"],
            "captured_micros": int(written["captured_micros"]),
            "overage_micros": int(written["overage_micros"] or 0),
        }

        prev_state = str(row["prev_state"])
        held_micros = int(row["held_micros"])
        currency = str(row["currency"])
        debited = cast(list[dict[str, str]], row["debited"])
        captured_micros = int(row["captured_micros"])
        overage_micros = int(row["overage_micros"])

        for entry in debited:
            if prev_state == "PENDING":
                # The reservation is still standing: convert it to spend.
                conn.execute(
                    """
                    UPDATE budget_window
                    SET spent_micros    = spent_micros + LEAST(%s, %s),
                        reserved_micros = reserved_micros - %s,
                        overage_micros  = overage_micros + GREATEST(0, %s - %s)
                    WHERE scope_path = %s AND window_kind = %s::window_kind
                      AND window_start = %s
                    """,
                    (
                        actual.micros,
                        held_micros,
                        held_micros,
                        actual.micros,
                        held_micros,
                        entry["scope_path"],
                        entry["window_kind"],
                        entry["window_start"],
                    ),
                )
            else:
                # The reaper already gave the reservation back, so there may be
                # no headroom left. Credit what fits and alarm on the rest
                # rather than letting the CHECK abort a settled charge.
                conn.execute(
                    """
                    UPDATE budget_window
                    SET spent_micros = spent_micros
                          + LEAST(%s, cap_micros - spent_micros - reserved_micros),
                        overage_micros = overage_micros + %s
                          - LEAST(%s, cap_micros - spent_micros - reserved_micros)
                    WHERE scope_path = %s AND window_kind = %s::window_kind
                      AND window_start = %s
                    """,
                    (
                        captured_micros,
                        captured_micros,
                        captured_micros,
                        entry["scope_path"],
                        entry["window_kind"],
                        entry["window_start"],
                    ),
                )

        conn.execute(
            """
            INSERT INTO ledger (entry_type, hold_id, scope_path, amount_micros,
                                currency, detail, policy_version)
            SELECT 'CAPTURE', hold_id, scope_path, %s, currency,
                   'settled from ' || %s, %s
            FROM hold WHERE hold_id = %s
            """,
            (captured_micros, prev_state, policy_version, hold_id),
        )
        if overage_micros > 0:
            conn.execute(
                """
                INSERT INTO ledger (entry_type, hold_id, scope_path, amount_micros,
                                    currency, reason_code, detail, policy_version)
                SELECT 'BUDGET_BREACH', hold_id, scope_path, %s, currency,
                       'AMOUNT_ABOVE_THRESHOLD',
                       'actual exceeded the hold, raise the safety factor', %s
                FROM hold WHERE hold_id = %s
                """,
                (overage_micros, policy_version, hold_id),
            )

        return CaptureResult(
            hold_id=hold_id,
            captured=money_from_micros(captured_micros, currency),
            overage=money_from_micros(overage_micros, currency),
            replayed=False,
        )


def release(
    conn: Connection[Any],
    hold_id: HoldId,
    *,
    policy_version: str = "policy-v0",
    reason: str = "action failed",
) -> bool:
    """Give a standing reservation back. Idempotent; False if already resolved."""
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            UPDATE hold
            SET state = 'RELEASED', resolved_at = now()
            WHERE hold_id = %s AND state = 'PENDING'
            RETURNING held_micros, currency, debited, scope_path
            """,
            (hold_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        for entry in cast(list[dict[str, str]], row["debited"]):
            conn.execute(
                """
                UPDATE budget_window
                SET reserved_micros = reserved_micros - %s
                WHERE scope_path = %s AND window_kind = %s::window_kind
                  AND window_start = %s
                """,
                (
                    int(row["held_micros"]),
                    entry["scope_path"],
                    entry["window_kind"],
                    entry["window_start"],
                ),
            )
        conn.execute(
            """
            INSERT INTO ledger (entry_type, hold_id, scope_path, amount_micros,
                                currency, detail, policy_version)
            VALUES ('RELEASE', %s, %s, %s, %s, %s, %s)
            """,
            (
                hold_id,
                str(row["scope_path"]),
                int(row["held_micros"]),
                str(row["currency"]),
                reason,
                policy_version,
            ),
        )
        return True


def reap_expired(
    conn: Connection[Any], *, policy_version: str = "policy-v0", limit: int = 500
) -> int:
    """Return reservations whose holder never came back. Server clock only."""
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(
            """
            UPDATE hold
            SET state = 'EXPIRED', resolved_at = now()
            WHERE hold_id IN (
                SELECT hold_id FROM hold
                WHERE state = 'PENDING' AND expires_at < now()
                ORDER BY expires_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING hold_id, held_micros, currency, debited, scope_path
            """,
            (limit,),
        )
        rows = list(cur.fetchall())
        for row in rows:
            for entry in cast(list[dict[str, str]], row["debited"]):
                conn.execute(
                    """
                    UPDATE budget_window
                    SET reserved_micros = reserved_micros - %s
                    WHERE scope_path = %s AND window_kind = %s::window_kind
                      AND window_start = %s
                    """,
                    (
                        int(row["held_micros"]),
                        entry["scope_path"],
                        entry["window_kind"],
                        entry["window_start"],
                    ),
                )
            conn.execute(
                """
                INSERT INTO ledger (entry_type, hold_id, scope_path, amount_micros,
                                    currency, reason_code, detail, policy_version)
                VALUES ('EXPIRE', %s, %s, %s, %s, 'ENGINE_FAULT',
                        'hold expired before capture', %s)
                """,
                (
                    cast(UUID, row["hold_id"]),
                    str(row["scope_path"]),
                    int(row["held_micros"]),
                    str(row["currency"]),
                    policy_version,
                ),
            )
        return len(rows)
