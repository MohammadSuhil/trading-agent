"""The leakage suite.

This is the test the whole project leans on. Look-ahead bias does not raise,
does not look wrong, and produces a beautiful equity curve -- so the only
defence is a guard that fails the BUILD, installed before any data arrives.

The decisive test here is `test_guard_catches_a_deliberately_leaky_client`:
we wire a stub database client that "forgets" the as-of bound, exactly as a
hurried human would, and assert the guard refuses it. A guard that has never
been shown to catch a real leak is decoration.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ta_core.asof import (
    AsOf,
    PITViolation,
    assert_pit_bounded,
    build_pit_query,
    is_pit_table,
)

AS_OF = AsOf(datetime(2026, 10, 30, 14, 0, tzinfo=UTC))


# --------------------------------------------------------------------------
# queries that MUST pass
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM md.bars_1d WHERE knowledge_time <= '2026-10-30 14:00:00'",
    "SELECT * FROM md.bars_1d b WHERE b.knowledge_time <= '2026-10-30 14:00:00'",
    "SELECT * FROM md.bars_1d AS b WHERE b.knowledge_time <= '2026-10-30 14:00:00'",
    # non-PIT reference data legitimately has no knowledge_time
    "SELECT sym, name FROM ref.symbols",
    "SELECT * FROM system.parts WHERE active",
    # both sides of a join bounded
    ("SELECT * FROM md.bars_1d b JOIN nx.news_features n ON n.sym = b.sym "
    "WHERE b.knowledge_time <= '2026-10-30 14:00:00' "
    "AND n.knowledge_time <= '2026-10-30 14:00:00'"),
])
def test_bounded_queries_are_accepted(sql: str) -> None:
    assert_pit_bounded(sql)


# --------------------------------------------------------------------------
# queries that MUST fail -- each is a real leakage vector
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sql,why", [
    ("SELECT * FROM md.bars_1d WHERE sym = 'RELIANCE'",
     "plain unbounded read"),
    ("SELECT * FROM md.index_members WHERE idx = 'NIFTY200'",
     "today's membership applied to the past = survivorship bias"),
    ("SELECT * FROM nx.news_features WHERE sym = 'TCS'",
     "news visible before it was disseminated"),
    ("SELECT * FROM md.corporate_actions",
     "unadjusted splits read as a -50% return"),
    ("SELECT * FROM fn.statements WHERE sym = 'INFY'",
     "financials visible before they were filed"),
    (("SELECT * FROM md.bars_1d b JOIN nx.news_features n ON n.sym = b.sym "
     "WHERE b.knowledge_time <= '2026-10-30 14:00:00'"),
     "join bounding only one side -- survives code review easily"),
    ("SELECT b.sym, f.roe FROM md.bars_1d b JOIN fn.ratios f ON f.sym = b.sym",
     "neither side bounded"),
])
def test_unbounded_queries_are_refused(sql: str, why: str) -> None:
    with pytest.raises(PITViolation):
        assert_pit_bounded(sql)


# --------------------------------------------------------------------------
# THE GATE: the guard must catch a realistically-leaky client
# --------------------------------------------------------------------------

class _LeakyClient:
    """A database client that 'forgets' the as-of bound.

    This is not a strawman: it is what you get when someone needs a quick
    number at 19:40 and writes the SQL by hand instead of going through
    `build_pit_query`.
    """

    def __init__(self) -> None:
        self.executed: list[str] = []

    def query(self, sql: str):
        self.executed.append(sql)
        return [("RELIANCE", 1167.4)]


class _GuardedClient(_LeakyClient):
    """The same client with the guard installed -- what production uses."""

    def query(self, sql: str):
        assert_pit_bounded(sql)
        return super().query(sql)


LEAKY_SQL = "SELECT sym, close FROM md.bars_1d WHERE sym = 'RELIANCE'"


def test_leaky_client_really_does_leak() -> None:
    """Sanity: without the guard the bad query sails straight through.

    If this ever fails, the next test proves nothing."""
    client = _LeakyClient()
    assert client.query(LEAKY_SQL) == [("RELIANCE", 1167.4)]
    assert client.executed == [LEAKY_SQL]


def test_guard_catches_a_deliberately_leaky_client() -> None:
    """The M0 gate: the guard refuses the exact query the leaky client ran."""
    client = _GuardedClient()
    with pytest.raises(PITViolation, match="md.bars_1d"):
        client.query(LEAKY_SQL)
    assert client.executed == [], "the query must never reach the database"


def test_builder_output_always_passes_its_own_guard() -> None:
    """Anything built by `build_pit_query` is bounded by construction."""
    for table in ["md.bars_1d", "nx.news_features", "fn.statements", "ref.symbols"]:
        assert_pit_bounded(build_pit_query(table, AS_OF, columns="*"))


def test_every_pit_table_is_actually_guarded() -> None:
    """Each registered PIT table is refused when read unbounded.

    Catches the case where a table is added to PIT_TABLES but the guard's
    pattern fails to match its name shape."""
    from ta_core.asof import PIT_TABLES
    for table in sorted(PIT_TABLES):
        assert is_pit_table(table)
        with pytest.raises(PITViolation):
            assert_pit_bounded(f"SELECT * FROM {table}")


def test_as_of_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        AsOf(datetime(2026, 10, 30, 14, 0))
