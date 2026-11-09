"""Regressions for the three ways a `knowledge_time` bound can be a lie.

The first version of this guard proved only that a bound EXISTED. These tests
pin the two further failures found on 2026-10-31, both of which passed the
original guard and both of which read the future.
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ta_core.asof import AsOf, PITViolation, assert_pit_bounded, build_pit_query

A = AsOf(datetime(2026, 10, 30, 13, 0, tzinfo=UTC))
REPO = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------- 1. no bound
def test_unbounded_read_is_refused():
    with pytest.raises(PITViolation, match="unbounded"):
        assert_pit_bounded("SELECT * FROM md.bars_1d WHERE symbol = 'RELIANCE'")


# ------------------------------------------- 2. a bound evaluated at runtime
@pytest.mark.parametrize("rhs", [
    "now()", "now64()", "today()", "yesterday()", "currentDate()",
    "CURRENT_TIMESTAMP", "toDate(now())", "d",
])
def test_a_volatile_bound_is_refused(rhs: str):
    """`knowledge_time <= now()` moves with the wall clock, so replaying a
    past day reads everything learned since. It is syntactically
    indistinguishable from a correct query, which is what made it dangerous."""
    with pytest.raises(PITViolation, match="literal as-of"):
        assert_pit_bounded(f"SELECT * FROM md.bars_1d WHERE knowledge_time <= {rhs}")


def test_a_literal_bound_is_accepted():
    assert_pit_bounded(
        "SELECT * FROM md.bars_1d WHERE knowledge_time <= '2026-10-30 13:00:00'"
    )


def test_a_driver_bound_parameter_is_accepted():
    """A placeholder is bound by the driver and cannot be checked statically,
    so it is trusted rather than refused -- refusing it would push callers
    toward string interpolation, which is strictly worse."""
    assert_pit_bounded(
        "SELECT * FROM md.bars_1d WHERE knowledge_time <= %(as_of)s"
    )


# --------------------------------------------- 3. a bound to the WRONG instant
def test_a_stale_literal_is_refused_when_the_as_of_is_known():
    """A constant copied from another query passes every check above."""
    with pytest.raises(PITViolation, match="!= as_of"):
        assert_pit_bounded(
            "SELECT * FROM md.bars_1d WHERE knowledge_time <= '2026-10-31 09:00:00'",
            A,
        )


def test_build_pit_query_output_always_satisfies_its_own_guard():
    for table in ("md.bars_1d", "md.trend_1d", "ev.recommendations"):
        assert_pit_bounded(build_pit_query(table, A), A)


# ------------------------------------------------- joins and multiple tables
def test_a_join_bounding_only_one_side_is_refused():
    with pytest.raises(PITViolation, match="nx.news_features"):
        assert_pit_bounded(
            "SELECT * FROM md.bars_1d b JOIN nx.news_features n ON n.sym = b.sym "
            "WHERE b.knowledge_time <= '2026-10-30 13:00:00'"
        )


def test_a_fully_bounded_join_is_accepted():
    assert_pit_bounded(
        "SELECT * FROM md.bars_1d b JOIN nx.news_features n ON n.sym = b.sym "
        "WHERE b.knowledge_time <= '2026-10-30 13:00:00' "
        "AND n.knowledge_time <= '2026-10-30 13:00:00'"
    )


def test_non_pit_tables_pass_through_unbounded():
    assert_pit_bounded("SELECT * FROM ref.symbols")


# ------------------------------------------------- the escape hatch is bounded
def test_query_admin_is_not_used_in_decision_code():
    """`query_admin` is the deliberate unguarded escape hatch for operational
    questions ("how many rows?"). It must never appear in code that makes or
    serves a decision, or the guard is decorative."""
    forbidden = ["packages/quant", "packages/llm", "packages/ledger", "apps/api"]
    present = [d for d in forbidden if (REPO / d).is_dir()]
    hits = subprocess.run(
        ["grep", "-rn", "query_admin", *present],
        cwd=REPO, capture_output=True, text=True, check=False,
    ).stdout.strip()
    assert not hits, f"query_admin used in decision code:\n{hits}"


def test_every_pit_table_in_the_schema_is_registered_in_the_guard():
    """A table with a knowledge_time column that nobody registered is read
    unbounded forever and nothing complains."""
    from ta_core.asof import PIT_TABLES

    declared: set[str] = set()
    for sql_file in (REPO / "infra" / "clickhouse").glob("schema*.sql"):
        current: str | None = None
        for line in sql_file.read_text().splitlines():
            s = line.strip()
            if s.upper().startswith("CREATE TABLE"):
                current = s.split()[-2] if s.endswith("(") else s.split()[-1]
                current = current.rstrip("(").strip()
            elif current and s.startswith("knowledge_time"):
                declared.add(current.lower())
                current = None
            elif s.startswith(")"):
                current = None

    missing = declared - set(PIT_TABLES)
    assert not missing, (
        f"these tables carry knowledge_time but are not in PIT_TABLES, so reads "
        f"of them are unguarded: {sorted(missing)}"
    )
