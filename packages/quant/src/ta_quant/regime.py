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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import polars as pl
from ta_core.enums import Regime

__all__ = [
    "ATR_WINDOW",
    "BREADTH_DOWN",
    "BREADTH_DOWN_SIDEWAYS",
    "CANDIDATES",
    "EntryBlock",
    "INDEX_BEAR",
    "INDEX_BEAR_SIDEWAYS",
    "LOOSEN_AFTER",
    "MacroRegime",
    "NATR_HIGH",
    "REGIME_INDEX",
    "SMA_FAST",
    "SMA_SLOW",
    "VOL_BULL_MAX",
    "VOL_HIGH",
    "VOL_WINDOW",
    "apply_hysteresis",
    "classify",
    "classify_macro_regime_panel",
    "compute_hurst_rs",
    "regime_on",
    "regime_series",
]

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


class MacroRegime(StrEnum):
    """4-state causal macro market regime for ensemble meta-voting and adaptive trails."""

    LOW_VOL_TREND = "LOW_VOL_TREND"
    HIGH_VOL_TREND = "HIGH_VOL_TREND"
    SIDEWAYS_CHOP = "SIDEWAYS_CHOP"
    BEAR_HIGH_VOL_SHOCK = "BEAR_HIGH_VOL_SHOCK"


def compute_hurst_rs(log_returns: Sequence[float]) -> float:
    """Rescaled-range (R/S) Hurst exponent clamped to `[0.05, 0.95]` (`0.5` if `< 60` bars)."""
    n = len(log_returns)
    if n < 60:
        return 0.5
    mean_all = sum(log_returns) / n
    var_all = sum((x - mean_all) ** 2 for x in log_returns) / n
    if var_all < 1e-24:
        return 0.5

    log_x: list[float] = []
    log_y: list[float] = []
    for k in (10, 16, 20, 25, 50):
        n_chunks = n // k
        if n_chunks < 1:
            continue
        rs_vals: list[float] = []
        for idx in range(n_chunks):
            chunk = log_returns[idx * k : (idx + 1) * k]
            mc = sum(chunk) / k
            var_c = sum((v - mc) ** 2 for v in chunk) / max(1, k - 1)
            sd = math.sqrt(var_c) if var_c > 0.0 else 0.0
            if sd < 1e-12:
                continue
            running = 0.0
            c_min = 0.0
            c_max = 0.0
            for v in chunk:
                running += v - mc
                if running < c_min:
                    c_min = running
                if running > c_max:
                    c_max = running
            rng = c_max - c_min
            if rng > 0.0:
                rs_vals.append(rng / sd)
        if rs_vals:
            log_x.append(math.log(k))
            log_y.append(math.log(sum(rs_vals) / len(rs_vals)))

    if len(log_x) < 2:
        return 0.5
    mx = sum(log_x) / len(log_x)
    my = sum(log_y) / len(log_y)
    den = sum((x - mx) ** 2 for x in log_x)
    if den <= 1e-12:
        return 0.5
    slope = sum((x - mx) * (y - my) for x, y in zip(log_x, log_y, strict=True)) / den
    return float(max(0.05, min(0.95, slope)))


def classify_macro_regime_panel(
    index_bars: pl.DataFrame,
    vix_bars: pl.DataFrame | None = None,
    *,
    breadth_by_date: Mapping[date, float] | None = None,
    index_name: str = REGIME_INDEX,
) -> pl.DataFrame:
    """Classify every session strictly causally into the 4-state `MacroRegime`."""
    schema = {
        "d": pl.Date,
        "close": pl.Float64,
        "sma50": pl.Float64,
        "sma200": pl.Float64,
        "adx14": pl.Float64,
        "hurst100": pl.Float64,
        "vix": pl.Float64,
        "vix_pct252": pl.Float64,
        "breadth_above_sma50": pl.Float64,
        "rv_20_over_60": pl.Float64,
        "macro_regime": pl.Utf8,
    }
    df = (
        index_bars.filter(pl.col("index_name") == index_name)
        if "index_name" in index_bars.columns
        else index_bars
    )
    if df.is_empty():
        return pl.DataFrame(schema=schema)

    df = df.unique(subset=["d"], keep="last").sort("d")
    vix_map: dict[date, float] = {}
    if vix_bars is not None and not vix_bars.is_empty():
        vdf = vix_bars.filter(pl.col("index_name") == "India VIX") if "index_name" in vix_bars.columns else vix_bars
        for r in vdf.select(["d", "close"]).iter_rows(named=True):
            if r["close"] is not None:
                vix_map[r["d"]] = float(r["close"])

    dates = df["d"].to_list()
    cl = [float(x) for x in df["close"].to_list()]
    hi = [float(h) if h is not None else c for h, c in zip(df["high"].to_list(), cl, strict=True)] if "high" in df.columns else list(cl)
    lo = [float(l) if l is not None else c for l, c in zip(df["low"].to_list(), cl, strict=True)] if "low" in df.columns else list(cl)
    n = len(dates)

    tr = [0.0] * n
    p_dm = [0.0] * n
    m_dm = [0.0] * n
    log_r = [0.0] * n
    for i in range(1, n):
        tr[i] = max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
        up, dn = hi[i] - hi[i - 1], lo[i - 1] - lo[i]
        p_dm[i] = up if (up > dn and up > 0.0) else 0.0
        m_dm[i] = dn if (dn > up and dn > 0.0) else 0.0
        if cl[i - 1] > 0.0 and cl[i] > 0.0:
            log_r[i] = math.log(cl[i] / cl[i - 1])

    adx14: list[float] = [20.0] * n
    if n > 28:
        aw = sum(tr[1:15]) / 14.0
        pw = sum(p_dm[1:15]) / 14.0
        mw = sum(m_dm[1:15]) / 14.0
        dxs: list[float] = []
        adx_w = 20.0
        for i in range(14, n):
            if i > 14:
                aw = (aw * 13.0 + tr[i]) / 14.0
                pw = (pw * 13.0 + p_dm[i]) / 14.0
                mw = (mw * 13.0 + m_dm[i]) / 14.0
            pdi = 100.0 * pw / aw if aw > 1e-12 else 0.0
            mdi = 100.0 * mw / aw if aw > 1e-12 else 0.0
            dx = 100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 1e-12 else 0.0
            dxs.append(dx)
            if len(dxs) == 14:
                adx_w = sum(dxs) / 14.0
                adx14[i] = adx_w
            elif len(dxs) > 14:
                adx_w = (adx_w * 13.0 + dx) / 14.0
                adx14[i] = adx_w

    vix_vals = [vix_map.get(d, 15.0) for d in dates]
    rows: list[dict[str, object]] = []
    for i, d in enumerate(dates):
        s50 = sum(cl[i - 49 : i + 1]) / 50.0 if i >= 49 else None
        s200 = sum(cl[i - 199 : i + 1]) / 200.0 if i >= 199 else None
        h100 = compute_hurst_rs(log_r[i - 99 : i + 1]) if i >= 100 else 0.5
        vw = vix_vals[max(0, i - 251) : i + 1]
        if len(vw) > 10:
            n_lt = sum(1 for x in vw if x < vix_vals[i])
            n_eq = sum(1 for x in vw if x == vix_vals[i])
            vp = 100.0 * (n_lt + 0.5 * n_eq) / len(vw)
        else:
            vp = 50.0
        b50 = float(breadth_by_date.get(d, 0.50)) if breadth_by_date is not None else 0.50

        rv_ratio = 1.0
        if i >= 60:
            w20 = log_r[i - 19 : i + 1]
            w60 = log_r[i - 59 : i + 1]
            m20 = sum(w20) / 20.0
            m60 = sum(w60) / 60.0
            s20 = math.sqrt(sum((x - m20) ** 2 for x in w20) / 19.0)
            s60 = math.sqrt(sum((x - m60) ** 2 for x in w60) / 59.0)
            if s60 > 1e-12:
                rv_ratio = s20 / s60

        c = cl[i]
        s50_eff = s50 if s50 is not None else c
        s200_eff = s200 if s200 is not None else c
        adx = adx14[i]

        if (c < s200_eff and (vp >= 70.0 or b50 < 0.38 or rv_ratio > 1.15)) or (vp >= 88.0 and c < s50_eff):
            reg = MacroRegime.BEAR_HIGH_VOL_SHOCK
        elif c > s50_eff > s200_eff and vp < 65.0 and (adx >= 20.0 or h100 >= 0.54):
            reg = MacroRegime.LOW_VOL_TREND
        elif c > s200_eff and adx >= 22.0 and vp >= 65.0:
            reg = MacroRegime.HIGH_VOL_TREND
        else:
            reg = MacroRegime.SIDEWAYS_CHOP

        rows.append({
            "d": d,
            "close": c,
            "sma50": s50,
            "sma200": s200,
            "adx14": round(adx, 4),
            "hurst100": round(h100, 4),
            "vix": round(vix_vals[i], 4),
            "vix_pct252": round(vp, 4),
            "breadth_above_sma50": round(b50, 4),
            "rv_20_over_60": round(rv_ratio, 4),
            "macro_regime": reg.value,
        })

    return pl.DataFrame(rows, schema=schema)
