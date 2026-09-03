"""P1 to P3 as queries.

Written as SQL rather than Python so they can be run against a live database
by a test, by the reaper, or by a human during an incident. Each returns the
rows that violate the property, so empty means healthy.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

P1_AUTHORIZATION_PRECEDENCE = """
SELECT c.hold_id, c.seq AS capture_seq, a.seq AS authorize_seq
FROM ledger c
LEFT JOIN ledger a
  ON a.hold_id = c.hold_id AND a.entry_type = 'AUTHORIZE' AND a.seq < c.seq
WHERE c.entry_type = 'CAPTURE' AND a.seq IS NULL
"""

P1_SINGLE_CAPTURE = """
SELECT hold_id, count(*) AS captures
FROM ledger WHERE entry_type = 'CAPTURE' AND hold_id IS NOT NULL
GROUP BY hold_id HAVING count(*) > 1
"""

P2_BUDGET_SAFETY = """
SELECT scope_path, window_kind, window_start,
       spent_micros, reserved_micros, cap_micros
FROM budget_window
WHERE spent_micros + reserved_micros > cap_micros
"""

P3_CONSERVATION = """
SELECT hold_id, state, held_micros, captured_micros
FROM hold
WHERE (captured_micros IS NOT NULL AND captured_micros > held_micros)
   OR (state = 'PENDING' AND resolved_at IS NOT NULL)
"""

P3_NO_NEGATIVE_RESERVATION = """
SELECT scope_path, window_kind, reserved_micros
FROM budget_window WHERE reserved_micros < 0
"""

RESERVED_MATCHES_PENDING_HOLDS = """
SELECT w.scope_path, w.window_kind, w.reserved_micros,
       COALESCE(h.pending_micros, 0) AS pending_micros
FROM budget_window w
LEFT JOIN (
    SELECT d->>'scope_path' AS scope_path,
           d->>'window_kind' AS window_kind,
           d->>'window_start' AS window_start,
           sum(held_micros) AS pending_micros
    FROM hold, jsonb_array_elements(debited) AS d
    WHERE state = 'PENDING'
    GROUP BY 1, 2, 3
) h
  ON h.scope_path = w.scope_path
 AND h.window_kind = w.window_kind::text
 AND h.window_start = to_char(w.window_start AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS.US+00:00')
WHERE w.reserved_micros <> COALESCE(h.pending_micros, 0)
"""

ALL_CHECKS: dict[str, str] = {
    "P1 every capture follows an authorize": P1_AUTHORIZATION_PRECEDENCE,
    "P1 at most one capture per hold": P1_SINGLE_CAPTURE,
    "P2 spent plus reserved never exceeds cap": P2_BUDGET_SAFETY,
    "P3 captured never exceeds held": P3_CONSERVATION,
    "P3 reservations never go negative": P3_NO_NEGATIVE_RESERVATION,
}


def violations(conn: Connection[Any]) -> dict[str, list[dict[str, Any]]]:
    """Every property that currently fails, with the offending rows."""
    found: dict[str, list[dict[str, Any]]] = {}
    for name, sql in ALL_CHECKS.items():
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(sql)
        rows = list(cur.fetchall())
        if rows:
            found[name] = rows
    return found


def assert_healthy(conn: Connection[Any]) -> None:
    found = violations(conn)
    if found:
        report = "\n".join(f"  {name}: {rows}" for name, rows in found.items())
        raise AssertionError(f"invariant violation\n{report}")
