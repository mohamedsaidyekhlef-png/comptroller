from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from comptroller.core.money import Money

psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")

from psycopg import Connection  # noqa: E402

DSN = os.environ.get(
    "COMPTROLLER_DSN",
    "postgresql://comptroller:comptroller@localhost:5433/comptroller",
)
MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "001_reservation.sql"


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _reachable(), reason=f"no Postgres at {DSN}; run docker compose up -d"
)


@pytest.fixture(scope="session")
def schema() -> None:
    if not _reachable():
        pytest.skip("no Postgres")
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        conn.execute(MIGRATION.read_text(encoding="utf-8"))


@pytest.fixture
def db(schema: None) -> Iterator[Connection[Any]]:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE hold, ledger, budget_window, budget_policy")
        yield conn


@pytest.fixture
def budget(db: Connection[Any]) -> Any:
    def configure(scope_path: str, cap: str, kind: str = "DAY", currency: str = "USD") -> None:
        micros = Money.parse(cap, currency).micros
        db.execute(
            """
            INSERT INTO budget_policy (scope_path, window_kind, cap_micros, currency)
            VALUES (%s, %s::window_kind, %s, %s)
            ON CONFLICT (scope_path, window_kind)
            DO UPDATE SET cap_micros = EXCLUDED.cap_micros
            """,
            (scope_path, kind, micros, currency),
        )

    return configure
