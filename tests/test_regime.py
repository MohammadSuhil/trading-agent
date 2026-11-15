"""Index regime rules and the REG-01 bear block (ADR-0004)."""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest
from ta_core.enums import Regime
from ta_quant.regime import INDEX_BEAR, LOOSEN_AFTER, REGIME_INDEX, regime_on, regime_series


def _index(closes: list[float]) -> pl.DataFrame:
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(len(closes))]
    return pl.DataFrame({
        "d": days, "index_name": [REGIME_INDEX] * len(closes), "close": closes,
        "high": [c * 1.003 for c in closes], "low": [c * 0.997 for c in closes],
    })


def test_steady_uptrend_is_bull_and_steady_downtrend_is_bear() -> None:
    up = regime_series(_index([100 * 1.001 ** i for i in range(260)]))
    dn = regime_series(_index([100 * 0.999 ** i for i in range(260)]))
    assert up["regime"][-1] == Regime.TREND_UP.value
    assert dn["regime"][-1] == Regime.TREND_DOWN.value


def test_warm_up_is_unknown_never_a_guess() -> None:
    s = regime_series(_index([100.0 + i for i in range(150)]))
    assert set(s["regime"].to_list()) == {Regime.UNKNOWN.value}


def test_regime_at_d_uses_only_bars_up_to_d() -> None:
    """Live computes the series on bars up to d; the backtest computes it once
    over everything and looks up d. Both must give the same answer."""
    closes = [100 * 1.001 ** i for i in range(230)] + [123 * 0.995 ** i for i in range(80)]
    bars = _index(closes)
    full = regime_series(bars)
    for d in bars["d"].to_list()[200::9]:
        live = regime_series(bars.filter(pl.col("d") <= d))
        assert regime_on(full, d)[0] == regime_on(live, d)[0], d


def test_held_regime_is_never_looser_than_the_raw_call() -> None:
    """RISK-26 on real series output: hysteresis may only ever hold a STRICTER state."""
    from ta_quant.regime import _SEVERITY

    closes = ([100 * 1.001 ** i for i in range(230)] + [123 * 0.993 ** i for i in range(40)]
              + [93 * 1.004 ** i for i in range(60)])
    s = regime_series(_index(closes))
    for r in s.filter(pl.col("regime") != pl.col("raw_regime")).iter_rows(named=True):
        assert _SEVERITY[Regime(r["regime"])] > _SEVERITY[Regime(r["raw_regime"])], r
    # and the exact hold length is pinned by apply_hysteresis's doctests
    assert LOOSEN_AFTER == 3


def _ch_up() -> bool:
    try:
        from ta_datafeed.store import GuardedClient
        with GuardedClient() as ch:
            return ch.query_admin("SELECT uniqExact(d) AS n FROM md.bars_1d")["n"][0] > 300
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _ch_up(), reason="needs ClickHouse with >300 sessions of bars")
def test_bear_block_opens_no_new_position() -> None:
    """REG-01: with the index regime forced to BEAR, the live rules never buy.

    Run on real bars so the candidates are ones that DO clear the expectancy
    gate on some days; the frozen-control run over the same window proves it.
    """
    from datetime import UTC, datetime

    from ta_core.asof import AsOf
    from ta_core.enums import Arm
    from ta_datafeed.store import GuardedClient
    from ta_quant.backtest.engine import run_backtest
    from ta_quant.nightly import latest_session, load_panel_inputs

    with GuardedClient() as ch:
        d = latest_session(ch)
        bars, members = load_panel_inputs(ch, d, a=AsOf(datetime.now(UTC)), lookback=420)
    n500 = members.filter(pl.col("index_name") == "NIFTY500")["symbol"].to_list()
    bars = bars.filter(pl.col("symbol").is_in(n500))
    days = sorted(bars["d"].unique().to_list())
    start = days[-150]
    bear = pl.DataFrame({"d": days, "regime": [Regime.TREND_DOWN.value] * len(days),
                         "note": ["forced bear"] * len(days)})

    control = run_backtest(bars, members, start_date=start, end_date=d, arm=Arm.FROZEN_CONTROL)
    blocked = run_backtest(bars, members, start_date=start, end_date=d, arm=Arm.DETERMINISTIC,
                           index_regime=bear, entry_block=INDEX_BEAR)
    assert len(control.outcomes) > 0, "window has no qualifying trades; test proves nothing"
    assert len(blocked.outcomes) == 0
    assert all(abs(e - 500_000.0) < 1e-6 for e in
               (blocked.equity_curve["equity"] if isinstance(blocked.equity_curve, pl.DataFrame)
                else [r["equity"] for r in blocked.equity_curve]))


@pytest.mark.skipif(not _ch_up(), reason="needs ClickHouse with >300 sessions of bars")
def test_live_pipeline_bear_block_downgrades_every_buy(monkeypatch) -> None:
    """REG-01 in pipeline.run_nightly itself (the engine has its own copy, tested above).

    Recent sessions rarely clear the 0.05 ATR expectancy gate, so a test that
    waited for a natural BUY would be vacuous. Lowering the gate guarantees
    candidates qualify; the test first PROVES BUYs appear, then that a forced
    BEAR index regime removes every one of them -- no BUY, no size, REG-01
    stated on the card. Real bars (2026-11-09: membership is known then).
    """
    from datetime import date as _date

    from ta_datafeed.store import GuardedClient
    from ta_ledger.risk import Portfolio
    from ta_quant import pipeline
    from ta_quant.nightly import as_of_for, load_panel_inputs

    monkeypatch.setattr(pipeline, "MIN_EXPECTANCY_ATR", -10.0)
    d = _date(2026, 11, 9)
    with GuardedClient() as ch:
        bars, members = load_panel_inputs(ch, d, a=as_of_for(d))
    pf = Portfolio(equity=500_000.0, cash=500_000.0, watermark=500_000.0)

    free = pipeline.run_nightly(bars, members, d=d, portfolio=pf)
    assert free.recommendations.filter(pl.col("action") == "BUY").height > 0, "gate not opened; test proves nothing"

    days = sorted(bars["d"].unique().to_list())
    bear = pl.DataFrame({"d": days, "regime": [Regime.TREND_DOWN.value] * len(days),
                         "note": ["forced bear"] * len(days)})
    blocked = pipeline.run_nightly(bars, members, d=d, portfolio=pf, index_regime=bear, entry_block=INDEX_BEAR)
    recs = blocked.recommendations
    # The block vetoes entries only: the caps (and the reported regime) are still
    # the breadth regime's -- the earlier REG-01 also swapped them, so a test of
    # it changed two things at once.
    assert blocked.regime is free.regime
    assert recs.filter(pl.col("action") == "BUY").height == 0
    assert (recs["qty"] == 0).all() and (recs["notional"] == 0).all()
    assert recs["barrier_reason"].str.contains("ENTRY-BLOCK").any()

    # A BREADTH block keyed on the regime the breadth heuristic reports today
    # must block too -- the same EntryBlock path, the other source.
    from ta_quant.regime import EntryBlock
    breadth_block = EntryBlock("BREADTH", frozenset({free.regime}), "test: today's breadth regime")
    b2 = pipeline.run_nightly(bars, members, d=d, portfolio=pf, entry_block=breadth_block)
    assert b2.recommendations.filter(pl.col("action") == "BUY").height == 0


def test_live_strategy_is_adr_0007_v2_challenger() -> None:
    """The live arm's strategy must be v2 with internal Stage-2 self-gating.

    Changing it is a strategy change: it needs a new ADR and a new test, not an
    edit to one constant. The frozen control never takes a strategy override (I-24).
    """
    from ta_quant import nightly

    assert nightly.LIVE_STRATEGY_VERSION == "v2"
    assert nightly.LIVE_ENTRY_BLOCK is None
