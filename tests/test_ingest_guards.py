"""Archive files must land on the session that was asked for (I-26), and report-only
variant backtests must never write the control's curve or the paper book (I-28)."""
from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest
from ta_datafeed.parse.index_close import parse_index_close
from ta_datafeed.parse.udiff import BhavcopyFormatError
from ta_datafeed.sources.nse_archive import _same_session

_IDX = (b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
        b"Closing Index Value\n")


def test_bar_file_dated_another_day_is_rejected() -> None:
    """The year-20-AD bug: a parse that lands rows on the wrong date must fail loudly."""
    bars = pl.DataFrame({"d": [date(2020, 7, 13), date(20, 7, 13)], "symbol": ["A", "B"]})
    with pytest.raises(BhavcopyFormatError):
        _same_session(bars, date(2020, 7, 13))
    ok = bars.head(1)
    assert _same_session(ok, date(2020, 7, 13)) is ok


def test_month_first_index_file_lands_on_the_requested_session() -> None:
    blob = _IDX + b"Nifty 500,04-10-2023,1,2,0.5,1.5\n"   # 10 April written month-first
    kt = datetime(2023, 4, 10, 13, tzinfo=UTC)
    assert parse_index_close(blob, knowledge_time=kt, session=date(2023, 4, 10))["d"].to_list() == [date(2023, 4, 10)]
    # The same bytes asked for as 4 October are equally unambiguous.
    assert parse_index_close(blob, knowledge_time=kt, session=date(2023, 10, 4))["d"].to_list() == [date(2023, 10, 4)]


def test_index_file_matching_no_reading_is_rejected() -> None:
    blob = _IDX + b"Nifty 500,04-10-2023,1,2,0.5,1.5\n"
    with pytest.raises(ValueError):
        parse_index_close(blob, knowledge_time=datetime(2023, 4, 11, tzinfo=UTC), session=date(2023, 4, 11))


@pytest.fixture
def _cleanup_test_runs():
    """These tests write REAL rows into ev.backtest_runs (that is what they
    test). Remove them afterwards -- pass or fail -- so a test run never shows
    up on the Calibration page as the owner's "last run" of a window."""
    yield
    from ta_datafeed.store import GuardedClient
    with GuardedClient() as ch:
        ch.command("ALTER TABLE ev.backtest_runs DELETE WHERE run_id LIKE 'TEST-%' SETTINGS mutations_sync = 1")


def _ch_up() -> bool:
    try:
        from ta_datafeed.store import GuardedClient
        with GuardedClient() as ch:
            return ch.query_admin("SELECT uniqExact(d) AS n FROM md.bars_1d")["n"][0] > 300
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _ch_up(), reason="needs ClickHouse with >300 sessions of bars")
def test_regime_filter_backtest_never_touches_control_or_paper_book(_cleanup_test_runs) -> None:
    from ta_datafeed.store import GuardedClient
    from ta_quant.nightly import execute_backtest

    snap = ("SELECT arm, count() AS n, sum(equity) AS s FROM ev.equity_daily GROUP BY arm ORDER BY arm")
    outs = "SELECT arm, count() AS n FROM ev.outcomes WHERE knowledge_time <= '2099-01-01 00:00:00' GROUP BY arm ORDER BY arm"
    with GuardedClient() as ch:
        before_eq, before_out = ch.query_admin(snap), ch.query_admin(outs)
        n_variant = "SELECT count() AS n FROM ev.backtest_runs WHERE line = 'REGIME_FILTER'"
        before_variants = ch.query_admin(n_variant)["n"][0]
    execute_backtest(sessions=63, regime_filter=True, run_id="TEST-I28")
    with GuardedClient() as ch:
        assert ch.query_admin(snap).equals(before_eq)
        assert ch.query_admin(outs).equals(before_out)
        assert ch.query_admin(n_variant)["n"][0] == before_variants + 1


@pytest.mark.skipif(not _ch_up(), reason="needs ClickHouse with >300 sessions of bars")
def test_each_window_keeps_its_own_last_run(_cleanup_test_runs) -> None:
    """Running one window must not replace another window's stored result.

    The owner's requirement: run 63, then 126 -> selecting 63 still shows the
    63-session run, until 63 is run again.
    """
    from fastapi.testclient import TestClient
    from ta_api.main import app
    from ta_quant.nightly import execute_backtest

    execute_backtest(sessions=63, persist_results=True, run_id="TEST-W63")
    execute_backtest(sessions=126, persist_results=True, run_id="TEST-W126")
    c = TestClient(app)
    w63, w126 = c.get("/api/backtest?sessions=63").json(), c.get("/api/backtest?sessions=126").json()
    assert w63["run"]["run_id"] == "TEST-W63" and w63["sessions"] == 63 and w63["n_sessions"] == 63
    assert w126["run"]["run_id"] == "TEST-W126" and w126["n_sessions"] == 126
    # I-30: every stored run carries its benchmark, consistent with its own return.
    b = w126["benchmark"]
    assert b["index"] == "Nifty 500" and b["return_pct"] is not None
    assert abs((w126["total_return_pct"] - b["return_pct"]) - b["excess_return_pct"]) < 1e-5
    assert {w["sessions"] for w in w63["windows"]} >= {63, 126}
    # Simulation follows the window too (both refuse: < 750 sessions).
    assert c.get("/api/simulation?sessions=63").json()["n_days"] == 63
    # A window never run is reported as such, not filled from another window.
    never = c.get("/api/backtest?sessions=999999").json()
    assert never["state"] == "NOT_RUN" and never["equity_curve"] == []
