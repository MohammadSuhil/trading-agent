from datetime import date

import polars as pl

import ta_quant.pipeline as pipeline
from ta_core.enums import Regime
from ta_ledger.risk import Portfolio


def test_market_regime_uses_nifty500_and_keeps_nifty200_breadth(monkeypatch):
    d = date(2026, 11, 2)
    symbols = ["A", "B", "C", "D", "E"]
    members = pl.DataFrame({
        "index_name": ["NIFTY200", "NIFTY200"] + ["NIFTY500"] * 5,
        "symbol": ["A", "B"] + symbols,
    })
    panel = pl.DataFrame({
        "d": [d] * 5,
        "symbol": symbols,
        "industry": ["Industry"] * 5,
        "close": [90.0, 90.0, 110.0, 110.0, 110.0],
        "sma50": [100.0, 100.0, 100.0, 100.0, 100.0],
        "sma200": [100.0] * 5,
        "ret_1d": [-0.005, -0.004, 0.003, 0.004, 0.005],
        "ret_21d": [0.0] * 5,
        "turnover": [1_000_000.0] * 5,
    })
    empty = pl.DataFrame()
    path_symbols: list[set[str]] = []

    def build_library(bars, **_kwargs):
        path_symbols.append(set(bars["symbol"].to_list()))
        return empty

    monkeypatch.setattr(pipeline, "build_panel", lambda _bars, _members: panel)
    monkeypatch.setattr(pipeline, "sector_aggregates", lambda *_args: empty)
    monkeypatch.setattr(pipeline, "industry_trends", lambda *_args: empty)
    monkeypatch.setattr(pipeline, "build_path_library", build_library)
    monkeypatch.setattr(pipeline, "score_universe", lambda *_args: empty)

    result = pipeline.run_nightly(
        pl.DataFrame(), members, d=d,
        portfolio=Portfolio(equity=500_000.0, cash=500_000.0, watermark=500_000.0),
        tradeable_index="NIFTY200",
    )

    assert result.regime is Regime.TREND_UP
    assert set(result.breadth["universe"].to_list()) == {"NIFTY200", "NIFTY500"}
    nifty200 = result.breadth.filter(pl.col("universe") == "NIFTY200").row(0, named=True)
    nifty500 = result.breadth.filter(pl.col("universe") == "NIFTY500").row(0, named=True)
    assert nifty200["pct_above_sma50"] == 0.0
    assert nifty500["pct_above_sma50"] == 0.6
    assert path_symbols[-1] == {"A", "B"}

    default_result = pipeline.run_nightly(
        pl.DataFrame(), members, d=d,
        portfolio=Portfolio(equity=500_000.0, cash=500_000.0, watermark=500_000.0),
    )

    assert default_result.n_universe == 5
    assert default_result.breadth["universe"].to_list() == ["NIFTY500"]
    assert path_symbols[-1] == set(symbols)