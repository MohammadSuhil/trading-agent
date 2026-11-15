"""Unit and ledger-parity tests for V2 Challenger Strategy (ADR-0007)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import polars as pl
import pytest
from ta_core.enums import Arm, Regime
from ta_ledger.rebuild import rebuild_from_ledger
from ta_ledger.risk import DEFAULT, Portfolio
from ta_quant.challenger import (
    monthly_rebalance_dates,
    run_backtest_v2,
    score_universe_v2,
    screen_session_v2,
)
from ta_quant.regime import EntryBlock
from ta_quant.weights import load_challenger_weights


def test_monthly_rebalance_dates_selects_first_session_per_month() -> None:
    sessions = [
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 2, 2),
        date(2026, 2, 3),
        date(2026, 3, 1),
    ]
    assert monthly_rebalance_dates(sessions) == {
        date(2026, 1, 5),
        date(2026, 2, 2),
        date(2026, 3, 1),
    }
    assert monthly_rebalance_dates([]) == set()


def test_score_universe_v2_filters_and_ranks_sortino_momentum() -> None:
    w = load_challenger_weights()
    df = pl.DataFrame([
        {
            "symbol": "SMOOTH", "d": date(2026, 1, 5), "close": 1500.0,
            "series": "EQ", "adtv_20": 8e7, "adtv_126": 8e7, "n_trades": 20_000,
            "bars_seen": 260, "mom_12_1": 0.30, "ret_126d": 0.22,
            "dvol_252": 0.11, "dist_52w_high": -0.02, "company_name": "Smooth Ltd",
        },
        {
            "symbol": "NOISY", "d": date(2026, 1, 5), "close": 1500.0,
            "series": "EQ", "adtv_20": 8e7, "adtv_126": 8e7, "n_trades": 20_000,
            "bars_seen": 260, "mom_12_1": 0.15, "ret_126d": 0.10,
            "dvol_252": 0.35, "dist_52w_high": -0.15, "company_name": "Noisy Ltd",
        },
        {
            "symbol": "PENNY", "d": date(2026, 1, 5), "close": 12.0,
            "series": "EQ", "adtv_20": 8e7, "adtv_126": 8e7, "n_trades": 20_000,
            "bars_seen": 260, "mom_12_1": 0.50, "ret_126d": 0.40,
            "dvol_252": 0.10, "dist_52w_high": -0.01, "company_name": "Penny Ltd",
        },
    ])
    scored = score_universe_v2(df, tradeable={"SMOOTH", "NOISY", "PENNY"}, cfg=DEFAULT, weights=w)
    assert scored["symbol"].to_list() == ["SMOOTH", "NOISY"]
    assert scored["rank"].to_list() == [1, 2]


def test_screen_session_v2_watch_cards_carry_zero_size() -> None:
    w = load_challenger_weights()
    day_panel = pl.DataFrame([{
        "symbol": "INFY", "d": date(2026, 1, 5), "close": 1500.0,
        "sma50": 1400.0, "sma200": 1300.0, "atr14": 25.0,
        "series": "EQ", "adtv_20": 9e7, "adtv_126": 9e7, "n_trades": 25_000,
        "bars_seen": 300, "mom_12_1": 0.25, "ret_126d": 0.18,
        "dvol_252": 0.14, "dist_52w_high": -0.02,
        "industry": "Information Technology", "company_name": "Infosys",
    }])
    pf = Portfolio(equity=500_000.0, cash=500_000.0, watermark=500_000.0)
    recs = screen_session_v2(
        day_panel, d=date(2026, 1, 5), portfolio=pf,
        is_rebalance_day=True, entries_blocked=True, block_note="forced bear", weights=w,
    )
    assert recs.height == 1
    row = recs.row(0, named=True)
    assert row["action"] == "WATCH"
    assert row["qty"] == 0 and row["notional"] == 0.0
    assert "ENTRY-BLOCK" in row["barrier_reason"]


def test_run_backtest_v2_ledger_rebuild_parity_and_entry_block() -> None:
    """Invariant P-01 & P-03: v2 backtest rebuilds identically from hash-chained ledger."""
    dates: list[date] = []
    cur = date(2026, 1, 1)
    while len(dates) < 55:
        if cur.weekday() < 5:
            dates.append(cur)
        cur += timedelta(days=1)

    rows = []
    for sym, mult in (("INFY", 1.004), ("TCS", 1.003), ("RELIANCE", 1.002)):
        px = 500.0
        for d in dates:
            px = round(px * mult, 2)
            rows.append({
                "symbol": sym, "d": d,
                "open": round(px * 0.998, 2), "high": round(px * 1.01, 2),
                "low": round(px * 0.99, 2), "close": px,
                "volume": 500_000, "turnover": 500_000.0 * px,
                "n_trades": 15_000, "series": "EQ",
            })
    bars = pl.DataFrame(rows)
    members = pl.DataFrame([
        {"symbol": "INFY", "industry": "Information Technology", "company_name": "Infosys", "index_name": "NIFTY500"},
        {"symbol": "TCS", "industry": "Information Technology", "company_name": "TCS", "index_name": "NIFTY500"},
        {"symbol": "RELIANCE", "industry": "Oil Gas & Consumable Fuels", "company_name": "Reliance", "index_name": "NIFTY500"},
    ])
    cfg = replace(DEFAULT, min_listed_sessions=15, min_adtv_rupees=1e6, min_trades_per_day=100)

    # 1. Blocked run places 0 trades
    all_block = EntryBlock("BREADTH", frozenset(Regime), "block all regimes")
    blocked = run_backtest_v2(bars, members, cfg=cfg, warmup_sessions=25, entry_block=all_block, use_pit_liquidity=False)
    assert len(blocked.outcomes) == 0
    assert blocked.final_equity == pytest.approx(500_000.0)

    # 2. Active run satisfies full hash-chain verification + rebuild_from_ledger parity + no duplicate lots
    res = run_backtest_v2(bars, members, cfg=cfg, warmup_sessions=25, use_pit_liquidity=False)
    proj = rebuild_from_ledger(res.events, initial_equity=500_000.0, arm=Arm.DETERMINISTIC.value)
    assert res.final_equity == pytest.approx(proj.portfolio.equity, abs=1e-6)
    assert res.outcomes == proj.outcomes
    assert len(res.regimes) == len(res.equity_curve)
    for ev in res.events:
        if ev.kind.value == "EQUITY_MARKED":
            open_syms = [lot["symbol"] for lot in ev.payload["open_lots"]]
            assert len(open_syms) == len(set(open_syms))
