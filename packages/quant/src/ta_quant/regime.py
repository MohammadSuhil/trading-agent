"""Index-based market regime: deterministic rules on the benchmark index (ADR-0004).

**Status (ADR-0006, 2026-11-12): gates NEW entries in the live arm.** Caps still
follow the breadth regime (`pipeline.infer_regime`); the live arm's EntryBlock
(`nightly.LIVE_ENTRY_BLOCK = INDEX_BEAR_SIDEWAYS`) vetoes new buys while this
index regime is bear or sideways. ADR-0004 had rejected a bear-only block; on
the corrected engine and with 5 start dates per period it beat the control.
The frozen control never takes a block.

The rules, on the Nifty 500 index (the universe we trade, so its regime is
the one our candidates live in), evaluated at each session's close and using
only bars up to that close:

====================  ==========================================  =============
state                 rule (first match wins)                     maps to
====================  ==========================================  =============
BEAR                  close < SMA200  and  SMA50 < SMA200          TREND_DOWN
HIGH_VOLATILITY       vol20 > 25%  or  NATR14 > 2.5%               HIGH_VOL
BULL                  close > SMA200, SMA50 > SMA200, vol20 <= 20%  TREND_UP
SIDEWAYS              everything else                              CHOP
====================  ==========================================  =============

`vol20` is the annualised stdev of 20 daily log returns; NATR14 is the
14-session mean true range over the close. BEAR is checked first on purpose:
a falling market that is also volatile is a falling market.

**Hysteresis (RISK-26).** Tightening takes effect on the first session; loosening
only after the less severe state has held for 3 sessions. Without it a market
straddling its 200-DMA flips the book between "blocked" and "buy" daily.

Thresholds come from the spec as written, NOT from fitting this data -- see the
ADR for the out-of-sample check that was run before adopting them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import polars as pl
from ta_core.enums import Regime

#: The index whose regime gates entries. Chosen a priori: it IS our universe.
REGIME_INDEX = "Nifty 500"

SMA_FAST, SMA_SLOW = 50, 200
VOL_WINDOW, ATR_WINDOW = 20, 14
VOL_BULL_MAX, VOL_HIGH, NATR_HIGH = 0.20, 0.25, 0.025
LOOSEN_AFTER = 3

#: Severity for hysteresis: higher is more restrictive.
_SEVERITY = {Regime.TREND_UP: 0, Regime.CHOP: 1, Regime.HIGH_VOL: 2,
             Regime.TREND_DOWN: 3, Regime.UNKNOWN: 3}


def classify(close: float, sma50: float | None, sma200: float | None,
             vol20: float | None, natr14: float | None) -> tuple[Regime, str]:
    """One session's raw state, before hysteresis.

    >>> classify(90, 95, 100, 0.15, 0.01)[0].value
    'TREND_DOWN'
    >>> classify(110, 105, 100, 0.30, 0.01)[0].value
    'HIGH_VOL'
    >>> classify(110, 105, 100, 0.15, 0.01)[0].value
    'TREND_UP'
    >>> classify(110, 105, 100, 0.22, 0.01)[0].value   # bull trend, vol 20-25%
    'CHOP'
    >>> classify(110, None, None, None, None)[0].value  # warm-up
    'UNKNOWN'
    """
    if sma50 is None or sma200 is None or vol20 is None or natr14 is None:
        return Regime.UNKNOWN, "index history shorter than the 200-session SMA"
    if close < sma200 and sma50 < sma200:
        return Regime.TREND_DOWN, f"index below 200-DMA and 50-DMA under 200-DMA (vol {vol20:.0%})"
    if vol20 > VOL_HIGH or natr14 > NATR_HIGH:
        return Regime.HIGH_VOL, f"index vol {vol20:.0%} / NATR {natr14:.2%} above {VOL_HIGH:.0%} / {NATR_HIGH:.1%}"
    if close > sma200 and sma50 > sma200 and vol20 <= VOL_BULL_MAX:
        return Regime.TREND_UP, f"index above 200-DMA, 50 over 200, vol {vol20:.0%}"
    return Regime.CHOP, f"index neither trending cleanly nor breaking down (vol {vol20:.0%})"


def apply_hysteresis(raw: list[Regime], *, loosen_after: int = LOOSEN_AFTER) -> list[Regime]:
    """RISK-26: tighten on the first session; loosen only after `loosen_after`.

    Leaving warm-up (UNKNOWN) takes the first measured state at once -- that is
    measurement arriving, not risk being loosened.

    >>> B, C, U = Regime.TREND_DOWN, Regime.CHOP, Regime.TREND_UP
    >>> [r.value for r in apply_hysteresis([B, C, C, C, U])]
    ['TREND_DOWN', 'TREND_DOWN', 'TREND_DOWN', 'CHOP', 'CHOP']
    >>> [r.value for r in apply_hysteresis([U, B, U])]          # tighten instantly
    ['TREND_UP', 'TREND_DOWN', 'TREND_DOWN']
    >>> [r.value for r in apply_hysteresis([B, C, U, C, C, C])]  # a broken run restarts the count
    ['TREND_DOWN', 'TREND_DOWN', 'TREND_DOWN', 'TREND_DOWN', 'TREND_DOWN', 'CHOP']
    """
    out: list[Regime] = []
    held, pending, n = Regime.UNKNOWN, None, 0
    for r in raw:
        if held is Regime.UNKNOWN or _SEVERITY[r] >= _SEVERITY[held]:
            held, pending, n = r, None, 0
        else:
            n = n + 1 if r is pending else 1
            pending = r
            if n >= loosen_after:
                held, pending, n = r, None, 0
        out.append(held)
    return out


def regime_series(index_bars: pl.DataFrame, *, index_name: str = REGIME_INDEX) -> pl.DataFrame:
    """Per-session regime for one index, strictly trailing, with hysteresis.

    `index_bars` needs `d, index_name, high, low, close`. Returns
    `d, regime, raw_regime, note, sma50, sma200, vol20, natr14` sorted by d.
    Row t uses bars <= t only, so `regime_series(bars)` filtered to d equals
    `regime_series(bars up to d)` at d -- the property the live/backtest
    equality depends on (tested).
    """
    df = (
        index_bars.filter(pl.col("index_name") == index_name)
        .unique(subset=["d"], keep="last").sort("d")
        if "index_name" in index_bars.columns else pl.DataFrame()
    )
    if df.is_empty():
        return pl.DataFrame(schema={"d": pl.Date, "regime": pl.Utf8, "raw_regime": pl.Utf8,
                                    "note": pl.Utf8, "sma50": pl.Float64, "sma200": pl.Float64,
                                    "vol20": pl.Float64, "natr14": pl.Float64})
    prev = pl.col("close").shift(1)
    tr = pl.max_horizontal(
        pl.col("high").fill_null(pl.col("close")) - pl.col("low").fill_null(pl.col("close")),
        (pl.col("high").fill_null(pl.col("close")) - prev).abs(),
        (pl.col("low").fill_null(pl.col("close")) - prev).abs(),
    )
    df = df.with_columns(
        pl.col("close").rolling_mean(SMA_FAST).alias("sma50"),
        pl.col("close").rolling_mean(SMA_SLOW).alias("sma200"),
        ((pl.col("close") / prev).log().rolling_std(VOL_WINDOW) * math.sqrt(252)).alias("vol20"),
        (tr.rolling_mean(ATR_WINDOW) / pl.col("close")).alias("natr14"),
    )
    classified = [classify(*r) for r in df.select("close", "sma50", "sma200", "vol20", "natr14").iter_rows()]
    raws = [c[0] for c in classified]
    held = apply_hysteresis(raws)
    notes = [
        note if h is r else f"{note}; holding {h.value} until {r.value} lasts {LOOSEN_AFTER} sessions"
        for (r, note), h in zip(classified, held, strict=True)
    ]
    regimes = [h.value for h in held]
    raws = [r.value for r in raws]
    return df.select("d", "sma50", "sma200", "vol20", "natr14").with_columns(
        pl.Series("regime", regimes), pl.Series("raw_regime", raws), pl.Series("note", notes),
    ).select("d", "regime", "raw_regime", "note", "sma50", "sma200", "vol20", "natr14")


def regime_on(series: pl.DataFrame, d: date) -> tuple[Regime, str]:
    """The regime in force at the close of `d` (latest row at or before d)."""
    row = series.filter(pl.col("d") <= d).tail(1)
    if row.is_empty():
        return Regime.UNKNOWN, "no index history at or before this session"
    return Regime(row["regime"][0]), f"{REGIME_INDEX}: {row['note'][0]}"


@dataclass(frozen=True)
class EntryBlock:
    """One rule: no NEW long while `source`'s regime is in `regimes`.

    It changes exactly one thing. Position/heat caps always come from the
    strategy's own breadth regime (`pipeline.infer_regime`); an EntryBlock only
    adds a veto on new entries. (The first REG-01 implementation also swapped
    the caps to the index regime, so a test of it changed two things at once.)
    Exits are never affected. An index regime still in warm-up (UNKNOWN) does
    not block: an unmeasured market is not evidence of a bear one.

    >>> b = EntryBlock("BREADTH", frozenset({Regime.TREND_DOWN, Regime.CHOP}), "test")
    >>> b.blocks(Regime.CHOP, None), b.blocks(Regime.TREND_UP, Regime.TREND_DOWN)
    (True, False)
    >>> EntryBlock("INDEX", frozenset({Regime.TREND_DOWN}), "t").blocks(Regime.TREND_UP, Regime.UNKNOWN)
    False
    """

    source: str                    # "BREADTH" | "INDEX"
    regimes: frozenset[Regime]
    label: str

    def blocks(self, breadth: Regime, index: Regime | None) -> bool:
        r = breadth if self.source == "BREADTH" else index
        return r is not None and r is not Regime.UNKNOWN and r in self.regimes

    def describe(self) -> str:
        names = "/".join(sorted(x.value for x in self.regimes))
        return f"{self.label}: no new buys while the {self.source.lower()} regime is {names}"


#: The pre-registered candidates (2026-11-12). V0 is "no EntryBlock".
INDEX_BEAR = EntryBlock("INDEX", frozenset({Regime.TREND_DOWN}), "V1 index bear")
INDEX_BEAR_SIDEWAYS = EntryBlock("INDEX", frozenset({Regime.TREND_DOWN, Regime.CHOP}), "V2 index bear+sideways")
BREADTH_DOWN = EntryBlock("BREADTH", frozenset({Regime.TREND_DOWN}), "V3 breadth down")
BREADTH_DOWN_SIDEWAYS = EntryBlock("BREADTH", frozenset({Regime.TREND_DOWN, Regime.CHOP}), "V4 breadth down+sideways")
CANDIDATES = (INDEX_BEAR, INDEX_BEAR_SIDEWAYS, BREADTH_DOWN, BREADTH_DOWN_SIDEWAYS)
