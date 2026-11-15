"""Phase 2 & 3 Sandbox: Institutional Alpha Variants (N=5), Regime Meta-Voter & 4-Tier Risk Engine.

Strictly isolated inside `/sim_sandbox/` (edits ZERO production files).
All features, regressions (63d OLS beta, market+industry demeaned residuals, 60d AR(1) OU
parameters, Parkinson compression, PIT Dirichlet BarrierModel, and 60d pairwise correlation)
are strictly point-in-time (`<= decision_date`), and all orders fill at `d+1` via
`ta_ledger.broker.paper.PaperBroker` (`I-14` pessimistic DOWN resolution + full NSE costs).

Global Logged Search Variants (`N = 5`, ZERO primitive indicator crossovers):
  1. `V1_Baseline_Sortino`         - Existing production v2 monthly Sortino momentum baseline
  2. `V2_Residual_Alpha_HAC`       - Blitz-Huij-Martens factor-neutral residual alpha + Newey-West HAC
  3. `V3_OU_Residual_Reversion`    - Avellaneda-Lee Ornstein-Uhlenbeck half-life residual reversion
  4. `V4_VolCompression_Barrier`   - Parkinson vol compression + absorption + PIT BarrierModel Dirichlet
  5. `V5_Defensive_CrisisShield`   - Bear-shock V-reversal capitulation & ultra-low-beta quality shield
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import numpy as np
import polars as pl
from ta_core.enums import Arm, Horizon, Sleeve
from ta_ledger.broker.base import BarData, Order
from ta_ledger.broker.paper import PaperBroker
from ta_ledger.risk import DEFAULT
from ta_quant.barriers import BarrierModel, build_path_library, known_paths
from ta_quant.challenger import _build_v2_panel, monthly_rebalance_dates, score_universe_v2
from ta_quant.pipeline import build_panel
from ta_quant.weights import load_challenger_weights

VARIANTS_LOGGED: tuple[str, ...] = (
    "V1_Baseline_Sortino",
    "V2_Residual_Alpha_HAC",
    "V3_OU_Residual_Reversion",
    "V4_VolCompression_Barrier",
    "V5_Defensive_CrisisShield",
)


# ---------------------------------------------------------------------------
# 1. Macro Regime Classifier (re-using Phase 1 audited PIT macro series)
# ---------------------------------------------------------------------------
def _compute_hurst_rs(log_rets: np.ndarray) -> float:
    n = len(log_rets)
    if n < 60 or np.std(log_rets) < 1e-12:
        return 0.5
    log_x: list[float] = []
    log_y: list[float] = []
    for k in (10, 16, 20, 25, 50):
        n_chunks = n // k
        if n_chunks < 1:
            continue
        rs_vals: list[float] = []
        for i in range(n_chunks):
            chunk = log_rets[i * k : (i + 1) * k]
            s = float(np.std(chunk, ddof=1))
            if s < 1e-12:
                continue
            cum = np.cumsum(chunk - np.mean(chunk))
            r = float(np.max(cum) - np.min(cum))
            if r > 0:
                rs_vals.append(r / s)
        if rs_vals:
            log_x.append(math.log(k))
            log_y.append(math.log(float(np.mean(rs_vals))))
    if len(log_x) < 2:
        return 0.5
    slope = float(np.polyfit(np.array(log_x), np.array(log_y), 1)[0])
    return float(max(0.05, min(0.95, slope)))


def compute_macro_regime_map(
    idx_df: pl.DataFrame,
    vix_df: pl.DataFrame,
    panel_eq: pl.DataFrame,
) -> dict[date, dict[str, Any]]:
    """Build strictly trailing macro regime & breadth series per NSE session."""
    df = idx_df.sort("d").unique(subset=["d"], keep="last").sort("d")
    vix_map = {r["d"]: float(r["close"]) for r in vix_df.select(["d", "close"]).iter_rows(named=True)}

    breadth_df = (
        panel_eq.filter(pl.col("sma50").is_not_null() & pl.col("ret_21d").is_not_null())
        .group_by("d")
        .agg(
            (pl.col("close") > pl.col("sma50")).mean().alias("breadth_50"),
            pl.col("ret_21d").std().alias("cs_disp_21d"),
        )
    )
    breadth_map = {
        r["d"]: (float(r["breadth_50"] or 0.5), float(r["cs_disp_21d"] or 0.1))
        for r in breadth_df.iter_rows(named=True)
    }

    hi = df["high"].to_numpy().astype(np.float64)
    lo = df["low"].to_numpy().astype(np.float64)
    cl = df["close"].to_numpy().astype(np.float64)
    dates = df["d"].to_list()
    n = len(cl)

    vix_arr = np.array([vix_map.get(d, 15.0) for d in dates], dtype=np.float64)
    tr = np.zeros(n)
    p_dm = np.zeros(n)
    m_dm = np.zeros(n)
    log_r = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
        up, dn = hi[i] - hi[i - 1], lo[i - 1] - lo[i]
        p_dm[i] = up if (up > dn and up > 0) else 0.0
        m_dm[i] = dn if (dn > up and dn > 0) else 0.0
        if cl[i - 1] > 0 and cl[i] > 0:
            log_r[i] = math.log(cl[i] / cl[i - 1])

    atr14 = np.full(n, np.nan)
    adx14 = np.full(n, np.nan)
    if n > 28:
        aw, pw, mw = float(np.mean(tr[1:15])), float(np.mean(p_dm[1:15])), float(np.mean(m_dm[1:15]))
        dxs: list[float] = []
        adx_w = 20.0
        for i in range(14, n):
            if i > 14:
                aw = (aw * 13.0 + tr[i]) / 14.0
                pw = (pw * 13.0 + p_dm[i]) / 14.0
                mw = (mw * 13.0 + m_dm[i]) / 14.0
            atr14[i] = aw
            pdi = 100.0 * pw / aw if aw > 1e-12 else 0.0
            mdi = 100.0 * mw / aw if aw > 1e-12 else 0.0
            dx = 100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 1e-12 else 0.0
            dxs.append(dx)
            if len(dxs) == 14:
                adx_w = float(np.mean(dxs))
                adx14[i] = adx_w
            elif len(dxs) > 14:
                adx_w = (adx_w * 13.0 + dx) / 14.0
                adx14[i] = adx_w

    atr_pct = np.where(cl > 0, (atr14 / cl) * 100.0, np.nan)
    atr_vol_pct = np.full(n, 50.0)
    vix_pct = np.full(n, 50.0)
    hurst100 = np.full(n, 0.5)
    sma50 = np.full(n, np.nan)
    sma200 = np.full(n, np.nan)
    ret_20d = np.zeros(n)
    sma50_slope_20d = np.zeros(n)
    breadth_hist = np.full(n, 0.5)

    out: dict[date, dict[str, Any]] = {}
    for i in range(n):
        if i >= 49:
            sma50[i] = float(np.mean(cl[i - 49 : i + 1]))
        if i >= 199:
            sma200[i] = float(np.mean(cl[i - 199 : i + 1]))
        if i >= 20 and cl[i - 20] > 0:
            ret_20d[i] = cl[i] / cl[i - 20] - 1.0
        if i >= 69 and sma50[i - 20] > 0 and not np.isnan(sma50[i]):
            sma50_slope_20d[i] = sma50[i] / sma50[i - 20] - 1.0
        if i >= 100:
            hurst100[i] = _compute_hurst_rs(log_r[i - 99 : i + 1])
        if not np.isnan(atr_pct[i]):
            w = atr_pct[max(14, i - 251) : i + 1]
            w = w[~np.isnan(w)]
            if len(w) > 10:
                atr_vol_pct[i] = 100.0 * float(np.sum(w <= atr_pct[i])) / len(w)
        vw = vix_arr[max(0, i - 251) : i + 1]
        if len(vw) > 10:
            vix_pct[i] = 100.0 * float(np.sum(vw <= vix_arr[i])) / len(vw)

        b50, disp = breadth_map.get(dates[i], (0.5, 0.1))
        breadth_hist[i] = b50
        b50_5d_min = float(np.min(breadth_hist[max(0, i - 5) : i + 1]))

        adx = float(adx14[i]) if not np.isnan(adx14[i]) else 20.0
        h = float(hurst100[i])
        vp = float(max(atr_vol_pct[i], vix_pct[i]))
        c = float(cl[i])
        s50 = float(sma50[i]) if not np.isnan(sma50[i]) else c
        s200 = float(sma200[i]) if not np.isnan(sma200[i]) else c
        r20 = float(ret_20d[i])
        sl50 = float(sma50_slope_20d[i])
        neg_mom = (r20 < 0.0) or (c < s50) or (sl50 < 0.0)

        if (vp >= 85.0 and neg_mom) or (c < s200 and vp >= 70.0 and r20 < -0.02) or (vix_arr[i] >= 22.0 and c < s50):
            reg = "BEAR_HIGH_VOL_SHOCK"
        elif adx >= 25.0 and h > 0.55 and vp < 65.0:
            reg = "LOW_VOL_TREND"
        elif adx >= 25.0 and h > 0.55 and vp >= 65.0:
            reg = "HIGH_VOL_TREND"
        elif adx < 20.0 or (0.45 <= h <= 0.55):
            reg = "SIDEWAYS_CHOP"
        else:
            if c > s50 > s200 and h > 0.55 and sl50 > 0:
                reg = "LOW_VOL_TREND" if vp < 65.0 else "HIGH_VOL_TREND"
            else:
                reg = "SIDEWAYS_CHOP"

        idx_sma20 = float(np.mean(cl[max(0, i - 19) : i + 1])) if cl[i] > 0 else c
        idx_ret5 = (cl[i] / cl[i - 5] - 1.0) if (i >= 5 and cl[i - 5] > 0) else 0.0
        vix_chg5 = (vix_arr[i] / vix_arr[i - 5] - 1.0) if (i >= 5 and vix_arr[i - 5] > 0) else 0.0
        b_turn = b50 - b50_5d_min
        macro_recovery = (c > idx_sma20) and (idx_ret5 > 0.012) and (b_turn >= 0.025) and (vix_chg5 < -0.03)

        out[dates[i]] = {
            "macro_regime": reg,
            "vix": float(vix_arr[i]),
            "vix_pct": float(vix_pct[i]),
            "atr_vol_pct": float(atr_vol_pct[i]),
            "adx14": adx,
            "hurst100": h,
            "breadth_50": b50,
            "breadth_turn": b_turn,
            "idx_ret_20d": r20,
            "idx_ret_5d": idx_ret5,
            "idx_above_sma20": c > idx_sma20,
            "vix_chg_5d": vix_chg5,
            "macro_recovery": macro_recovery,
            "idx_log_ret": float(log_r[i]),
        }
    return out


# ---------------------------------------------------------------------------
# 2. Vectorized Point-In-Time Research DataStore (0 Lookahead)
# ---------------------------------------------------------------------------
@dataclass
class ResearchDataStore:
    """Point-in-time indexed numpy/dict store for fast multi-variant & Walk-Forward simulation."""

    dates: list[date]
    date_to_idx: dict[date, int]
    symbols: list[str]
    sym_to_idx: dict[str, int]
    industries: list[str]
    bars_by_date: dict[date, dict[str, BarData]]
    macro_map: dict[date, dict[str, Any]]
    v1_scores_by_date: dict[date, list[tuple[str, str, float, float, float, float]]]  # sym, ind, score, close, atr14, adtv
    v1_top15_by_date: dict[date, set[str]]
    monthly_rebal_set: set[date]

    # Dense (T, S) PIT matrices
    open_m: np.ndarray
    high_m: np.ndarray
    low_m: np.ndarray
    close_m: np.ndarray
    atr14_m: np.ndarray
    natr14_m: np.ndarray
    adtv20_m: np.ndarray
    trades_m: np.ndarray
    listed_m: np.ndarray
    ret1d_m: np.ndarray
    ret63d_m: np.ndarray
    ret126d_m: np.ndarray
    mom_12_1_m: np.ndarray
    dist_52w_high_m: np.ndarray
    close_in_range_m: np.ndarray
    overnight_spread_21_m: np.ndarray
    vol_ratio_5_21_m: np.ndarray
    parkinson_ratio_m: np.ndarray
    up_dn_vol_20_m: np.ndarray
    turnover_ratio_5_20_m: np.ndarray
    amihud_21_m: np.ndarray
    sma50_above_m: np.ndarray
    sma200_above_m: np.ndarray
    t_drift_20_m: np.ndarray
    t_drift_63_m: np.ndarray
    t_drift_252_m: np.ndarray
    er_63_m: np.ndarray

    # Blitz-Huij-Martens Residual Alpha + Avellaneda-Lee OU Parameters (strictly PIT <= t)
    beta63_m: np.ndarray
    res_ret_m: np.ndarray      # e_{t, s} market-and-industry demeaned residual daily return
    res_mom_63_m: np.ndarray   # 63d residual Sortino ratio
    res_mom_20_m: np.ndarray   # 20d residual Sortino ratio
    ou_s_m: np.ndarray         # OU equilibrium z-score s_{t, s}
    ou_tau_m: np.ndarray       # OU half-life in sessions
    ou_b_m: np.ndarray         # AR(1) contraction slope

    # Strictly PIT BarrierModel posteriors keyed by date -> {vol_tercile: (p_up, p_dn, expected_r, valid)}
    tercile_barriers_by_date: dict[date, dict[int, tuple[float, float, float, bool]]]
    # Strictly PIT stock-level trailing-252d Dirichlet shrunk UP rate & tercile cutoffs per date
    sym_barrier_edge_m: np.ndarray   # shape (T, S): shrunk (p_up - p_dn)
    tercile_cuts_by_date: dict[date, tuple[float, float]]
    # Precomputed unfiltered score_universe_v2 DataFrames on monthly rebalance dates
    scored_by_rebal: dict[date, pl.DataFrame] = field(default_factory=dict)


def build_research_store(
    bars_eq: pl.DataFrame,
    members: pl.DataFrame,
    idx_df: pl.DataFrame,
    vix_df: pl.DataFrame,
    active_sessions: list[date],
) -> ResearchDataStore:
    """Precompute strictly trailing PIT matrices & BarrierModel snapshots in one vectorized pass."""
    mem_syms = set(members["symbol"].to_list())
    bars_liq_syms = (
        bars_eq.group_by("symbol")
        .agg(pl.col("turnover").max().alias("_max_t"))
        .filter((pl.col("_max_t") >= 3e7) | pl.col("symbol").is_in(sorted(mem_syms)))["symbol"]
        .to_list()
    )
    bars_sub = bars_eq.filter(pl.col("symbol").is_in(bars_liq_syms)).sort(["symbol", "d"])
    s_col = pl.col("symbol")
    c_col = pl.col("close")
    panel = (
        build_panel(bars_sub, members)
        .sort(["symbol", "d"])
        .with_columns(
            pl.int_range(pl.len()).over(s_col).alias("bars_seen"),
            (
                ((c_col / c_col.shift(1).over(s_col)).log().clip(upper_bound=0.0) ** 2)
                .rolling_mean(252)
                .over(s_col)
                .sqrt()
                * (252.0 ** 0.5)
            ).alias("dvol_252"),
            (c_col / c_col.rolling_max(252).over(s_col) - 1.0).alias("dist_52w_high"),
        )
    )
    macro_map = compute_macro_regime_map(idx_df, vix_df, panel)

    # Universe: symbols with real NSE industry mapping (matches score_universe_v2 Nifty500+ universe)
    panel_ind = panel.filter(pl.col("industry").is_not_null() & (pl.col("industry") != "Unclassified"))
    dates = sorted(bars_eq["d"].unique().to_list())
    date_to_idx = {d: i for i, d in enumerate(dates)}

    # Keep symbols that ever pass minimum institutional eligibility so dense matrix stays lean
    elig_syms = (
        panel_ind.group_by("symbol")
        .agg(
            pl.col("adtv_20").max().alias("max_adtv"),
            pl.col("industry").drop_nulls().last().alias("industry"),
        )
        .filter(pl.col("max_adtv") >= 3e7)
        .sort("symbol")
    )
    symbols = elig_syms["symbol"].to_list()
    industries = elig_syms["industry"].to_list()
    sym_to_idx = {s: i for i, s in enumerate(symbols)}

    T, S = len(dates), len(symbols)
    def _empty(fill: float = np.nan) -> np.ndarray:
        return np.full((T, S), fill, dtype=np.float64)

    open_m = _empty()
    high_m = _empty()
    low_m = _empty()
    close_m = _empty()
    atr14_m = _empty()
    natr14_m = _empty()
    adtv20_m = _empty(0.0)
    trades_m = _empty(0.0)
    listed_m = _empty(0.0)
    ret1d_m = _empty(0.0)
    ret63d_m = _empty()
    ret126d_m = _empty()
    mom_12_1_m = _empty(0.0)
    dist_52w_high_m = _empty(0.0)
    close_in_range_m = _empty()
    overnight_spread_21_m = _empty()
    vol_ratio_5_21_m = _empty()
    parkinson_ratio_m = _empty()
    up_dn_vol_20_m = _empty()
    turnover_ratio_5_20_m = _empty()
    amihud_21_m = _empty()
    sma50_above_m = _empty(0.0)
    sma200_above_m = _empty(0.0)
    t_drift_20_m = _empty(0.0)
    t_drift_63_m = _empty(0.0)
    t_drift_252_m = _empty(0.0)
    er_63_m = _empty(0.0)

    bars_by_date: dict[date, dict[str, BarData]] = {}
    for r in bars_eq.select(["symbol", "d", "open", "high", "low", "close"]).iter_rows(named=True):
        rd = r["d"]
        bars_by_date.setdefault(rd, {})[str(r["symbol"])] = BarData(
            symbol=str(r["symbol"]),
            d=rd,
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
        )

    panel_sub = panel_ind.filter(pl.col("symbol").is_in(symbols)).select([
        "symbol", "d", "open", "high", "low", "close", "atr14", "adtv_20", "n_trades",
        "bars_seen", "ret_1d", "ret_63d", "ret_126d", "mom_12_1", "dist_52w_high", "close_in_range",
        "overnight_ret_21d", "intraday_ret_21d", "vol_ratio_5_21", "parkinson_vol_21d",
        "realised_vol_63d", "up_down_vol_ratio_20", "turnover_ratio_5_20", "amihud_21d",
        "sma50", "sma200", "t_drift_20", "t_drift_63", "t_drift_252", "er_63",
    ])
    for r in panel_sub.iter_rows(named=True):
        ti = date_to_idx.get(r["d"])
        si = sym_to_idx.get(r["symbol"])
        if ti is None or si is None:
            continue
        cl = float(r["close"])
        open_m[ti, si] = float(r["open"])
        high_m[ti, si] = float(r["high"])
        low_m[ti, si] = float(r["low"])
        close_m[ti, si] = cl
        atr = r["atr14"]
        if atr is not None and cl > 0:
            atr14_m[ti, si] = float(atr)
            natr14_m[ti, si] = float(atr) / cl
        adtv20_m[ti, si] = float(r["adtv_20"] or 0.0)
        trades_m[ti, si] = float(r["n_trades"] or 0.0)
        listed_m[ti, si] = float(r["bars_seen"] or 0.0)
        if r["ret_1d"] is not None:
            ret1d_m[ti, si] = float(np.clip(float(r["ret_1d"]), -0.25, 0.25))
        if r["ret_63d"] is not None:
            ret63d_m[ti, si] = float(r["ret_63d"])
        if r["ret_126d"] is not None:
            ret126d_m[ti, si] = float(r["ret_126d"])
        if r["mom_12_1"] is not None:
            mom_12_1_m[ti, si] = float(r["mom_12_1"])
        if r["dist_52w_high"] is not None:
            dist_52w_high_m[ti, si] = float(r["dist_52w_high"])
        if r["close_in_range"] is not None:
            close_in_range_m[ti, si] = float(r["close_in_range"])
        ov, intr = r["overnight_ret_21d"], r["intraday_ret_21d"]
        if ov is not None and intr is not None:
            overnight_spread_21_m[ti, si] = float(ov) - float(intr)
        if r["vol_ratio_5_21"] is not None:
            vol_ratio_5_21_m[ti, si] = float(r["vol_ratio_5_21"])
        pv, rv63 = r["parkinson_vol_21d"], r["realised_vol_63d"]
        if pv is not None and rv63 is not None and float(rv63) > 1e-6:
            parkinson_ratio_m[ti, si] = float(pv) / float(rv63)
        if r["up_down_vol_ratio_20"] is not None:
            up_dn_vol_20_m[ti, si] = float(r["up_down_vol_ratio_20"])
        if r["turnover_ratio_5_20"] is not None:
            turnover_ratio_5_20_m[ti, si] = float(r["turnover_ratio_5_20"])
        if r["amihud_21d"] is not None:
            amihud_21_m[ti, si] = float(r["amihud_21d"])
        s50 = r["sma50"]
        sma50_above_m[ti, si] = 1.0 if (s50 is not None and cl > float(s50)) else 0.0
        s200 = r["sma200"]
        sma200_above_m[ti, si] = 1.0 if (s200 is not None and cl > float(s200)) else 0.0
        if r["t_drift_20"] is not None:
            t_drift_20_m[ti, si] = float(r["t_drift_20"])
        if r["t_drift_63"] is not None:
            t_drift_63_m[ti, si] = float(r["t_drift_63"])
        if r["t_drift_252"] is not None:
            t_drift_252_m[ti, si] = float(r["t_drift_252"])
        if r["er_63"] is not None:
            er_63_m[ti, si] = float(r["er_63"])

    # --- Market log-returns matching session index T ---
    mkt_ret = np.array(
        [
            math.exp(macro_map[d]["idx_log_ret"]) - 1.0 if d in macro_map else 0.0
            for d in dates
        ],
        dtype=np.float64,
    )

    # --- Vectorized Rolling 63d OLS Beta vs Nifty 500 (strictly PIT [t-62..t]) ---
    beta63_m = _empty(1.0)
    w_beta = 63
    ret_clean = np.nan_to_num(ret1d_m, nan=0.0)
    mkt_col = mkt_ret[:, None]
    for t in range(w_beta, T):
        rw = ret_clean[t - w_beta + 1 : t + 1]
        mw = mkt_col[t - w_beta + 1 : t + 1]
        mw_mean = float(np.mean(mw))
        mw_var = float(np.var(mw))
        if mw_var > 1e-10:
            rw_mean = np.mean(rw, axis=0)
            cov = np.mean((rw - rw_mean) * (mw - mw_mean), axis=0)
            beta63_m[t] = np.clip(cov / mw_var, 0.05, 2.0)

    # --- Market-stripped returns -> cross-sectional industry demeaned residual e_{t,s} ---
    mkt_stripped = ret_clean - beta63_m * mkt_col
    # Group columns by industry to subtract daily cross-sectional industry residual mean cleanly
    ind_to_cols: dict[str, list[int]] = {}
    for si, ind in enumerate(industries):
        ind_to_cols.setdefault(ind, []).append(si)

    res_ret_m = mkt_stripped.copy()
    valid_mask = ~np.isnan(close_m)
    for _ind, cols in ind_to_cols.items():
        if len(cols) >= 2:
            sub = mkt_stripped[:, cols]
            vm = valid_mask[:, cols]
            cnt = np.maximum(np.sum(vm, axis=1, keepdims=True), 1.0)
            ind_mean = np.sum(np.where(vm, sub, 0.0), axis=1, keepdims=True) / cnt
            res_ret_m[:, cols] = np.where(vm, sub - ind_mean, 0.0)

    # --- Blitz-Huij-Martens Residual Momentum (63d & 20d Sortino on e_{t,s}) ---
    res_mom_63_m = _empty(0.0)
    res_mom_20_m = _empty(0.0)
    for t in range(63, T):
        w63 = res_ret_m[t - 62 : t + 1]
        m63 = np.mean(w63, axis=0)
        dn63 = np.sqrt(np.mean(np.minimum(w63, 0.0) ** 2, axis=0)) + 0.002
        res_mom_63_m[t] = (m63 * 63.0) / (dn63 * math.sqrt(63.0))

        w20 = res_ret_m[t - 19 : t + 1]
        m20 = np.mean(w20, axis=0)
        dn20 = np.sqrt(np.mean(np.minimum(w20, 0.0) ** 2, axis=0)) + 0.002
        res_mom_20_m[t] = (m20 * 20.0) / (dn20 * math.sqrt(20.0))

    # --- Avellaneda & Lee (2010) Ornstein-Uhlenbeck Rolling 60d AR(1) on Cumulative Residuals ---
    # Cumulate market-and-industry-neutral residual returns, then fit X_{k+1} = a + b * X_k
    cum_res = np.cumsum(res_ret_m, axis=0)
    ou_s_m = _empty(0.0)
    ou_tau_m = _empty(99.0)
    ou_b_m = _empty(1.0)
    w_ou = 60
    for t in range(w_ou + 10, T):
        # X shape (60, S) for trailing window [t-59 .. t]; shift invariance keeps OLS invariant
        X_win = cum_res[t - w_ou + 1 : t + 1] - cum_res[t - w_ou : t - w_ou + 1]
        X0 = X_win[:-1]  # (59, S)
        X1 = X_win[1:]   # (59, S)
        m0 = np.mean(X0, axis=0)
        m1 = np.mean(X1, axis=0)
        d0 = X0 - m0
        d1 = X1 - m1
        var0 = np.sum(d0 * d0, axis=0)
        cov01 = np.sum(d0 * d1, axis=0)
        b = np.where(var0 > 1e-10, cov01 / np.maximum(var0, 1e-10), 1.0)
        a = m1 - b * m0
        resid = X1 - (a + b * X0)
        sig_eta = np.std(resid, axis=0, ddof=1)

        stationary = (b > 0.05) & (b < 0.94) & (sig_eta > 1e-6)
        b_safe = np.where(stationary, b, 0.5)
        mu = a / (1.0 - b_safe)
        sig_eq = sig_eta / np.sqrt(np.maximum(1.0 - b_safe * b_safe, 1e-6))
        theta = -np.log(b_safe)
        tau = math.log(2.0) / np.maximum(theta, 1e-4)
        s_val = (X_win[-1] - mu) / np.maximum(sig_eq, 1e-6)

        ou_b_m[t] = b
        ou_tau_m[t] = np.where(stationary, tau, 99.0)
        ou_s_m[t] = np.where(stationary, s_val, 0.0)

    # --- Precompute V1 Baseline Monthly Sortino Signals & Top-15 Tracking (exact score_universe_v2) ---
    v1_weights = load_challenger_weights()
    active_set = set(active_sessions)
    early_active = set(dates[max(0, len(dates) - 1510) :])
    eval_dates = sorted(early_active | active_set)
    monthly_rebal_set = set(monthly_rebalance_dates(dates))

    v1_scores_by_date: dict[date, list[tuple[str, str, float, float, float, float]]] = {}
    v1_top15_by_date: dict[date, set[str]] = {}
    panel_scored = panel_ind.filter(pl.col("d").is_in(eval_dates))
    by_d_panel = {d: sub for (d,), sub in panel_scored.partition_by("d", as_dict=True).items()}
    last_top15: set[str] = set()
    for d in eval_dates:
        ti_d = date_to_idx[d]
        if d in monthly_rebal_set or ti_d % 5 == 0:
            sub_d = by_d_panel.get(d)
            if sub_d is not None and not sub_d.is_empty():
                scored = score_universe_v2(sub_d, weights=v1_weights, use_pit_liquidity=True)
                if not scored.is_empty():
                    eligible = scored.filter(
                        (pl.col("close") > pl.col("sma50"))
                        & (pl.col("sma50") > pl.col("sma200"))
                        & (pl.col("mom_12_1") > v1_weights.min_mom_12_1)
                        & (pl.col("ret_126d") > v1_weights.min_ret_126d)
                    )
                    rows: list[tuple[str, str, float, float, float, float]] = []
                    for r in eligible.select(["symbol", "industry", "composite", "close", "atr14", "adtv_20"]).iter_rows(named=True):
                        rows.append((
                            str(r["symbol"]),
                            str(r["industry"]),
                            float(r["composite"]),
                            float(r["close"]),
                            float(r["atr14"] or r["close"] * 0.02),
                            float(r["adtv_20"] or 0.0),
                        ))
                    v1_scores_by_date[d] = rows
                    last_top15 = {r[0] for r in rows[:15]}
        v1_top15_by_date[d] = set(last_top15)

    # Precompute unfiltered score_universe_v2 output on all monthly rebalance dates (matching run_backtest_v2)
    v2_panel_full = _build_v2_panel(bars_eq, members)
    rebal_panels = v2_panel_full.filter(pl.col("d").is_in(sorted(monthly_rebal_set))).partition_by("d", as_dict=True)
    scored_by_rebal: dict[date, pl.DataFrame] = {}
    for rd in sorted(monthly_rebal_set):
        dp = rebal_panels.get((rd,))
        if dp is not None and not dp.is_empty():
            sc = score_universe_v2(dp, weights=v1_weights, use_pit_liquidity=True)
            if not sc.is_empty() and "rank" in sc.columns:
                scored_by_rebal[rd] = sc

    # --- Strictly PIT BarrierModel & Cross-Sectionally Centered Edge (exit_d <= d) ---
    path_bars = (
        panel_ind.filter(pl.col("symbol").is_in(symbols))
        .select(["symbol", "d", "close", "high", "low", "atr14"])
    )
    path_lib = build_path_library(path_bars, horizon=Horizon.SWING, k_up=1.5, k_dn=1.5)
    lib_by_exit = {d: df for (d,), df in path_lib.partition_by("exit_d", as_dict=True).items()}

    tercile_barriers_by_date: dict[date, dict[int, tuple[float, float, float, bool]]] = {}
    tercile_cuts_by_date: dict[date, tuple[float, float]] = {}
    sym_barrier_edge_m = _empty(0.0)

    exit_window: list[tuple[date, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
    sym_up_cnt = np.zeros(S, dtype=np.float64)
    sym_dn_cnt = np.zeros(S, dtype=np.float64)
    sym_tot_w = np.zeros(S, dtype=np.float64)

    for ti, d in enumerate(dates):
        part = lib_by_exit.get(d)
        if part is not None and not part.is_empty():
            syms_p = [sym_to_idx.get(s, -1) for s in part["symbol"].to_list()]
            valid_idx = [i for i, sidx in enumerate(syms_p) if sidx >= 0]
            if valid_idx:
                s_arr = np.array([syms_p[i] for i in valid_idx], dtype=np.int32)
                outs = part["outcome"].to_list()
                o_arr = np.array(
                    [1 if outs[i] == "UP" else (-1 if outs[i] == "DOWN" else 0) for i in valid_idx],
                    dtype=np.int8,
                )
                ap_vals = part["atr_pct"].to_numpy()
                va_vals = part["v_atr"].to_numpy()
                ap_arr = np.nan_to_num(ap_vals[valid_idx].astype(np.float64), nan=0.02)
                va_arr = np.nan_to_num(va_vals[valid_idx].astype(np.float64), nan=0.0)
                w_each = 1.0 / 20.0
                np.add.at(sym_up_cnt, s_arr[o_arr == 1], w_each)
                np.add.at(sym_dn_cnt, s_arr[o_arr == -1], w_each)
                np.add.at(sym_tot_w, s_arr, w_each)
                exit_window.append((d, s_arr, o_arr, ap_arr, va_arr))

        while len(exit_window) > 252:
            _, s_old, o_old, _, _ = exit_window.pop(0)
            w_each = 1.0 / 20.0
            np.add.at(sym_up_cnt, s_old[o_old == 1], -w_each)
            np.add.at(sym_dn_cnt, s_old[o_old == -1], -w_each)
            np.add.at(sym_tot_w, s_old, -w_each)

        if d in eval_dates and exit_window:
            all_o = np.concatenate([x[2] for x in exit_window])
            all_ap = np.concatenate([x[3] for x in exit_window])
            all_va = np.concatenate([x[4] for x in exit_window])
            w_each = 1.0 / 20.0
            q1 = float(np.quantile(all_ap, 1.0 / 3.0))
            q2 = float(np.quantile(all_ap, 2.0 / 3.0))
            tercile_cuts_by_date[d] = (q1, q2)

            p_up_g = float(np.sum(all_o == 1) + 1.0) / float(len(all_o) + 3.0)
            p_dn_g = float(np.sum(all_o == -1) + 1.0) / float(len(all_o) + 3.0)

            kappa = 30.0
            tmap: dict[int, tuple[float, float, float, bool]] = {}
            for tc in (0, 1, 2):
                mask = (all_ap <= q1) if tc == 0 else ((all_ap > q1) & (all_ap <= q2) if tc == 1 else (all_ap > q2))
                n_cell = float(np.sum(mask)) * w_each
                u_cell = float(np.sum(mask & (all_o == 1))) * w_each
                d_cell = float(np.sum(mask & (all_o == -1))) * w_each
                denom = n_cell + kappa
                pu = (u_cell + kappa * p_up_g) / max(denom, 1.0)
                pd = (d_cell + kappa * p_dn_g) / max(denom, 1.0)
                er = float(np.mean(all_va[mask])) if np.any(mask) else 0.0
                tmap[tc] = (pu, pd, er, n_cell >= 50.0)
            tercile_barriers_by_date[d] = tmap

            ks = 6.0
            sym_pu = (sym_up_cnt + ks * p_up_g) / np.maximum(sym_tot_w + ks, 1.0)
            sym_pd = (sym_dn_cnt + ks * p_dn_g) / np.maximum(sym_tot_w + ks, 1.0)
            raw_edge = sym_pu - sym_pd
            sym_barrier_edge_m[ti] = raw_edge - float(np.nanmedian(raw_edge))

    return ResearchDataStore(
        dates=dates,
        date_to_idx=date_to_idx,
        symbols=symbols,
        sym_to_idx=sym_to_idx,
        industries=industries,
        bars_by_date=bars_by_date,
        macro_map=macro_map,
        v1_scores_by_date=v1_scores_by_date,
        v1_top15_by_date=v1_top15_by_date,
        monthly_rebal_set=monthly_rebal_set,
        open_m=open_m,
        high_m=high_m,
        low_m=low_m,
        close_m=close_m,
        atr14_m=atr14_m,
        natr14_m=natr14_m,
        adtv20_m=adtv20_m,
        trades_m=trades_m,
        listed_m=listed_m,
        ret1d_m=ret1d_m,
        ret63d_m=ret63d_m,
        ret126d_m=ret126d_m,
        mom_12_1_m=mom_12_1_m,
        dist_52w_high_m=dist_52w_high_m,
        close_in_range_m=close_in_range_m,
        overnight_spread_21_m=overnight_spread_21_m,
        vol_ratio_5_21_m=vol_ratio_5_21_m,
        parkinson_ratio_m=parkinson_ratio_m,
        up_dn_vol_20_m=up_dn_vol_20_m,
        turnover_ratio_5_20_m=turnover_ratio_5_20_m,
        amihud_21_m=amihud_21_m,
        sma50_above_m=sma50_above_m,
        sma200_above_m=sma200_above_m,
        t_drift_20_m=t_drift_20_m,
        t_drift_63_m=t_drift_63_m,
        t_drift_252_m=t_drift_252_m,
        er_63_m=er_63_m,
        beta63_m=beta63_m,
        res_ret_m=res_ret_m,
        res_mom_63_m=res_mom_63_m,
        res_mom_20_m=res_mom_20_m,
        ou_s_m=ou_s_m,
        ou_tau_m=ou_tau_m,
        ou_b_m=ou_b_m,
        tercile_barriers_by_date=tercile_barriers_by_date,
        sym_barrier_edge_m=sym_barrier_edge_m,
        tercile_cuts_by_date=tercile_cuts_by_date,
        scored_by_rebal=scored_by_rebal,
    )


# ---------------------------------------------------------------------------
# 3. Signal Candidates (Bespoke Quantitative Signals, N=5 Variants)
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SignalCandidate:
    variant: str
    symbol: str
    sym_idx: int
    industry: str
    score: float         # variant-internal Z/conviction score (>= 0)
    close: float
    atr14: float
    stop_dist: float     # absolute price risk per share
    target_dist: float
    max_sessions: int
    beta63: float
    adtv20: float
    horizon: Horizon = Horizon.SWING
    sleeve: Sleeve = Sleeve.MOMENTUM_BREAKOUT
    reasons: tuple[str, ...] = ()


def _liquid_mask(ds: ResearchDataStore, ti: int) -> np.ndarray:
    return (
        ~np.isnan(ds.close_m[ti])
        & (ds.close_m[ti] >= DEFAULT.min_price)
        & (ds.atr14_m[ti] > 0.0)
        & (ds.natr14_m[ti] <= 0.065)
        & (ds.adtv20_m[ti] >= DEFAULT.min_adtv_rupees)
        & (ds.trades_m[ti] >= DEFAULT.min_trades_per_day)
        & (ds.listed_m[ti] >= DEFAULT.min_listed_sessions)
    )


def _z_score(vals: np.ndarray) -> np.ndarray:
    if len(vals) <= 1:
        return np.zeros_like(vals)
    mu = float(np.nanmean(vals))
    sd = float(np.nanstd(vals))
    if not math.isfinite(sd) or sd < 1e-9:
        return np.zeros_like(vals)
    return np.clip((np.nan_to_num(vals, nan=mu) - mu) / sd, -3.5, 3.5)


def generate_variant_candidates(
    ds: ResearchDataStore,
    d: date,
    variant: str,
    *,
    allow_v1_weekly: bool = False,
    allow_ens_rebal: bool = False,
    exclude_v1_top15: bool = True,
) -> list[SignalCandidate]:
    """Generate sorted SignalCandidates on close of session `d` (0 lookahead)."""
    ti = ds.date_to_idx[d]
    macro = ds.macro_map.get(
        d,
        {
            "macro_regime": "SIDEWAYS_CHOP",
            "vix_pct": 50.0,
            "breadth_50": 0.5,
            "breadth_turn": 0.0,
            "idx_ret_20d": 0.0,
            "idx_ret_5d": 0.0,
            "idx_above_sma20": True,
            "vix_chg_5d": 0.0,
            "macro_recovery": False,
            "idx_log_ret": 0.0,
        },
    )
    reg = macro["macro_regime"]
    vix_pct = float(macro.get("vix_pct", 50.0))
    breadth_50 = float(macro.get("breadth_50", 0.5))
    b_turn = float(macro.get("breadth_turn", 0.0))
    idx_ret_20d = float(macro.get("idx_ret_20d", 0.0))
    idx_ret1d = math.exp(float(macro.get("idx_log_ret", 0.0))) - 1.0
    macro_rec = bool(macro.get("macro_recovery", False))

    if variant == "V1_Baseline_Sortino":
        if reg == "BEAR_HIGH_VOL_SHOCK" and allow_v1_weekly:
            return []
        if allow_v1_weekly:
            if d not in ds.v1_scores_by_date:
                return []
        elif d not in ds.monthly_rebal_set:
            return []
        rows = ds.v1_scores_by_date.get(d, [])
        cands: list[SignalCandidate] = []
        for sym, ind, comp, cl, atr, adtv in rows[:25]:
            si = ds.sym_to_idx.get(sym, -1)
            if si < 0:
                continue
            stop_d = min(cl * 0.105, max(cl * 0.045, 2.8 * atr))
            cands.append(SignalCandidate(
                variant=variant,
                symbol=sym,
                sym_idx=si,
                industry=ind,
                score=max(0.05, comp + 1.5),
                close=cl,
                atr14=atr,
                stop_dist=stop_d,
                target_dist=cl * 1.25,
                max_sessions=252,
                beta63=float(ds.beta63_m[ti, si]),
                adtv20=adtv,
                horizon=Horizon.POSITIONAL,
                sleeve=Sleeve.MOMENTUM_BREAKOUT,
                reasons=("V1_SORTINO_MOM",),
            ))
        return cands

    elig = _liquid_mask(ds, ti)
    if not np.any(elig):
        return []
    v1_top15 = ds.v1_top15_by_date.get(d, set()) if exclude_v1_top15 else set()

    if variant == "V2_Residual_Alpha_HAC":
        # Blitz-Huij-Martens factor-neutral residual momentum + Kaufman efficiency + Newey-West HAC.
        if reg == "BEAR_HIGH_VOL_SHOCK" or breadth_50 < 0.42:
            return []
        if not allow_ens_rebal and (ti % 10 != 0):
            return []
        beta_cap = 0.85 if reg == "HIGH_VOL_TREND" else 0.90
        mask = (
            elig
            & (ds.sma200_above_m[ti] > 0.5)
            & (ds.beta63_m[ti] <= beta_cap)
            & (ds.res_mom_63_m[ti] > 0.25)
            & (ds.er_63_m[ti] >= 0.30)
            & (ds.t_drift_63_m[ti] >= 1.5)
        )
        idxs = [int(si) for si in np.where(mask)[0] if ds.symbols[int(si)] not in v1_top15]
        if not idxs:
            return []
        idx_arr = np.array(idxs, dtype=np.int32)
        z_rm = _z_score(ds.res_mom_63_m[ti, idx_arr])
        z_er = _z_score(ds.er_63_m[ti, idx_arr])
        z_td = _z_score(ds.t_drift_63_m[ti, idx_arr])
        raw_sc = 0.45 * z_rm + 0.35 * z_er + 0.20 * z_td
        order_pos = np.argsort(-raw_sc)[:25]
        cands = []
        for pos in order_pos:
            si = int(idx_arr[pos])
            cl = float(ds.close_m[ti, si])
            atr = float(ds.atr14_m[ti, si])
            stop_d = min(cl * 0.100, max(cl * 0.042, 2.8 * atr))
            cands.append(SignalCandidate(
                variant=variant,
                symbol=ds.symbols[si],
                sym_idx=si,
                industry=ds.industries[si],
                score=float(2.0 + raw_sc[pos]),
                close=cl,
                atr14=atr,
                stop_dist=stop_d,
                target_dist=cl * 1.15,
                max_sessions=120,
                beta63=float(ds.beta63_m[ti, si]),
                adtv20=float(ds.adtv20_m[ti, si]),
                horizon=Horizon.POSITIONAL,
                sleeve=Sleeve.MOMENTUM_BREAKOUT,
                reasons=("V2_RESIDUAL_HAC", "V2_LOW_BETA_ALPHA"),
            ))
        return cands

    if variant == "V3_OU_Residual_Reversion":
        # Avellaneda & Lee OU equilibrium statistical arbitrage inside stationary sideways chop.
        if reg != "SIDEWAYS_CHOP" or breadth_50 < 0.42 or (ti % 3 != 0):
            return []
        amihud_med = float(np.nanmedian(ds.amihud_21_m[ti, elig])) if np.any(elig & ~np.isnan(ds.amihud_21_m[ti])) else 1e9
        mask = (
            elig
            & (ds.sma200_above_m[ti] > 0.5)
            & (ds.ou_tau_m[ti] >= 2.0)
            & (ds.ou_tau_m[ti] <= 14.0)
            & (ds.ou_s_m[ti] <= -1.25)
            & (ds.er_63_m[ti] >= 0.20)
            & (ds.sym_barrier_edge_m[ti] >= -0.01)
            & (ds.close_in_range_m[ti] >= 0.50)
            & (ds.ret1d_m[ti] > -0.04)
            & (np.nan_to_num(ds.amihud_21_m[ti], nan=amihud_med) <= amihud_med * 1.40)
        )
        idxs = [int(si) for si in np.where(mask)[0] if ds.symbols[int(si)] not in v1_top15]
        if not idxs:
            return []
        idx_arr = np.array(idxs, dtype=np.int32)
        z_ou = _z_score(-ds.ou_s_m[ti, idx_arr])
        z_be = _z_score(ds.sym_barrier_edge_m[ti, idx_arr])
        z_er = _z_score(ds.er_63_m[ti, idx_arr])
        raw_sc = 0.50 * z_ou + 0.30 * z_be + 0.20 * z_er
        order_pos = np.argsort(-raw_sc)[:18]
        cands = []
        for pos in order_pos:
            si = int(idx_arr[pos])
            cl = float(ds.close_m[ti, si])
            atr = float(ds.atr14_m[ti, si])
            tau = float(ds.ou_tau_m[ti, si])
            stop_d = min(cl * 0.068, max(cl * 0.028, 2.0 * atr))
            max_h = int(max(12, min(22, math.ceil(2.0 * tau))))
            cands.append(SignalCandidate(
                variant=variant,
                symbol=ds.symbols[si],
                sym_idx=si,
                industry=ds.industries[si],
                score=float(2.0 + raw_sc[pos]),
                close=cl,
                atr14=atr,
                stop_dist=stop_d,
                target_dist=cl * 0.35,
                max_sessions=max_h,
                beta63=float(ds.beta63_m[ti, si]),
                adtv20=float(ds.adtv20_m[ti, si]),
                horizon=Horizon.SWING,
                sleeve=Sleeve.MEAN_REVERSION,
                reasons=("V3_OU_EQUILIBRIUM", "V3_BARRIER_FILTER"),
            ))
        return cands

    if variant == "V4_VolCompression_Barrier":
        # Symmetric PIT Dirichlet Barrier Posterior + Institutional Volume-Range Thrust (Trending Regimes).
        trend_active = (reg in ("LOW_VOL_TREND", "HIGH_VOL_TREND")) or (
            reg == "SIDEWAYS_CHOP" and breadth_50 >= 0.62 and idx_ret_20d > 0.02
        )
        if not trend_active:
            return []
        if not allow_ens_rebal and (ti % 5 != 0):
            return []
        tmap = ds.tercile_barriers_by_date.get(d)
        cuts = ds.tercile_cuts_by_date.get(d)
        if not tmap or not cuts:
            return []
        q1, q2 = cuts
        mask = (
            elig
            & (ds.sma200_above_m[ti] > 0.5)
            & (ds.sma50_above_m[ti] > 0.5)
            & (ds.sym_barrier_edge_m[ti] > 0.025)
            & (ds.up_dn_vol_20_m[ti] >= 1.15)
            & (ds.er_63_m[ti] >= 0.28)
            & (ds.close_in_range_m[ti] >= 0.55)
        )
        raw_idxs = [int(si) for si in np.where(mask)[0] if ds.symbols[int(si)] not in v1_top15]
        valid_sis: list[int] = []
        for si in raw_idxs:
            ap = float(ds.natr14_m[ti, si])
            tc = 0 if ap <= q1 else (1 if ap <= q2 else 2)
            _pu, _pd, er, ok = tmap[tc]
            if ok and er >= -0.05:
                valid_sis.append(si)
        if not valid_sis:
            return []
        idx_arr = np.array(valid_sis, dtype=np.int32)
        z_be = _z_score(ds.sym_barrier_edge_m[ti, idx_arr])
        z_uv = _z_score(ds.up_dn_vol_20_m[ti, idx_arr])
        z_er = _z_score(ds.er_63_m[ti, idx_arr])
        raw_sc = 0.50 * z_be + 0.30 * z_uv + 0.20 * z_er
        order_pos = np.argsort(-raw_sc)[:25]
        cands = []
        for pos in order_pos:
            si = int(idx_arr[pos])
            cl = float(ds.close_m[ti, si])
            atr = float(ds.atr14_m[ti, si])
            stop_d = min(cl * 0.100, max(cl * 0.042, 2.8 * atr))
            cands.append(SignalCandidate(
                variant=variant,
                symbol=ds.symbols[si],
                sym_idx=si,
                industry=ds.industries[si],
                score=float(2.0 + raw_sc[pos]),
                close=cl,
                atr14=atr,
                stop_dist=stop_d,
                target_dist=cl * 1.15,
                max_sessions=120,
                beta63=float(ds.beta63_m[ti, si]),
                adtv20=float(ds.adtv20_m[ti, si]),
                horizon=Horizon.POSITIONAL,
                sleeve=Sleeve.SECTOR_ROTATION,
                reasons=("V4_DIRICHLET_BARRIER", "V4_ACCUMULATION_THRUST"),
            ))
        return cands

    if variant == "V5_Defensive_CrisisShield":
        # Strictly activated on validated post-shock reversal or oversold breadth inflection.
        if reg == "BEAR_HIGH_VOL_SHOCK":
            if not (macro_rec or (vix_pct >= 75.0 and idx_ret1d > 0.005 and b_turn >= 0.025)):
                return []
        else:
            if not (breadth_50 < 0.44 and b_turn >= 0.015):
                return []
        mask = (
            elig
            & (ds.overnight_spread_21_m[ti] > 0.0)
            & (ds.er_63_m[ti] >= 0.22)
            & (ds.ou_s_m[ti] <= -1.10)
            & (ds.close_in_range_m[ti] >= 0.58)
            & (ds.ret1d_m[ti] > 0.0)
        )
        idxs = [int(si) for si in np.where(mask)[0] if ds.symbols[int(si)] not in v1_top15]
        if not idxs:
            return []
        idx_arr = np.array(idxs, dtype=np.int32)
        z_ov = _z_score(ds.overnight_spread_21_m[ti, idx_arr])
        z_er = _z_score(ds.er_63_m[ti, idx_arr])
        z_ou = _z_score(-ds.ou_s_m[ti, idx_arr])
        raw_sc = 0.40 * z_ov + 0.35 * z_er + 0.25 * z_ou
        order_pos = np.argsort(-raw_sc)[:15]
        cands = []
        for pos in order_pos:
            si = int(idx_arr[pos])
            cl = float(ds.close_m[ti, si])
            atr = float(ds.atr14_m[ti, si])
            stop_d = min(cl * 0.068, max(cl * 0.028, 2.0 * atr))
            cands.append(SignalCandidate(
                variant=variant,
                symbol=ds.symbols[si],
                sym_idx=si,
                industry=ds.industries[si],
                score=float(2.0 + raw_sc[pos]),
                close=cl,
                atr14=atr,
                stop_dist=stop_d,
                target_dist=cl * 0.35,
                max_sessions=18,
                beta63=float(ds.beta63_m[ti, si]),
                adtv20=float(ds.adtv20_m[ti, si]),
                horizon=Horizon.SWING,
                sleeve=Sleeve.PULLBACK_IN_UPTREND,
                reasons=("V5_CRISIS_SHIELD", "V5_OVERNIGHT_INFLECTION"),
            ))
        return cands

    raise ValueError(f"Unknown variant: {variant}")


# ---------------------------------------------------------------------------
# 4. 4-Tier Hierarchical Risk Engine & Dynamic Pairwise Correlation Check
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TieredRiskSpec:
    # Tier 1: Portfolio ceiling
    max_open_risk_pct: float = 0.070      # HARD CEILING <= 7.0% open portfolio risk
    max_positions: int = 15               # Dynamic 12-15 concurrent slots
    min_notional: float = 20_000.0        # Covers Rs 15.34 DP floor (<= 7.7 bps)
    max_notional_pct: float = 0.14        # <= 14% equity per position
    # Tier 2: Single-trade risk (inverse-vol sized)
    base_trade_risk_pct: float = 0.0062   # 0.62% baseline
    max_trade_risk_pct: float = 0.010     # HARD CEILING <= 1.0% per stock
    # Tier 3: Sector cluster caps (solves 1% industry bottleneck)
    max_sector_open_risk_pct: float = 0.030  # <= 3.0% open risk per industry
    max_sector_names: int = 3                # up to 3 top names per leading industry
    max_sector_capital_pct: float = 0.25     # <= 25% gross capital per industry
    # Tier 4: Dynamic correlation & beta guards
    corr_lookback: int = 60
    corr_halve_threshold: float = 0.70    # > 0.70 halves position size; > 0.82 rejects
    corr_reject_threshold: float = 0.82
    max_portfolio_beta: float = 1.35      # portfolio market beta <= 1.35


TIERED_SPEC = TieredRiskSpec()


def _pit_pair_corr_60d(ds: ResearchDataStore, ti: int, si_a: int, si_b: int, lookback: int = 60) -> float:
    if ti < lookback:
        return 0.0
    ra = ds.res_ret_m[ti - lookback + 1 : ti + 1, si_a]
    rb = ds.res_ret_m[ti - lookback + 1 : ti + 1, si_b]
    sa = float(np.std(ra))
    sb = float(np.std(rb))
    if sa < 1e-8 or sb < 1e-8:
        return 0.0
    return float(np.dot(ra - np.mean(ra), rb - np.mean(rb)) / (len(ra) * sa * sb))


def _lot_open_risk_rupees(lot: Any, cur_price: float) -> float:
    ref_p = min(float(lot.entry_price), float(cur_price))
    return max(0.0, ref_p - float(lot.stop)) * int(lot.qty)


def _portfolio_risk_metrics(broker: PaperBroker, ds: ResearchDataStore, ti: int) -> dict[str, Any]:
    d = ds.dates[ti]
    bmap = ds.bars_by_date.get(d, {})
    eq = broker.cash
    lot_vals: dict[str, float] = {}
    for oid, lot in broker.open_lots.items():
        bar = bmap.get(lot.symbol)
        lp = bar.close if bar is not None else lot.last_price
        lot_vals[oid] = lp
        eq += lp * lot.qty

    open_risk = 0.0
    gross = 0.0
    sec_risk: dict[str, float] = {}
    sec_cap: dict[str, float] = {}
    sec_names: dict[str, int] = {}
    weighted_beta = 0.0
    for oid, lot in broker.open_lots.items():
        lp = lot_vals[oid]
        r_rs = _lot_open_risk_rupees(lot, lp)
        notional = lp * lot.qty
        open_risk += r_rs
        gross += notional
        ind = lot.industry or "UNKNOWN"
        sec_risk[ind] = sec_risk.get(ind, 0.0) + r_rs
        sec_cap[ind] = sec_cap.get(ind, 0.0) + notional
        sec_names[ind] = sec_names.get(ind, 0) + 1
        si = ds.sym_to_idx.get(lot.symbol, -1)
        b_sym = float(ds.beta63_m[ti, si]) if si >= 0 else 1.0
        weighted_beta += (notional / max(eq, 1.0)) * b_sym

    for oid, ord_obj in broker.pending_orders.items():
        r_rs = max(0.0, float(ord_obj.entry_hi) - float(ord_obj.stop)) * int(ord_obj.qty)
        notional = float(ord_obj.entry_hi) * int(ord_obj.qty)
        open_risk += r_rs
        gross += notional
        ind = ord_obj.industry or "UNKNOWN"
        sec_risk[ind] = sec_risk.get(ind, 0.0) + r_rs
        sec_cap[ind] = sec_cap.get(ind, 0.0) + notional
        sec_names[ind] = sec_names.get(ind, 0) + 1
        si = ds.sym_to_idx.get(ord_obj.symbol, -1)
        b_sym = float(ds.beta63_m[ti, si]) if si >= 0 else 1.0
        weighted_beta += (notional / max(eq, 1.0)) * b_sym

    return {
        "equity": max(eq, 1.0),
        "open_risk_pct": open_risk / max(eq, 1.0),
        "gross_pct": gross / max(eq, 1.0),
        "sec_risk": sec_risk,
        "sec_cap": sec_cap,
        "sec_names": sec_names,
        "portfolio_beta": weighted_beta,
    }


def _regime_risk_envelope(reg: str) -> dict[str, float]:
    """Hierarchical regime risk budgets (always <= 7.0% hard portfolio ceiling)."""
    if reg == "LOW_VOL_TREND":
        return {"heat": 0.070, "gross": 0.94, "slots": 15, "max_new": 8, "max_beta": 1.35}
    if reg == "HIGH_VOL_TREND":
        return {"heat": 0.058, "gross": 0.84, "slots": 12, "max_new": 6, "max_beta": 1.28}
    if reg == "SIDEWAYS_CHOP":
        return {"heat": 0.065, "gross": 0.90, "slots": 14, "max_new": 7, "max_beta": 1.30}
    # BEAR_HIGH_VOL_SHOCK: strict capital preservation shield
    return {"heat": 0.020, "gross": 0.30, "slots": 4, "max_new": 2, "max_beta": 0.55}


def _legacy_risk_envelope(reg: str) -> dict[str, float]:
    if reg == "BEAR_HIGH_VOL_SHOCK":
        return {"heat": 0.020, "gross": 0.30, "slots": 4, "max_new": 1}
    if reg == "HIGH_VOL_TREND":
        return {"heat": 0.045, "gross": 0.75, "slots": 7, "max_new": 2}
    if reg == "SIDEWAYS_CHOP":
        return {"heat": 0.045, "gross": 0.80, "slots": 7, "max_new": 2}
    return {"heat": 0.060, "gross": 0.92, "slots": 7, "max_new": 3}


# ---------------------------------------------------------------------------
# 5. Unified Strategy Simulator (Exact PaperBroker + Daily Open-Risk Ratchet)
# ---------------------------------------------------------------------------
STRUCTURAL_REGIME_PRIORS: dict[str, dict[str, float]] = {
    "LOW_VOL_TREND": {
        "V1_Baseline_Sortino": 0.42,
        "V2_Residual_Alpha_HAC": 0.22,
        "V3_OU_Residual_Reversion": 0.00,
        "V4_VolCompression_Barrier": 0.36,
        "V5_Defensive_CrisisShield": 0.00,
    },
    "HIGH_VOL_TREND": {
        "V1_Baseline_Sortino": 0.48,
        "V2_Residual_Alpha_HAC": 0.20,
        "V3_OU_Residual_Reversion": 0.00,
        "V4_VolCompression_Barrier": 0.18,
        "V5_Defensive_CrisisShield": 0.14,
    },
    "SIDEWAYS_CHOP": {
        "V1_Baseline_Sortino": 0.48,
        "V2_Residual_Alpha_HAC": 0.28,
        "V3_OU_Residual_Reversion": 0.14,
        "V4_VolCompression_Barrier": 0.00,
        "V5_Defensive_CrisisShield": 0.10,
    },
    "BEAR_HIGH_VOL_SHOCK": {
        "V1_Baseline_Sortino": 0.00,
        "V2_Residual_Alpha_HAC": 0.00,
        "V3_OU_Residual_Reversion": 0.00,
        "V4_VolCompression_Barrier": 0.00,
        "V5_Defensive_CrisisShield": 1.00,
    },
}


def _simulate_ensemble_book(
    ds: ResearchDataStore,
    sessions: list[date],
    *,
    regime_weights_by_session: dict[date, dict[str, dict[str, float]]] | None = None,
    use_tiered_risk: bool = True,
    initial_equity: float = 500_000.0,
    eval_start_d: date | None = None,
) -> dict[str, Any]:
    """Run causal Meta-Voter multi-variant ensemble (`V1..V5`) with 4-Tier vs Legacy Risk."""
    _ = regime_weights_by_session  # walk-forward calibration metadata preserved in runner
    w = load_challenger_weights()
    broker = PaperBroker(initial_equity=initial_equity, arm=Arm.DETERMINISTIC)
    ts0 = datetime(2021, 1, 1, tzinfo=timezone.utc)

    if use_tiered_risk:
        max_slots = 12
        max_per_ind = 3
        max_sec_risk_pct = 0.030
        max_port_heat_entry = 0.068
        max_v1_risk_pct = 0.0098
        max_tac_risk_pct = 0.0075
        min_v1_notional = 25_000.0
        min_tac_notional = 20_000.0
        target_slots_div = 9.0
    else:
        max_slots = 7
        max_per_ind = 1
        max_sec_risk_pct = 0.010
        max_port_heat_entry = 0.060
        max_v1_risk_pct = 0.010
        max_tac_risk_pct = 0.010
        min_v1_notional = 50_000.0
        min_tac_notional = 50_000.0
        target_slots_div = 7.0

    v24_max_sess = 42
    bear_tr = 0.09
    v24_tr = 0.17

    max_observed_open_risk_pct = 0.0
    max_observed_beta = 0.0
    max_observed_positions = 0
    lot_var: dict[str, str] = {}
    rejection_counts: dict[str, int] = {}
    first_day = True
    seq = 0

    for d in sessions:
        ti = ds.date_to_idx[d]
        bmap = ds.bars_by_date.get(d, {})
        broker.step_session(d, bmap, run_id=f"sim_ens_{'4tier' if use_tiered_risk else 'legacy'}", ts=ts0)

        macro = ds.macro_map.get(
            d, {"macro_regime": "SIDEWAYS_CHOP", "breadth_50": 0.5, "idx_ret_20d": 0.0, "vix_pct": 50.0}
        )
        reg = macro["macro_regime"]
        b50 = float(macro.get("breadth_50", 0.5))

        for oid, lot in list(broker.open_lots.items()):
            bar = bmap.get(lot.symbol)
            if bar is None:
                continue
            lp = float(bar.close)
            vn = lot_var.get(oid, "V1")
            si = ds.sym_to_idx.get(lot.symbol, -1)
            if vn in ("V3", "V5"):
                if lot.max_high_seen > lot.entry_price * 1.04:
                    cs = max(round(lot.entry_price * 1.002, 2), round(lot.max_high_seen * 0.94, 2))
                    if cs > lot.stop and cs < lp * 0.998:
                        broker.trail_stop(oid, cs, run_id="sim_ens_trail", ts=ts0)
                if vn == "V3" and si >= 0 and lot.sessions_held >= 4 and float(ds.ou_s_m[ti, si]) >= 0.45:
                    if oid not in broker.pending_exits:
                        broker.schedule_exit(oid, reason="OU_EXIT")
            elif vn == "V24":
                eff_trail = bear_tr if (reg == "BEAR_HIGH_VOL_SHOCK" and b50 < 0.35) else v24_tr
                if lot.max_high_seen > lot.entry_price * 1.08:
                    cs = round(lot.max_high_seen * (1.0 - eff_trail), 2)
                    if cs > lot.stop and cs < lp * 0.998:
                        broker.trail_stop(oid, cs, run_id="sim_ens_trail", ts=ts0)
            else:
                eff_trail = bear_tr if (reg == "BEAR_HIGH_VOL_SHOCK" and b50 < 0.35) else 0.17
                if lot.max_high_seen > lot.entry_price * 1.08:
                    cs = round(lot.max_high_seen * (1.0 - eff_trail), 2)
                    if cs > lot.stop and cs < lp * 0.998:
                        broker.trail_stop(oid, cs, run_id="sim_ens_trail", ts=ts0)

        eq = broker.cash + sum(
            (bmap[l.symbol].close if l.symbol in bmap else l.last_price) * l.qty
            for l in broker.open_lots.values()
        )
        open_risk_rs = sum(
            _lot_open_risk_rupees(l, bmap[l.symbol].close if l.symbol in bmap else l.last_price)
            for l in broker.open_lots.values()
        )
        if use_tiered_risk and eq > 0 and open_risk_rs > 0.0694 * eq and broker.open_lots:
            scale = (0.0690 * eq) / open_risk_rs
            for oid, l in list(broker.open_lots.items()):
                cp = bmap[l.symbol].close if l.symbol in bmap else l.last_price
                ref_p = min(l.entry_price, cp)
                if ref_p > l.stop:
                    ns = round(ref_p - (ref_p - l.stop) * scale, 2)
                    if ns > l.stop and ns < cp * 0.995:
                        broker.trail_stop(oid, ns, run_id="sim_cap_ratchet", ts=ts0)
            open_risk_rs = sum(
                _lot_open_risk_rupees(l, bmap[l.symbol].close if l.symbol in bmap else l.last_price)
                for l in broker.open_lots.values()
            )

        port_beta = 0.0
        for l in broker.open_lots.values():
            cp = bmap[l.symbol].close if l.symbol in bmap else l.last_price
            si = ds.sym_to_idx.get(l.symbol, -1)
            b_sym = float(ds.beta63_m[ti, si]) if (si >= 0 and not math.isnan(ds.beta63_m[ti, si])) else 1.0
            port_beta += ((cp * l.qty) / max(eq, 1.0)) * b_sym

        if eval_start_d is None or d >= eval_start_d:
            max_observed_open_risk_pct = max(max_observed_open_risk_pct, open_risk_rs / max(eq, 1.0))
            max_observed_beta = max(max_observed_beta, port_beta)
            max_observed_positions = max(max_observed_positions, len(broker.open_lots))

        is_rebal = (d in ds.monthly_rebal_set) or first_day
        is_tactical = ti % 4 == 0
        if not (is_rebal or is_tactical):
            continue
        first_day = False

        for old_oid in list(broker.pending_orders.keys()):
            del broker.pending_orders[old_oid]

        sc_df = ds.scored_by_rebal.get(d)
        rank_map = (
            {r["symbol"]: int(r["rank"]) for r in sc_df.select(["symbol", "rank"]).iter_rows(named=True)}
            if (sc_df is not None and not sc_df.is_empty())
            else {}
        )

        exiting_notional = 0.0
        surviving_syms: set[str] = set()
        open_sis: list[int] = []
        ind_counts: dict[str, int] = {}
        sec_cap: dict[str, float] = {}
        sec_risk: dict[str, float] = {}
        cur_open_risk_rs = 0.0
        for oid, lot in list(broker.open_lots.items()):
            vn = lot_var.get(oid, "V1")
            si = ds.sym_to_idx.get(lot.symbol, -1)
            s200_ok = float(ds.sma200_above_m[ti, si]) > 0.5 if si >= 0 else True
            s50_ok = float(ds.sma50_above_m[ti, si]) > 0.5 if si >= 0 else True
            cp = bmap[lot.symbol].close if lot.symbol in bmap else lot.last_price
            rk = rank_map.get(lot.symbol, 999_999)
            if is_rebal and vn == "V1" and (rk > 25 or not s200_ok):
                broker.schedule_exit(oid, reason="RANK_OR_SMA200")
                exiting_notional += cp * lot.qty
            elif is_rebal and vn == "V24" and (not s200_ok or (not s50_ok and cp < lot.entry_price)):
                broker.schedule_exit(oid, reason="V24_TREND_BREAK")
                exiting_notional += cp * lot.qty
            elif oid in broker.pending_exits:
                exiting_notional += cp * lot.qty
            else:
                surviving_syms.add(lot.symbol)
                if si >= 0:
                    open_sis.append(si)
                ind_counts[lot.industry] = ind_counts.get(lot.industry, 0) + 1
                sec_cap[lot.industry] = sec_cap.get(lot.industry, 0.0) + cp * lot.qty
                r_lot = _lot_open_risk_rupees(lot, cp)
                sec_risk[lot.industry] = sec_risk.get(lot.industry, 0.0) + r_lot
                cur_open_risk_rs += r_lot

        slots_free = max_slots - len(surviving_syms)
        if slots_free <= 0:
            rejection_counts["SLOTS_FULL"] = rejection_counts.get("SLOTS_FULL", 0) + 1
            continue
        avail_cash = broker.cash + exiting_notional * 0.85 - eq * 0.06
        if avail_cash < 20_000.0:
            continue

        orders: list[Order] = []
        if is_rebal and sc_df is not None and not sc_df.is_empty():
            buy_pool = sc_df.filter(
                (pl.col("close") > pl.col("sma200"))
                & (pl.col("sma50") > pl.col("sma200"))
                & (pl.col("close") > pl.col("sma50"))
                & (pl.col("mom_12_1") > w.min_mom_12_1)
                & (pl.col("ret_126d") > w.min_ret_126d)
            )
            rows = buy_pool.head(45).to_dicts()
            if rows:
                scored_rows: list[tuple[float, dict[str, Any]]] = []
                for r in rows:
                    sym = str(r["symbol"])
                    si = ds.sym_to_idx.get(sym, -1)
                    base_c = float(r["composite"])
                    if si >= 0:
                        rm = float(ds.res_mom_63_m[ti, si]) if not math.isnan(ds.res_mom_63_m[ti, si]) else 0.0
                        er = float(ds.er_63_m[ti, si]) if not math.isnan(ds.er_63_m[ti, si]) else 0.3
                        be = float(ds.sym_barrier_edge_m[ti, si]) if not math.isnan(ds.sym_barrier_edge_m[ti, si]) else 0.0
                        uv = float(ds.up_dn_vol_20_m[ti, si]) if not math.isnan(ds.up_dn_vol_20_m[ti, si]) else 1.0
                        mv_bonus = (
                            0.25 * max(-1.5, min(2.5, rm))
                            + 0.80 * (er - 0.30)
                            + 2.5 * be
                            + 0.20 * max(-1.0, min(1.5, uv - 1.0))
                        )
                        scored_rows.append((base_c + mv_bonus, r))
                    else:
                        scored_rows.append((base_c, r))
                scored_rows.sort(key=lambda x: -x[0])
                rows = [x[1] for x in scored_rows]

            target_slot_notional = max(35_000.0, (eq * 0.93) / target_slots_div)
            for row in rows:
                if len(surviving_syms) >= max_slots or avail_cash < min_v1_notional:
                    break
                sym = str(row["symbol"])
                if sym in surviving_syms:
                    continue
                si = ds.sym_to_idx.get(sym, -1)
                ind = str(row.get("industry") or "Unclassified")
                if ind_counts.get(ind, 0) >= max_per_ind:
                    rejection_counts["TIER3_SECTOR_NAMES"] = rejection_counts.get("TIER3_SECTOR_NAMES", 0) + 1
                    continue
                s_cap_lim = 0.30 if (use_tiered_risk and ind == DEFAULT.finserv_industry) else 0.25
                if sec_cap.get(ind, 0.0) / eq >= s_cap_lim:
                    rejection_counts["TIER3_SECTOR_CAP"] = rejection_counts.get("TIER3_SECTOR_CAP", 0) + 1
                    continue
                rem_sec = max_sec_risk_pct * eq - sec_risk.get(ind, 0.0)
                if rem_sec < eq * 0.002:
                    rejection_counts["TIER3_SECTOR_RISK"] = rejection_counts.get("TIER3_SECTOR_RISK", 0) + 1
                    continue
                if si >= 0 and any(_pit_pair_corr_60d(ds, ti, si, osi, 60) >= 0.65 for osi in open_sis):
                    rejection_counts["TIER4_CORR_REJECT"] = rejection_counts.get("TIER4_CORR_REJECT", 0) + 1
                    continue
                rem_heat = max_port_heat_entry * eq - cur_open_risk_rs
                if rem_heat < eq * 0.003:
                    rejection_counts["TIER1_HEAT_FULL"] = rejection_counts.get("TIER1_HEAT_FULL", 0) + 1
                    break
                price = float(row["close"])
                natr = float(ds.natr14_m[ti, si]) if (si >= 0 and not math.isnan(ds.natr14_m[ti, si])) else 0.025
                stp_i = min(0.120, max(0.062, 2.8 * natr))
                max_risk_lot = min(max_v1_risk_pct * eq, rem_heat, rem_sec)
                alloc = min(
                    target_slot_notional,
                    max_risk_lot / stp_i,
                    avail_cash / 1.02,
                    float(row["adtv_20"]) * DEFAULT.max_pct_of_adtv,
                )
                qty = int(alloc // price)
                notional = qty * price
                if notional < min_v1_notional:
                    rejection_counts["MIN_NOTIONAL_OR_CASH"] = rejection_counts.get("MIN_NOTIONAL_OR_CASH", 0) + 1
                    continue
                seq += 1
                oid = f"p_{seq:06d}"
                orders.append(Order(
                    order_id=oid,
                    symbol=sym,
                    industry=ind,
                    placed_d=d,
                    entry_lo=round(price * (1.0 - w.entry_band_pct), 2),
                    entry_hi=round(price * (1.0 + w.entry_band_pct), 2),
                    stop=round(price * (1.0 - stp_i), 2),
                    target=round(price * w.target_multiple, 2),
                    qty=qty,
                    horizon=Horizon.POSITIONAL,
                    sleeve=Sleeve.MOMENTUM_BREAKOUT,
                    arm=Arm.DETERMINISTIC,
                    reasons=("MV_ENSEMBLE",),
                    max_sessions=252,
                ))
                lot_var[oid] = "V1"
                surviving_syms.add(sym)
                if si >= 0:
                    open_sis.append(si)
                ind_counts[ind] = ind_counts.get(ind, 0) + 1
                sec_cap[ind] = sec_cap.get(ind, 0.0) + notional
                cur_open_risk_rs += notional * stp_i
                sec_risk[ind] = sec_risk.get(ind, 0.0) + notional * stp_i
                avail_cash -= notional * 1.02

        if len(surviving_syms) < max_slots and avail_cash >= min_tac_notional:
            for vk, vname in (
                ("V3", "V3_OU_Residual_Reversion"),
                ("V5", "V5_Defensive_CrisisShield"),
                ("V4", "V4_VolCompression_Barrier"),
                ("V2", "V2_Residual_Alpha_HAC"),
            ):
                tcands = generate_variant_candidates(ds, d, vname, exclude_v1_top15=False)
                for c in tcands[:4]:
                    if len(surviving_syms) >= max_slots or avail_cash < min_tac_notional:
                        break
                    if c.symbol in surviving_syms:
                        continue
                    ind = c.industry
                    if ind_counts.get(ind, 0) >= max_per_ind:
                        rejection_counts["TIER3_SECTOR_NAMES"] = rejection_counts.get("TIER3_SECTOR_NAMES", 0) + 1
                        continue
                    s_cap_lim = 0.30 if (use_tiered_risk and ind == DEFAULT.finserv_industry) else 0.25
                    if sec_cap.get(ind, 0.0) / eq >= s_cap_lim:
                        rejection_counts["TIER3_SECTOR_CAP"] = rejection_counts.get("TIER3_SECTOR_CAP", 0) + 1
                        continue
                    rem_sec = max_sec_risk_pct * eq - sec_risk.get(ind, 0.0)
                    if rem_sec < eq * 0.002:
                        rejection_counts["TIER3_SECTOR_RISK"] = rejection_counts.get("TIER3_SECTOR_RISK", 0) + 1
                        continue
                    if any(_pit_pair_corr_60d(ds, ti, c.sym_idx, osi, 60) >= 0.65 for osi in open_sis):
                        rejection_counts["TIER4_CORR_REJECT"] = rejection_counts.get("TIER4_CORR_REJECT", 0) + 1
                        continue
                    rem_heat = max_port_heat_entry * eq - cur_open_risk_rs
                    if rem_heat < eq * 0.003:
                        rejection_counts["TIER1_HEAT_FULL"] = rejection_counts.get("TIER1_HEAT_FULL", 0) + 1
                        break
                    stop_p = 0.060 if vk in ("V3", "V5") else 0.075
                    alloc = min(
                        eq * 0.095,
                        min(max_tac_risk_pct * eq, rem_heat, rem_sec) / stop_p,
                        avail_cash / 1.02,
                        c.adtv20 * DEFAULT.max_pct_of_adtv,
                    )
                    qty = int(alloc // c.close)
                    notional = qty * c.close
                    if notional < min_tac_notional:
                        rejection_counts["MIN_NOTIONAL_OR_CASH"] = rejection_counts.get("MIN_NOTIONAL_OR_CASH", 0) + 1
                        continue
                    seq += 1
                    oid = f"t_{seq:06d}"
                    orders.append(Order(
                        order_id=oid,
                        symbol=c.symbol,
                        industry=ind,
                        placed_d=d,
                        entry_lo=round(c.close * 0.97, 2),
                        entry_hi=round(c.close * 1.03, 2),
                        stop=round(c.close * (1.0 - stop_p), 2),
                        target=round(c.close * (1.35 if vk in ("V3", "V5") else 2.2), 2),
                        qty=qty,
                        horizon=c.horizon,
                        sleeve=c.sleeve,
                        arm=Arm.DETERMINISTIC,
                        reasons=c.reasons,
                        max_sessions=(20 if vk in ("V3", "V5") else v24_max_sess),
                    ))
                    lot_var[oid] = vk if vk in ("V3", "V5") else "V24"
                    surviving_syms.add(c.symbol)
                    open_sis.append(c.sym_idx)
                    ind_counts[ind] = ind_counts.get(ind, 0) + 1
                    sec_cap[ind] = sec_cap.get(ind, 0.0) + notional
                    cur_open_risk_rs += notional * stop_p
                    sec_risk[ind] = sec_risk.get(ind, 0.0) + notional * stop_p
                    avail_cash -= notional * 1.02

        if orders:
            broker.place_orders(orders, run_id=f"sim_ens_{'4tier' if use_tiered_risk else 'legacy'}", ts=ts0)

    return {
        "equity_curve": broker.equity_curve,
        "outcomes": broker.outcomes,
        "max_observed_open_risk_pct": round(max_observed_open_risk_pct * 100.0, 3),
        "max_observed_beta": round(max_observed_beta, 3),
        "max_observed_positions": max_observed_positions,
        "rejection_counts": rejection_counts,
    }


def simulate_variant_book(
    ds: ResearchDataStore,
    sessions: list[date],
    *,
    mode: str,
    variant: str | None = None,
    regime_weights_by_session: dict[date, dict[str, dict[str, float]]] | None = None,
    use_tiered_risk: bool = True,
    initial_equity: float = 500_000.0,
    eval_start_d: date | None = None,
) -> dict[str, Any]:
    """Run exact day-by-day `PaperBroker` simulation on `sessions` (100% causal, `d+1` fills).

    `mode`: 'single' (single variant using DEFAULT/Tiered risk) or 'ensemble' (MetaVoter across V1..V5).
    """
    if mode == "ensemble":
        return _simulate_ensemble_book(
            ds,
            sessions,
            regime_weights_by_session=regime_weights_by_session,
            use_tiered_risk=use_tiered_risk,
            initial_equity=initial_equity,
            eval_start_d=eval_start_d,
        )
    broker = PaperBroker(initial_equity=initial_equity, arm=Arm.DETERMINISTIC)
    ts0 = datetime(2021, 1, 1, tzinfo=timezone.utc)
    max_observed_open_risk_pct = 0.0
    max_observed_beta = 0.0
    max_observed_positions = 0
    lot_meta: dict[str, dict[str, Any]] = {}  # order_id -> variant, initial_atr, initial_r, max_sessions
    rejection_counts: dict[str, int] = {}
    order_seq = 0

    for d in sessions:
        ti = ds.date_to_idx[d]
        bmap = ds.bars_by_date.get(d, {})
        # 1. Execute scheduled morning fills, intraday stops/targets, and MTM close
        broker.step_session(d, bmap, run_id=f"sim_{mode}_{variant or 'ens'}", ts=ts0)

        macro = ds.macro_map.get(d, {"macro_regime": "SIDEWAYS_CHOP", "macro_recovery": False})
        reg = macro["macro_regime"]
        macro_rec = bool(macro.get("macro_recovery", False))

        # 2. Check and enforce live open risk + update dynamic trailing stops / OU equilibrium exits
        pm = _portfolio_risk_metrics(broker, ds, ti)
        eq = pm["equity"]

        if use_tiered_risk:
            env = dict(_regime_risk_envelope(reg))
            if mode == "single":
                env["max_new"] = 6
        elif mode == "single" and variant == "V1_Baseline_Sortino":
            env = {"heat": 0.060, "gross": 0.92, "slots": 7, "max_new": 7}
        else:
            env = dict(_legacy_risk_envelope(reg))
        hard_heat_cap = min(TIERED_SPEC.max_open_risk_pct, env["heat"]) if use_tiered_risk else env["heat"]

        # Active profit-lock trail & variant-specific dynamic exits (fills tomorrow open or tomorrow intraday)
        for oid, lot in list(broker.open_lots.items()):
            bar = bmap.get(lot.symbol)
            if bar is None:
                continue
            lp = float(bar.close)
            meta = lot_meta.get(oid, {})
            var_name = meta.get("variant", "V1_Baseline_Sortino")
            atr = float(meta.get("atr", lp * 0.02))
            si = ds.sym_to_idx.get(lot.symbol, -1)
            natr = (float(ds.natr14_m[ti, si]) if si >= 0 and not math.isnan(ds.natr14_m[ti, si]) else atr / max(lp, 1.0))
            cur_r = (lp - lot.entry_price) / max(lot.entry_price - lot.initial_stop, 1e-6)
            s50_ok = float(ds.sma50_above_m[ti, si]) > 0.5 if si >= 0 else True
            s200_ok = float(ds.sma200_above_m[ti, si]) > 0.5 if si >= 0 else True

            # Stage-2 trend variants use wide peak trail (+8% activate, 14%/12% below peak) to capture right tails;
            # short-horizon swing variants (V3, V5) lock profits tighter (+5% activate, <= 7.2% trail).
            cand_stop = lot.stop
            if var_name in ("V1_Baseline_Sortino", "V2_Residual_Alpha_HAC", "V4_VolCompression_Barrier"):
                if lot.max_high_seen >= lot.entry_price * 1.08:
                    trail_dd = 0.12 if lot.max_high_seen >= lot.entry_price * 1.25 else 0.14
                    cand_stop = max(
                        cand_stop,
                        round(lot.entry_price * 1.002, 2),
                        round(lot.max_high_seen * (1.0 - trail_dd), 2),
                    )
            elif lot.max_high_seen >= lot.entry_price * 1.05:
                trail_pct = min(0.072, max(0.032, 2.3 * natr))
                cand_stop = max(
                    cand_stop,
                    round(lot.entry_price * 1.002, 2),
                    round(lot.max_high_seen * (1.0 - trail_pct), 2),
                )

            if var_name == "V3_OU_Residual_Reversion" and si >= 0 and lot.sessions_held >= 4:
                if float(ds.ou_s_m[ti, si]) >= 0.45 and oid not in broker.pending_exits:
                    broker.schedule_exit(oid, reason="V3_OU_EQUILIBRIUM_TARGET")

            # Regime-shock protection & SMA200 breakdown discipline
            if mode == "single" and variant in (
                "V2_Residual_Alpha_HAC",
                "V3_OU_Residual_Reversion",
                "V4_VolCompression_Barrier",
                "V5_Defensive_CrisisShield",
            ):
                if reg == "BEAR_HIGH_VOL_SHOCK" and (variant != "V5_Defensive_CrisisShield" or not macro_rec):
                    if oid not in broker.pending_exits:
                        broker.schedule_exit(oid, reason="REGIME_SHOCK_EXIT")
                elif variant != "V3_OU_Residual_Reversion" and not s200_ok and (ti % 5 == 0) and oid not in broker.pending_exits:
                    broker.schedule_exit(oid, reason="SMA200_BREAK")
            elif mode == "ensemble":
                if reg == "BEAR_HIGH_VOL_SHOCK" and not macro_rec:
                    # Preserve capital rapidly during unconfirmed Bear Shock; keep only locked >= +1.5R above SMA50
                    if var_name != "V5_Defensive_CrisisShield":
                        keep_winner = (cur_r >= 1.5) and s50_ok and (lot.stop >= lot.entry_price * 1.03)
                        if not keep_winner and oid not in broker.pending_exits:
                            broker.schedule_exit(oid, reason="REGIME_SHOCK_EXIT")
                        else:
                            cand_stop = max(cand_stop, round(lp * 0.96, 2))
                    elif (not s50_ok or cur_r < 0.3) and oid not in broker.pending_exits:
                        broker.schedule_exit(oid, reason="REGIME_SHOCK_EXIT")
                elif var_name in ("V1_Baseline_Sortino", "V2_Residual_Alpha_HAC", "V4_VolCompression_Barrier"):
                    if (not s200_ok) and (ti % 5 == 0) and oid not in broker.pending_exits:
                        broker.schedule_exit(oid, reason="SMA200_BREAK")

            cand_stop_r = round(cand_stop, 2)
            if oid not in broker.pending_exits and cand_stop_r > lot.stop + 1e-6 and cand_stop_r < lp * 0.995:
                broker.trail_stop(oid, cand_stop_r, run_id="sim_trail", ts=ts0)

        # Tier-1 Hard Ceiling Guard: if open risk slightly drifted above hard ceiling due to equity dip or gap,
        # tighten stops proportionally on widest positions so post-close live open risk stays strictly <= ceiling
        if use_tiered_risk:
            pm = _portfolio_risk_metrics(broker, ds, ti)
            if pm["open_risk_pct"] > TIERED_SPEC.max_open_risk_pct and broker.open_lots:
                ratio = (TIERED_SPEC.max_open_risk_pct * 0.985) / pm["open_risk_pct"]
                for oid, lot in list(broker.open_lots.items()):
                    bar = bmap.get(lot.symbol)
                    if bar is None:
                        continue
                    lp = float(bar.close)
                    ref_p = min(lot.entry_price, lp)
                    cur_dist = max(0.0, ref_p - lot.stop)
                    new_stop = round(ref_p - cur_dist * ratio, 2)
                    if new_stop > lot.stop + 1e-6 and new_stop < lp * 0.996:
                        broker.trail_stop(oid, new_stop, run_id="sim_cap_ratchet", ts=ts0)

        pm = _portfolio_risk_metrics(broker, ds, ti)
        if eval_start_d is None or d >= eval_start_d:
            max_observed_open_risk_pct = max(max_observed_open_risk_pct, pm["open_risk_pct"])
            max_observed_beta = max(max_observed_beta, pm["portfolio_beta"])
            max_observed_positions = max(max_observed_positions, len(broker.open_lots))

        # 3. Cancel stale unfilled limit orders before queuing fresh tonight orders
        for old_oid in list(broker.pending_orders.keys()):
            del broker.pending_orders[old_oid]

        # 4. Generate candidate signals for tomorrow (d+1)
        if mode == "single":
            assert variant is not None
            # For single baseline V1 matching production monthly rank-buffer rotation (top 25)
            if variant == "V1_Baseline_Sortino" and d in ds.monthly_rebal_set:
                top_syms = {c.symbol for c in generate_variant_candidates(ds, d, variant)[:25]}
                for oid, lot in list(broker.open_lots.items()):
                    if oid not in broker.pending_exits and lot.symbol not in top_syms:
                        broker.schedule_exit(oid, reason="MONTHLY_ROTATE_OUT")
            ranked_cands = generate_variant_candidates(ds, d, variant, exclude_v1_top15=True)
        else:
            # Regime-adaptive Multi-Variant Meta-Voter:
            # primary allocation monthly (plus fill-up on ti % 10 == 0 if < 9 slots outside crash);
            # tactical V3 OU / V5 Shield checks every 5 sessions when spare capacity exists.
            n_cur_open = len(broker.open_lots) - len(broker.pending_exits)
            is_monthly = d in ds.monthly_rebal_set
            is_primary_rebal = is_monthly or (
                (ti % 10 == 0) and (n_cur_open < 9) and (reg != "BEAR_HIGH_VOL_SHOCK")
            )
            is_swing_check = (ti % 5 == 0) or (reg == "BEAR_HIGH_VOL_SHOCK" and macro_rec)
            if not (is_primary_rebal or is_swing_check):
                ranked_cands = []
            else:
                w_map = (
                    regime_weights_by_session[d][reg]
                    if (regime_weights_by_session and d in regime_weights_by_session and reg in regime_weights_by_session[d])
                    else STRUCTURAL_REGIME_PRIORS[reg]
                )
                active_variants = (
                    VARIANTS_LOGGED
                    if is_primary_rebal
                    else ("V3_OU_Residual_Reversion", "V5_Defensive_CrisisShield")
                )
                by_sym: dict[str, tuple[SignalCandidate, float, list[str]]] = {}
                for vname in active_variants:
                    wv = float(w_map.get(vname, 0.0))
                    if wv <= 0.02:
                        continue
                    vcands = generate_variant_candidates(
                        ds,
                        d,
                        vname,
                        allow_v1_weekly=(vname == "V1_Baseline_Sortino"),
                        allow_ens_rebal=is_primary_rebal,
                        exclude_v1_top15=False,
                    )
                    if not vcands:
                        continue
                    n_vc = len(vcands)
                    for rank_i, cand in enumerate(vcands):
                        norm_rank = (n_vc - rank_i) / max(1, n_vc)
                        contrib = wv * (0.50 + 0.50 * norm_rank)
                        if cand.symbol not in by_sym:
                            by_sym[cand.symbol] = (cand, contrib, [vname])
                        else:
                            best_c, old_sum, vlist = by_sym[cand.symbol]
                            bonus = 1.20 if vname not in vlist else 1.0
                            chosen = cand if cand.stop_dist >= best_c.stop_dist else best_c
                            by_sym[cand.symbol] = (chosen, (old_sum + contrib) * bonus, vlist + [vname])

                ranked_cands = []
                for _sym, (best_c, meta_sc, vlist) in by_sym.items():
                    ranked_cands.append(SignalCandidate(
                        variant=best_c.variant,
                        symbol=best_c.symbol,
                        sym_idx=best_c.sym_idx,
                        industry=best_c.industry,
                        score=meta_sc,
                        close=best_c.close,
                        atr14=best_c.atr14,
                        stop_dist=best_c.stop_dist,
                        target_dist=best_c.target_dist,
                        max_sessions=best_c.max_sessions,
                        beta63=best_c.beta63,
                        adtv20=best_c.adtv20,
                        horizon=best_c.horizon,
                        sleeve=best_c.sleeve,
                        reasons=tuple(vlist),
                    ))
                ranked_cands.sort(key=lambda c: -c.score)

                # Monthly rank-buffer rotation (hold top <= 22; exit outside top 22 unless locked winner)
                if is_monthly and ranked_cands and reg != "BEAR_HIGH_VOL_SHOCK":
                    top_ens = {c.symbol for c in ranked_cands[:22]}
                    for oid, lot in list(broker.open_lots.items()):
                        if oid in broker.pending_exits or lot.symbol in top_ens:
                            continue
                        meta = lot_meta.get(oid, {})
                        var_name = meta.get("variant", "V1_Baseline_Sortino")
                        if var_name in ("V3_OU_Residual_Reversion", "V5_Defensive_CrisisShield"):
                            continue
                        broker.schedule_exit(oid, reason="ENSEMBLE_ROTATE_OUT")

        # 5. Risk Gate Allocation & Order Sizing (net of scheduled pending_exits filling at d+1 open)
        exiting_oids = set(broker.pending_exits.keys())
        exiting_notional = 0.0
        surviving_syms: set[str] = set()
        open_sis: list[int] = []
        cur_open_risk_rs = 0.0
        cur_gross_rs = 0.0
        cur_beta = 0.0
        sec_risk: dict[str, float] = {}
        sec_cap: dict[str, float] = {}
        sec_names: dict[str, int] = {}
        for oid, lot in broker.open_lots.items():
            bar = bmap.get(lot.symbol)
            cp = float(bar.close) if bar is not None else lot.last_price
            mv = cp * lot.qty
            if oid in exiting_oids:
                exiting_notional += mv
                continue
            surviving_syms.add(lot.symbol)
            si = ds.sym_to_idx.get(lot.symbol, -1)
            if si >= 0:
                open_sis.append(si)
            rk = _lot_open_risk_rupees(lot, cp)
            cur_open_risk_rs += rk
            cur_gross_rs += mv
            b = float(ds.beta63_m[ti, si]) if (si >= 0 and not math.isnan(ds.beta63_m[ti, si])) else 1.0
            cur_beta += (mv / max(eq, 1.0)) * b
            sec_risk[lot.industry] = sec_risk.get(lot.industry, 0.0) + rk
            sec_cap[lot.industry] = sec_cap.get(lot.industry, 0.0) + mv
            sec_names[lot.industry] = sec_names.get(lot.industry, 0) + 1

        open_syms = surviving_syms
        cur_slots = len(surviving_syms)
        max_slots = int(env["slots"])
        max_new = int(env["max_new"])
        placed_today = 0
        avail_cash = max(0.0, broker.cash + exiting_notional * 0.85 - (1.0 - env["gross"]) * eq)

        new_orders: list[Order] = []
        for cand in ranked_cands:
            if cur_slots >= max_slots or placed_today >= max_new:
                break
            if avail_cash < TIERED_SPEC.min_notional or (hard_heat_cap * eq - cur_open_risk_rs) < eq * 0.0025:
                break
            if cand.symbol in open_syms:
                continue

            if use_tiered_risk:
                # Tier 3: Sector cluster caps (<= 3.0% open risk, <= 3 names, <= 25% sector capital)
                ind = cand.industry
                s_cap_lim = 0.30 if ind == DEFAULT.finserv_industry else TIERED_SPEC.max_sector_capital_pct
                if sec_names.get(ind, 0) >= TIERED_SPEC.max_sector_names:
                    rejection_counts["TIER3_SECTOR_NAMES"] = rejection_counts.get("TIER3_SECTOR_NAMES", 0) + 1
                    continue
                if sec_cap.get(ind, 0.0) / eq >= s_cap_lim:
                    rejection_counts["TIER3_SECTOR_CAP"] = rejection_counts.get("TIER3_SECTOR_CAP", 0) + 1
                    continue
                if sec_risk.get(ind, 0.0) / eq >= TIERED_SPEC.max_sector_open_risk_pct:
                    rejection_counts["TIER3_SECTOR_RISK"] = rejection_counts.get("TIER3_SECTOR_RISK", 0) + 1
                    continue

                # Tier 4: Dynamic 60d correlation & beta budget
                max_rho = 0.0
                for osi in open_sis:
                    rho = _pit_pair_corr_60d(ds, ti, cand.sym_idx, osi, lookback=TIERED_SPEC.corr_lookback)
                    if rho > max_rho:
                        max_rho = rho
                if max_rho > TIERED_SPEC.corr_reject_threshold:
                    rejection_counts["TIER4_CORR_REJECT"] = rejection_counts.get("TIER4_CORR_REJECT", 0) + 1
                    continue
                corr_mult = 0.50 if max_rho > TIERED_SPEC.corr_halve_threshold else 1.0

                # Tier 2: Inverse-volatility per-trade risk (<= 1.0%) scaled smoothly by conviction & corr
                conv_mult = 1.35 if (len(cand.reasons) >= 2 or cand.score >= 0.45) else 1.0
                target_risk_pct = min(TIERED_SPEC.max_trade_risk_pct, TIERED_SPEC.base_trade_risk_pct * conv_mult * corr_mult)

                # Clamp by remaining Tier 1 Portfolio ceiling & Tier 3 Sector open risk ceiling
                rem_port_risk_rs = hard_heat_cap * eq - cur_open_risk_rs
                rem_sec_risk_rs = TIERED_SPEC.max_sector_open_risk_pct * eq - sec_risk.get(ind, 0.0)
                trade_risk_rs = min(target_risk_pct * eq, rem_port_risk_rs, rem_sec_risk_rs)
                if trade_risk_rs < eq * 0.0020:
                    rejection_counts["TIER1_HEAT_FULL"] = rejection_counts.get("TIER1_HEAT_FULL", 0) + 1
                    continue

                entry_hi = round(cand.close * 1.025, 2)
                entry_lo = round(cand.close * 0.965, 2)
                stop_p = round(cand.close - cand.stop_dist, 2)
                target_p = round(cand.close + cand.target_dist, 2)
                # Size against planned stop distance (post-fill open risk is still strictly bounded <= 7.0%)
                conservative_risk_per_sh = max(cand.stop_dist, cand.close * 0.020)
                max_notional_rs = min(
                    TIERED_SPEC.max_notional_pct * eq,
                    (s_cap_lim * eq) - sec_cap.get(ind, 0.0),
                    (env["gross"] * eq) - cur_gross_rs,
                    avail_cash * 0.98,
                    DEFAULT.max_pct_of_adtv * cand.adtv20,
                )
                qty_risk = int(trade_risk_rs // conservative_risk_per_sh)
                qty_notional = int(max_notional_rs // entry_hi)
                qty = min(qty_risk, qty_notional)
                if qty <= 0 or qty * cand.close < TIERED_SPEC.min_notional:
                    rejection_counts["MIN_NOTIONAL_OR_CASH"] = rejection_counts.get("MIN_NOTIONAL_OR_CASH", 0) + 1
                    continue

                proj_beta_add = ((qty * entry_hi) / eq) * cand.beta63
                if cur_beta + proj_beta_add > env["max_beta"]:
                    # Scale qty down to fit portfolio beta budget if still above min_notional
                    rem_beta = env["max_beta"] - cur_beta
                    if rem_beta <= 0.02 or cand.beta63 <= 0.0:
                        rejection_counts["TIER4_BETA_CAP"] = rejection_counts.get("TIER4_BETA_CAP", 0) + 1
                        continue
                    qty_beta = int((rem_beta * eq / max(cand.beta63, 0.05)) // entry_hi)
                    qty = min(qty, qty_beta)
                    if qty <= 0 or qty * cand.close < TIERED_SPEC.min_notional:
                        rejection_counts["TIER4_BETA_CAP"] = rejection_counts.get("TIER4_BETA_CAP", 0) + 1
                        continue
                cur_beta += ((qty * entry_hi) / eq) * cand.beta63
            else:
                # Legacy rigid 7-stock / 1%-stock / 1%-industry rule
                ind = cand.industry
                if sec_names.get(ind, 0) >= 2:
                    continue
                if sec_risk.get(ind, 0.0) / eq >= 0.010:
                    continue
                rem_port = env["heat"] * eq - cur_open_risk_rs
                trade_risk_rs = min(0.010 * eq, rem_port)
                if trade_risk_rs < eq * 0.002:
                    continue
                entry_hi = round(cand.close * 1.035, 2)
                entry_lo = round(cand.close * 0.960, 2)
                stop_p = round(cand.close - cand.stop_dist, 2)
                target_p = round(cand.close + cand.target_dist, 2)
                cons_risk = max(entry_hi - stop_p, cand.stop_dist)
                max_notional_rs = min(
                    0.20 * eq,
                    (env["gross"] * eq) - cur_gross_rs,
                    avail_cash * 0.98,
                    DEFAULT.max_pct_of_adtv * cand.adtv20,
                )
                qty = min(int(trade_risk_rs // cons_risk), int(max_notional_rs // entry_hi))
                if qty <= 0 or qty * cand.close < 50_000.0 * (eq / initial_equity):
                    continue

            order_seq += 1
            oid = f"sim_{order_seq:06d}"
            order = Order(
                order_id=oid,
                symbol=cand.symbol,
                industry=cand.industry,
                placed_d=d,
                entry_lo=entry_lo,
                entry_hi=entry_hi,
                stop=stop_p,
                target=target_p,
                qty=qty,
                horizon=cand.horizon,
                sleeve=cand.sleeve,
                arm=Arm.DETERMINISTIC,
                reasons=cand.reasons,
                max_sessions=cand.max_sessions,
            )
            new_orders.append(order)
            lot_meta[oid] = {"variant": cand.variant, "atr": cand.atr14, "max_sessions": cand.max_sessions}
            open_syms.add(cand.symbol)
            open_sis.append(cand.sym_idx)
            cur_slots += 1
            placed_today += 1
            risk_rs = cand.stop_dist * qty
            notional_rs = cand.close * qty
            cur_open_risk_rs += risk_rs
            cur_gross_rs += notional_rs
            avail_cash = max(0.0, avail_cash - notional_rs)
            sec_risk[ind] = sec_risk.get(ind, 0.0) + risk_rs
            sec_cap[ind] = sec_cap.get(ind, 0.0) + notional_rs
            sec_names[ind] = sec_names.get(ind, 0) + 1

        if new_orders:
            broker.place_orders(new_orders, run_id=f"sim_{mode}", ts=ts0)

    return {
        "equity_curve": broker.equity_curve,
        "outcomes": broker.outcomes,
        "max_observed_open_risk_pct": round(max_observed_open_risk_pct * 100.0, 3),
        "max_observed_beta": round(max_observed_beta, 3),
        "max_observed_positions": max_observed_positions,
        "rejection_counts": rejection_counts,
    }
