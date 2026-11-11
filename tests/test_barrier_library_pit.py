"""The backtest's once-built path library must not see past the session.

`run_backtest` builds the triple-barrier library ONCE over the whole panel and
filters it per session. Filtering on the path's START date admits the last
~h starts, whose outcomes are decided by bars after the session -- the
backtest then trades on base rates the live nightly could never have had, and
the anti-drift invariant (backtest == paper) breaks silently. The filter must
be on `exit_d`, and it must reproduce exactly the library the live nightly
builds from a panel that ends on that session.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import polars as pl

from ta_core.enums import Horizon
from ta_quant.barriers import build_path_library


def _panel(n: int = 80) -> pl.DataFrame:
    rng = random.Random(7)
    rows = []
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    for sym in ("AAA", "BBB", "CCC"):
        px = 100.0
        for d in days:
            px *= 1 + rng.gauss(0, 0.02)
            rows.append({"symbol": sym, "d": d, "close": px,
                         "high": px * 1.01, "low": px * 0.99, "atr14": px * 0.02})
    return pl.DataFrame(rows)


def test_backtest_library_filter_equals_live_library() -> None:
    panel = _panel()
    full = build_path_library(panel, horizon=Horizon.SWING)
    for session in sorted(panel["d"].unique().to_list())[30::7]:
        live = build_path_library(panel.filter(pl.col("d") <= session), horizon=Horizon.SWING)
        bt = full.filter(pl.col("exit_d") <= session)
        key = ["symbol", "d"]
        assert bt.select(key + ["outcome"]).sort(key).equals(
            live.select(key + ["outcome"]).sort(key)
        ), session


def test_start_date_filter_would_leak() -> None:
    """Guards the guard: the old `d < session` filter really does admit future-decided paths."""
    panel = _panel()
    full = build_path_library(panel, horizon=Horizon.SWING)
    session = sorted(panel["d"].unique().to_list())[40]
    leaked = full.filter((pl.col("d") < session) & (pl.col("exit_d") > session))
    assert leaked.height > 0


def test_base_rates_do_not_depend_on_how_much_history_is_loaded() -> None:
    """ADR-0003: backfilling older bars must not change what a session learns from.

    With an expanding library, loading 580 sessions instead of 420 flipped the
    frozen control from 95 trades to 0. With the fixed window, trimming older
    history (beyond window + horizon) leaves the known paths identical.
    """
    from ta_quant.barriers import known_paths

    panel = _panel(n=120)
    days = sorted(panel["d"].unique().to_list())
    session, window = days[-5], 30
    full = known_paths(build_path_library(panel, horizon=Horizon.SWING), session, window=window)
    trimmed_panel = panel.filter(pl.col("d") >= days[40])
    trimmed = known_paths(build_path_library(trimmed_panel, horizon=Horizon.SWING), session, window=window)
    key = ["symbol", "d"]
    assert full.height > 0
    assert full.select(key + ["outcome"]).sort(key).equals(trimmed.select(key + ["outcome"]).sort(key))


def test_live_and_backtest_share_one_cut() -> None:
    """The backtest filters a whole-panel library; live builds from a panel ending at d. Same paths."""
    from ta_quant.barriers import known_paths

    panel = _panel(n=120)
    full = build_path_library(panel, horizon=Horizon.SWING)
    for session in sorted(panel["d"].unique().to_list())[60::11]:
        live = known_paths(build_path_library(panel.filter(pl.col("d") <= session), horizon=Horizon.SWING),
                           session, window=30)
        bt = known_paths(full, session, window=30)
        key = ["symbol", "d"]
        assert bt.select(key).sort(key).equals(live.select(key).sort(key)), session
