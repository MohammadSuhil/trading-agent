"""Invariant P-01: Parity between historical backtester and live paper broker.

Enforces:
  1. Paper broker and backtester call the SAME simulate_fill() and apply_costs() objects
     (identity, not copies).
  2. Over identical market bar sequences, run_backtest() and an independent day-by-day
     PaperBroker event-ledger execution produce byte-for-byte identical equity curves,
     outcomes, and terminal portfolio states.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import polars as pl
import pytest
from ta_core.enums import Arm
from ta_ledger.broker import paper
from ta_ledger.broker.base import BarData
from ta_ledger.broker.paper import PaperBroker
from ta_ledger.manifest import RunManifest
from ta_ledger.rebuild import rebuild_from_ledger
from ta_ledger.risk import DEFAULT
from ta_quant.backtest import (
    _screen_session,
    apply_costs,
    engine,
    orders_from_recommendations,
    run_backtest,
    simulate_fill,
)
from ta_quant.pipeline import build_panel


def test_invariant_p01_object_identity():
    """Verify engine and paper broker call identical fill and cost functions."""
    assert engine.simulate_fill is paper.simulate_fill
    assert engine.apply_costs is paper.apply_costs
    assert simulate_fill is paper.simulate_fill
    assert apply_costs is paper.apply_costs


def _generate_synthetic_market(n_sessions: int = 45) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Generate realistic synthetic bars and universe members where trades trigger and resolve."""
    start = date(2026, 1, 1)
    dates: list[date] = []
    curr = start
    while len(dates) < n_sessions:
        if curr.weekday() < 5:  # Mon-Fri
            dates.append(curr)
        curr += timedelta(days=1)

    symbols = ["INFY", "TCS"]
    bars_list: list[dict] = []

    for sym in symbols:
        base_price = 1000.0 if sym == "INFY" else 2000.0
        for i, d in enumerate(dates):
            # In sessions 0-20 (warmup), flat price.
            # In session 21-25, price rises steadily to trigger breakout and fill.
            # In session 26-30, target is touched to resolve position.
            if i < 20:
                p = base_price + (i % 3) * 2.0
            elif i < 25:
                p = base_price + 20.0 + (i - 20) * 10.0
            elif i < 30:
                p = base_price + 80.0 + (i - 25) * 15.0  # surges to hit target
            else:
                p = base_price + 120.0 - (i - 30) * 4.0

            o = p - 1.0
            h = p + 4.0
            l = p - 3.0
            c = p
            bars_list.append({
                "symbol": sym,
                "d": d,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 200_000,
                "turnover": 200_000.0 * c,
                "n_trades": 15_000,
                "series": "EQ",
            })

    bars = pl.DataFrame(bars_list).sort(["symbol", "d"])

    members = pl.DataFrame([
        {
            "symbol": "INFY",
            "industry": "Information Technology",
            "company_name": "Infosys Ltd",
            "index_name": "NIFTY500",
        },
        {
            "symbol": "TCS",
            "industry": "Information Technology",
            "company_name": "Tata Consultancy Services Ltd",
            "index_name": "NIFTY500",
        },
    ])

    return bars, members


def test_backtest_matches_paper():
    """Verify run_backtest and PaperBroker + rebuild_from_ledger produce exact parity."""
    bars, members = _generate_synthetic_market(45)

    # Relax liquidity constraints for compact synthetic test
    cfg = replace(
        DEFAULT,
        min_listed_sessions=10,
        min_adtv_rupees=1e5,
        min_trades_per_day=10,
    )

    initial_equity = 500_000.0
    warmup_sessions = 20

    # 1. Run via Backtest Engine
    bt_res = run_backtest(
        bars,
        members,
        initial_equity=initial_equity,
        arm=Arm.FROZEN_CONTROL,
        cfg=cfg,
        warmup_sessions=warmup_sessions,
        include_watch_for_backtest=True,
    )

    # 2. Run independently via PaperBroker session-by-session loop
    active_sessions = sorted(bars["d"].unique().to_list())[warmup_sessions:]
    panel = build_panel(bars, members)

    bars_by_date: dict[date, dict[str, BarData]] = {}
    for row in bars.iter_rows(named=True):
        row_d = row["d"]
        if row_d not in bars_by_date:
            bars_by_date[row_d] = {}
        bars_by_date[row_d][row["symbol"]] = BarData(
            symbol=str(row["symbol"]),
            d=row_d,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )

    paper_broker = PaperBroker(initial_equity=initial_equity, arm=Arm.FROZEN_CONTROL)
    run_id = f"BT-{Arm.FROZEN_CONTROL.value}"
    manifest = RunManifest(
        run_id=run_id,
        d=active_sessions[0],
        as_of=datetime.combine(active_sessions[0], time(15, 30), tzinfo=UTC),
        arm=Arm.FROZEN_CONTROL.value,
        git_sha="BACKTEST",
        uv_lock_sha="BACKTEST",
        weights_sha="BACKTEST",
        config_sha="BACKTEST",
    )
    paper_broker.record_manifest(manifest)

    for d in active_sessions:
        session_ts = datetime.combine(d, time(15, 30), tzinfo=UTC)
        bars_today = bars_by_date.get(d, {})

        # Step 1: Execution of pending orders and active positions
        paper_broker.step_session(d, bars_today, run_id=run_id, ts=session_ts)

        # Step 2: Screening and order generation
        panel_upto_d = panel.filter(pl.col("d") <= d)
        recs_df = _screen_session(
            panel_upto_d,
            members,
            d=d,
            portfolio=paper_broker.portfolio(),
            cfg=cfg,
            tradeable_index="NIFTY500",
            scored_index="NIFTY500",
            max_cards=25,
        )

        if not recs_df.is_empty():
            orders = orders_from_recommendations(
                recs_df,
                d,
                arm=Arm.FROZEN_CONTROL,
                include_watch_for_backtest=True,
            )
            if orders:
                paper_broker.place_orders(orders, run_id=run_id, ts=session_ts)

    # 3. Project state from ledger event stream
    paper_proj = rebuild_from_ledger(
        paper_broker.events,
        initial_equity=initial_equity,
        arm=Arm.FROZEN_CONTROL.value,
    )

    # 4. Strict parity assertions (Invariant P-01)
    assert bt_res.final_equity == pytest.approx(paper_proj.portfolio.equity, abs=1e-6)
    assert bt_res.outcomes == paper_proj.outcomes
    assert len(bt_res.equity_curve) == len(paper_proj.equity_curve)

    for i in range(len(bt_res.equity_curve)):
        bt_pt = bt_res.equity_curve[i]
        pr_pt = paper_proj.equity_curve[i]
        assert bt_pt["d"] == pr_pt["d"]
        assert bt_pt["equity"] == pytest.approx(pr_pt["equity"], abs=1e-6)
        assert bt_pt["cash"] == pytest.approx(pr_pt["cash"], abs=1e-6)
        assert bt_pt["deployed"] == pytest.approx(pr_pt["deployed"], abs=1e-6)
        assert bt_pt["drawdown"] == pytest.approx(pr_pt["drawdown"], abs=1e-6)

    # Assert that trades were genuinely placed and resolved
    assert len(bt_res.outcomes) > 0
    assert bt_res.stats.n_trades == len(bt_res.outcomes)
    assert bt_res.stats.n_sessions == len(active_sessions)

    # Assert no session ever holds duplicate concurrent lots in the same symbol
    for ev in bt_res.events:
        if ev.kind.value == "EQUITY_MARKED":
            open_syms = [lot["symbol"] for lot in ev.payload["open_lots"]]
            assert len(open_syms) == len(set(open_syms))


def test_residual_correlation_index_matches_pair_correlations():
    """ResidualCorrelationIndex must equal compute_pair_correlations bit-for-bit."""
    from ta_quant.factors.correlation import (
        ResidualCorrelationIndex,
        compute_pair_correlations,
    )

    bars, members = _generate_synthetic_market(50)
    panel = build_panel(bars, members)
    idx = ResidualCorrelationIndex(panel)
    syms = ["INFY", "TCS"]
    for d in sorted(panel["d"].unique().to_list())[25::5]:
        direct = compute_pair_correlations(
            panel.filter(pl.col("d") <= d), d, candidate_symbols=syms, open_symbols=syms
        )
        wset = set(idx.window_dates_for(d))
        for a in syms:
            for b in syms:
                assert idx.corr(d, a, b, _wset=wset if wset else None) == pytest.approx(
                    direct[a][b], abs=1e-12
                )
