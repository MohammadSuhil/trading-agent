"""Invariant P-06: Replay re-derives from record; it never recalls a stored answer.

Verifies that:
  1. Replay executes the actual decision pipeline on PIT-bounded historical inputs.
  2. Byte-for-byte exact matches succeed cleanly.
  3. Any regression in configuration, weights SHA, git SHA, or recommendation geometry
     raises ReplayDivergenceError detailing the divergent column and symbol.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from ta_ledger.manifest import RunManifest
from ta_ledger.replay import ReplayDivergenceError, replay_nightly_from_record
from ta_ledger.risk import DEFAULT, Portfolio
from ta_quant.pipeline import run_nightly


def _build_test_market(n_sessions: int = 35) -> tuple[pl.DataFrame, pl.DataFrame, date, datetime]:
    start = date(2026, 1, 1)
    dates: list[date] = []
    curr = start
    while len(dates) < n_sessions:
        if curr.weekday() < 5:
            dates.append(curr)
        curr += timedelta(days=1)

    decision_d = dates[-1]
    as_of = datetime.combine(decision_d, datetime.min.time(), tzinfo=UTC) + timedelta(hours=15, minutes=30)

    symbols = ["INFY", "TCS"]
    bars_list: list[dict] = []
    for sym in symbols:
        base = 1500.0 if sym == "INFY" else 3000.0
        for i, d in enumerate(dates):
            p = base + i * 5.0
            kt = datetime.combine(d, datetime.min.time(), tzinfo=UTC) + timedelta(hours=13)
            bars_list.append({
                "symbol": sym,
                "d": d,
                "open": p - 2.0,
                "high": p + 8.0,
                "low": p - 5.0,
                "close": p,
                "volume": 100_000,
                "turnover": 100_000.0 * p,
                "n_trades": 10_000,
                "series": "EQ",
                "knowledge_time": kt,
            })

    bars = pl.DataFrame(bars_list).sort(["symbol", "d"])

    members = pl.DataFrame([
        {
            "symbol": "INFY",
            "industry": "Information Technology",
            "company_name": "Infosys Ltd",
            "index_name": "NIFTY500",
            "d": decision_d,
            "knowledge_time": as_of,
        },
        {
            "symbol": "TCS",
            "industry": "Information Technology",
            "company_name": "Tata Consultancy Services Ltd",
            "index_name": "NIFTY500",
            "d": decision_d,
            "knowledge_time": as_of,
        },
    ])

    return bars, members, decision_d, as_of


def test_replay_rederives_and_catches_regression():
    """Verify replay re-executes pipeline from inputs and catches all divergences."""
    bars, members, decision_d, as_of = _build_test_market(35)
    portfolio = Portfolio(equity=500_000.0, cash=500_000.0, watermark=500_000.0)

    cfg = replace(
        DEFAULT,
        min_listed_sessions=10,
        min_adtv_rupees=1e5,
        min_trades_per_day=10,
    )

    # 1. Generate baseline live recommendation run
    baseline = run_nightly(
        bars,
        members,
        d=decision_d,
        portfolio=portfolio,
        cfg=cfg,
        as_of=as_of,
    )
    assert not baseline.recommendations.is_empty(), "Baseline must produce recommendations"

    manifest = RunManifest(
        run_id=baseline.run_id,
        d=decision_d,
        as_of=as_of,
        arm="DETERMINISTIC",
        git_sha="git_sha_abc123",
        uv_lock_sha="uv_sha_xyz456",
        weights_sha="weights_sha_789",
        config_sha="config_sha_000",
    )

    # 2. Perfect replay: should re-derive identical recommendations
    replayed = replay_nightly_from_record(
        bars=bars,
        members=members,
        manifest=manifest,
        recorded_recommendations=baseline.recommendations,
        portfolio=portfolio,
        cfg=cfg,
        current_weights_sha="weights_sha_789",
        current_git_sha="git_sha_abc123",
    )
    assert replayed.recommendations.height == baseline.recommendations.height
    assert replayed.recommendations["symbol"].to_list() == baseline.recommendations["symbol"].to_list()

    # 3. Catches tampered recommendation (stop shifted by 0.50)
    first_sym = baseline.recommendations["symbol"][0]
    tampered_recs = baseline.recommendations.with_columns(
        pl.when(pl.col("symbol") == first_sym)
        .then(pl.col("stop") + 0.50)
        .otherwise(pl.col("stop"))
        .alias("stop")
    )
    with pytest.raises(ReplayDivergenceError, match=f"Divergence in symbol {first_sym} column stop"):
        replay_nightly_from_record(
            bars=bars,
            members=members,
            manifest=manifest,
            recorded_recommendations=tampered_recs,
            portfolio=portfolio,
            cfg=cfg,
            current_weights_sha="weights_sha_789",
            current_git_sha="git_sha_abc123",
        )

    # 4. Catches mutated config (position cap / risk per trade altered)
    cfg_mutated = replace(cfg, max_notional=55_000.0, risk_per_trade_pct=0.001)
    with pytest.raises(ReplayDivergenceError):
        replay_nightly_from_record(
            bars=bars,
            members=members,
            manifest=manifest,
            recorded_recommendations=baseline.recommendations,
            portfolio=portfolio,
            cfg=cfg_mutated,
            current_weights_sha="weights_sha_789",
            current_git_sha="git_sha_abc123",
        )

    # 5. Catches weights_sha drift
    with pytest.raises(ReplayDivergenceError, match="weights_sha drift"):
        replay_nightly_from_record(
            bars=bars,
            members=members,
            manifest=manifest,
            recorded_recommendations=baseline.recommendations,
            portfolio=portfolio,
            cfg=cfg,
            current_weights_sha="drifted_weights_sha",
            current_git_sha="git_sha_abc123",
        )

    # 6. Catches git_sha drift
    with pytest.raises(ReplayDivergenceError, match="git_sha drift"):
        replay_nightly_from_record(
            bars=bars,
            members=members,
            manifest=manifest,
            recorded_recommendations=baseline.recommendations,
            portfolio=portfolio,
            cfg=cfg,
            current_weights_sha="weights_sha_789",
            current_git_sha="drifted_git_sha",
        )

    # 7. Catches row count mismatch
    truncated_recs = baseline.recommendations.slice(0, baseline.recommendations.height - 1)
    with pytest.raises(ReplayDivergenceError, match="Row count mismatch"):
        replay_nightly_from_record(
            bars=bars,
            members=members,
            manifest=manifest,
            recorded_recommendations=truncated_recs,
            portfolio=portfolio,
            cfg=cfg,
            current_weights_sha="weights_sha_789",
            current_git_sha="git_sha_abc123",
        )
