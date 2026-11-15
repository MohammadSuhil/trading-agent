"""Phase 1: Stack Introspection & Diagnostic Autopsy of Baseline Strategy (`ta_quant` v2).

Strictly sandboxed under `/sim_sandbox/` — edits ZERO production files.
Outputs detailed console report and `/sim_sandbox/baseline_diagnostics.json`:
  1. Complete inventory of pure NSE data sources, feature pipelines, ML stubs, & risk limits.
  2. Mathematical audit of PSR / DSR (`fisher=False`, unannualized daily SR, `sr_0`).
  3. Multi-dimensional macro-regime classification (`ADX-14`, `Hurst-100`, `India VIX`,
     `252d ATR% percentile`, `50/200-DMA`, market breadth/dispersion) and bleed attribution
     across `[1250, 760, 250, 120]` windows.
  4. Counterfactual expectancy replay of valid Stage-2 signals rejected by the legacy
     `max 7 stocks` and `max 1% industry risk` caps.
"""
from __future__ import annotations

import json
import math
import time as pytime
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np
import polars as pl
from ta_core.asof import AsOf
from ta_core.enums import Arm
from ta_datafeed.store import GuardedClient
from ta_ledger.broker.base import BarData, Order
from ta_ledger.broker.paper import PaperBroker, apply_costs
from ta_ledger.risk import DEFAULT
from ta_quant.challenger import (
    _build_v2_panel,
    monthly_rebalance_dates,
    run_backtest_v2,
    score_universe_v2,
)
from ta_quant.factors.correlation import ResidualCorrelationIndex
from ta_quant.factors.price import FACTOR_COLUMNS
from ta_quant.nightly import latest_session, load_panel_inputs
from ta_quant.stats import (
    compute_backtest_stats,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
)
from ta_quant.weights import load_challenger_weights

OUT_PATH = Path(__file__).resolve().parent / "baseline_diagnostics.json"
WINDOWS = [1250, 760, 250, 120]
EULER_GAMMA = 0.5772156649015329
_STD_NORM = NormalDist(0.0, 1.0)


# ---------------------------------------------------------------------------
# 1. Stack & Data Capability Inventory
# ---------------------------------------------------------------------------
def inventory_capabilities(ch: GuardedClient) -> dict[str, Any]:
    sources = ch.query_admin(
        "SELECT source, min(d) AS min_d, max(d) AS max_d, uniqExact(d) AS n_days, count() AS n_rows "
        "FROM md.bars_1d GROUP BY source ORDER BY min_d"
    ).to_dicts()
    for row in sources:
        row["min_d"] = str(row["min_d"])
        row["max_d"] = str(row["max_d"])

    n_indices = int(ch.query_admin("SELECT uniqExact(index_name) AS n FROM md.index_1d")["n"][0])
    key_indices = ch.query_admin(
        "SELECT index_name, min(d) AS min_d, max(d) AS max_d, uniqExact(d) AS n_days "
        "FROM md.index_1d WHERE index_name IN ('Nifty 500', 'Nifty 50', 'India VIX', 'Nifty 1D Rate Index', 'Nifty 10 yr Benchmark G-Sec') "
        "GROUP BY index_name ORDER BY index_name"
    ).to_dicts()
    for row in key_indices:
        row["min_d"] = str(row["min_d"])
        row["max_d"] = str(row["max_d"])

    return {
        "bars_1d_sources": sources,
        "pure_nse_only": all(str(r["source"]).startswith("nse") for r in sources),
        "total_official_indices": n_indices,
        "key_benchmark_indices": key_indices,
        "price_factor_count": len(FACTOR_COLUMNS),
        "price_factors_sample": list(FACTOR_COLUMNS),
        "trend_features": ["trend_20", "trend_63", "trend_252", "t_drift_20", "t_drift_63", "t_drift_252", "er_20", "er_63", "er_252", "adx_delta_10", "flips_63d"],
        "risk_correlation": "ResidualCorrelationIndex (63d cross-sectionally demeaned residual Pearson rho)",
        "barrier_model": "BarrierModel (252d empirical triple-barrier Dirichlet posterior conditioned on ATR% tercile, pessimistic same-bar DOWN)",
        "ml_module_status": "ta_quant/ml/__init__.py empty stub (ml_prob=None; ready for Dirichlet meta-labeling & regime probability weighting)",
        "legacy_risk_defaults": {
            "max_positions": DEFAULT.max_positions,
            "max_positions_backstop": DEFAULT.max_positions_backstop,
            "risk_per_trade_pct": DEFAULT.risk_per_trade_pct,
            "risk_per_trade_ceiling_pct": DEFAULT.risk_per_trade_ceiling_pct,
            "max_portfolio_heat_pct": DEFAULT.max_portfolio_heat_pct,
            "max_industry_names": DEFAULT.max_industry_names,
            "max_industry_pct": DEFAULT.max_industry_pct,
            "max_pair_residual_corr": DEFAULT.max_pair_residual_corr,
        },
    }


# ---------------------------------------------------------------------------
# 2. PSR / DSR Audit (Independent Reference vs ta_quant.stats)
# ---------------------------------------------------------------------------
def ref_psr(sr_obs: float, sr_benchmark: float, n_obs: int, gamma_3: float, gamma_4: float) -> float:
    if n_obs <= 2:
        return 0.0
    denom_sq = 1.0 - gamma_3 * sr_obs + ((gamma_4 - 1.0) / 4.0) * (sr_obs ** 2)
    if denom_sq <= 0.0:
        return 0.0
    z = ((sr_obs - sr_benchmark) * math.sqrt(n_obs - 1)) / math.sqrt(denom_sq)
    return float(_STD_NORM.cdf(z))


def ref_sr0(n_trials: int, var_trials: float) -> float:
    if n_trials <= 1 or var_trials <= 0.0:
        return 0.0
    return float(
        math.sqrt(var_trials)
        * (
            (1.0 - EULER_GAMMA) * _STD_NORM.inv_cdf(1.0 - 1.0 / n_trials)
            + EULER_GAMMA * _STD_NORM.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
        )
    )


def audit_psr_dsr_formulas() -> dict[str, Any]:
    rng = np.random.default_rng(42)
    rets = rng.normal(0.0008, 0.011, size=1250)
    mean_r = float(np.mean(rets))
    std_bessel = float(np.std(rets, ddof=1))
    sr_obs = mean_r / std_bessel
    g3 = float(np.mean(((rets - mean_r) / std_bessel) ** 3))
    g4 = float(np.mean(((rets - mean_r) / std_bessel) ** 4))

    psr_prod = probabilistic_sharpe_ratio(sr_obs, 0.0, len(rets), skew=g3, kurt=g4)
    psr_ref = ref_psr(sr_obs, 0.0, len(rets), g3, g4)

    std_sr = 0.5 / math.sqrt(252.0)
    sr0_prod = expected_max_sharpe(36, std_sr=std_sr)
    sr0_ref = ref_sr0(36, std_sr ** 2)

    dsr_prod = deflated_sharpe_ratio(sr_obs, len(rets), 36, skew=g3, kurt=g4, std_sr=std_sr)
    dsr_ref = ref_psr(sr_obs, sr0_ref, len(rets), g3, g4)

    return {
        "n_obs": len(rets),
        "sr_obs_daily": round(sr_obs, 6),
        "skew_gamma3": round(g3, 6),
        "kurt_pearson_fisher_false": round(g4, 6),
        "psr_max_abs_diff": abs(psr_prod - psr_ref),
        "sr0_n36_max_abs_diff": abs(sr0_prod - sr0_ref),
        "dsr_max_abs_diff": abs(dsr_prod - dsr_ref),
        "sr0_n36_daily": round(sr0_prod, 6),
        "sr0_n36_annualised": round(sr0_prod * math.sqrt(252.0), 4),
    }


# ---------------------------------------------------------------------------
# 3. Multi-Dimensional Macro Regime Classification (Nifty 500 + India VIX + Breadth)
# ---------------------------------------------------------------------------
def compute_hurst_rs(log_rets: np.ndarray) -> float:
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


def build_macro_regime_frame(
    idx_df: pl.DataFrame,
    vix_df: pl.DataFrame,
    panel_eq: pl.DataFrame,
) -> pl.DataFrame:
    df = idx_df.sort("d").unique(subset=["d"], keep="last").sort("d")
    vix_map = {r["d"]: float(r["close"]) for r in vix_df.select(["d", "close"]).iter_rows(named=True)}

    # Cross-sectional breadth (% > sma50) and 20d dispersion from equity panel
    breadth_df = (
        panel_eq.filter(pl.col("sma50").is_not_null() & pl.col("ret_21d").is_not_null())
        .group_by("d")
        .agg(
            (pl.col("close") > pl.col("sma50")).mean().alias("breadth_50"),
            pl.col("ret_21d").std().alias("cs_disp_21d"),
        )
    )
    breadth_map = {r["d"]: (float(r["breadth_50"] or 0.5), float(r["cs_disp_21d"] or 0.1)) for r in breadth_df.iter_rows(named=True)}

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
    regimes: list[str] = []
    breadth_list: list[float] = []
    disp_list: list[float] = []

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
            hurst100[i] = compute_hurst_rs(log_r[i - 99 : i + 1])
        if not np.isnan(atr_pct[i]):
            w = atr_pct[max(14, i - 251) : i + 1]
            w = w[~np.isnan(w)]
            if len(w) > 10:
                atr_vol_pct[i] = 100.0 * float(np.sum(w <= atr_pct[i])) / len(w)
        vw = vix_arr[max(0, i - 251) : i + 1]
        if len(vw) > 10:
            vix_pct[i] = 100.0 * float(np.sum(vw <= vix_arr[i])) / len(vw)

        b50, disp = breadth_map.get(dates[i], (0.5, 0.1))
        breadth_list.append(b50)
        disp_list.append(disp)

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
        regimes.append(reg)

    return pl.DataFrame({
        "d": dates,
        "close": cl.tolist(),
        "vix": vix_arr.tolist(),
        "adx14": np.nan_to_num(adx14, nan=20.0).tolist(),
        "hurst100": hurst100.tolist(),
        "atr_vol_pct": atr_vol_pct.tolist(),
        "vix_pct": vix_pct.tolist(),
        "idx_ret_20d": ret_20d.tolist(),
        "breadth_50": breadth_list,
        "cs_disp_21d": disp_list,
        "macro_regime": regimes,
    })


# ---------------------------------------------------------------------------
# 4. Regime Bleed Attribution & Legacy Risk Bottleneck Counterfactuals
# ---------------------------------------------------------------------------
def compute_regime_attribution(
    equity_curve: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    regime_map: dict[date, str],
) -> dict[str, Any]:
    dates = [r["d"] if isinstance(r["d"], date) else date.fromisoformat(str(r["d"])) for r in equity_curve]
    equities = [float(r["equity"]) for r in equity_curve]

    by_reg_rets: dict[str, list[float]] = defaultdict(list)
    for i in range(1, len(dates)):
        d = dates[i]
        r = (equities[i] - equities[i - 1]) / equities[i - 1] if equities[i - 1] > 0 else 0.0
        by_reg_rets[regime_map.get(d, "UNKNOWN")].append(r)

    by_reg_trades: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in outcomes:
        ed = t.get("entry_date") or t.get("entry_d")
        if ed is None:
            continue
        ed_d = ed if isinstance(ed, date) else date.fromisoformat(str(ed))
        by_reg_trades[regime_map.get(ed_d, "UNKNOWN")].append(t)

    out: dict[str, Any] = {}
    for reg in ("LOW_VOL_TREND", "HIGH_VOL_TREND", "SIDEWAYS_CHOP", "BEAR_HIGH_VOL_SHOCK"):
        rets = by_reg_rets.get(reg, [])
        n_sess = len(rets)
        cum_ret, peak, max_dd = 1.0, 1.0, 0.0
        for r in rets:
            cum_ret *= 1.0 + r
            peak = max(peak, cum_ret)
            max_dd = max(max_dd, (peak - cum_ret) / peak if peak > 0 else 0.0)

        if n_sess >= 2:
            arr = np.array(rets, dtype=np.float64)
            sd = float(np.std(arr, ddof=1))
            daily_sr = float(np.mean(arr) / sd) if sd > 1e-12 else 0.0
            ann_sr = daily_sr * math.sqrt(252.0)
        else:
            daily_sr, ann_sr = 0.0, 0.0

        trades = by_reg_trades.get(reg, [])
        n_tr = len(trades)
        wins = sum(1 for t in trades if float(t.get("pnl_rupees", 0.0)) > 0)
        gwin = sum(float(t.get("pnl_rupees", 0.0)) for t in trades if float(t.get("pnl_rupees", 0.0)) > 0)
        gloss = abs(sum(float(t.get("pnl_rupees", 0.0)) for t in trades if float(t.get("pnl_rupees", 0.0)) < 0))
        pf = (gwin / gloss) if gloss > 0 else (99.0 if gwin > 0 else 0.0)
        whipsaws = sum(1 for t in trades if float(t.get("pnl_rupees", 0.0)) < 0 and int(t.get("holding_days", 99)) <= 14)
        rmults = [float(t.get("r_multiple", 0.0)) for t in trades]
        out[reg] = {
            "sessions": n_sess,
            "pct_of_sessions": round(100.0 * n_sess / max(1, len(dates) - 1), 2),
            "compounded_return_pct": round((cum_ret - 1.0) * 100.0, 2),
            "daily_sr": round(daily_sr, 5),
            "annualised_sharpe": round(ann_sr, 3),
            "in_regime_max_dd_pct": round(max_dd * 100.0, 2),
            "trades_entered": n_tr,
            "win_rate_pct": round(100.0 * wins / n_tr, 2) if n_tr else 0.0,
            "profit_factor": round(pf, 3),
            "avg_r_multiple": round(float(np.mean(rmults)), 3) if rmults else 0.0,
            "whipsaw_14cd_rate_pct": round(100.0 * whipsaws / n_tr, 2) if n_tr else 0.0,
        }
    return out


def window_metrics_summary(
    equity_curve: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    *,
    n_trials: int = 36,
) -> dict[str, Any]:
    s = compute_backtest_stats(equity_curve, outcomes, n_trials=n_trials, initial_equity=float(equity_curve[0]["equity"]))
    rets = [
        (float(equity_curve[i]["equity"]) - float(equity_curve[i - 1]["equity"])) / float(equity_curve[i - 1]["equity"])
        for i in range(1, len(equity_curve))
        if float(equity_curve[i - 1]["equity"]) > 0
    ]
    m = len(rets)
    arr = np.array(rets, dtype=np.float64) if m >= 2 else np.array([0.0, 0.0])
    sd = float(np.std(arr, ddof=1))
    daily_sr = float(np.mean(arr) / sd) if sd > 1e-12 else 0.0
    std_sr_trials = max(0.5 / math.sqrt(252.0), 1.0 / math.sqrt(max(1, m)))
    sr0 = expected_max_sharpe(n_trials, std_sr=std_sr_trials)
    return {
        "sessions": len(equity_curve),
        "n_returns": m,
        "total_return_pct": round(s.total_return_pct * 100.0, 2),
        "cagr_pct": round(s.cagr_pct * 100.0, 2),
        "daily_sr": round(daily_sr, 5),
        "sharpe_annualised": s.sharpe_annualised,
        "psr": s.psr,
        "dsr_n36": s.dsr,
        "sr0_n36_daily": round(sr0, 5),
        "sr0_n36_ann": round(sr0 * math.sqrt(252.0), 3),
        "max_dd_pct": round(s.max_drawdown_pct * 100.0, 2),
        "calmar": round(s.cagr_pct / s.max_drawdown_pct, 3) if s.max_drawdown_pct > 0 else 0.0,
        "trades": s.n_trades,
        "win_rate_pct": round(s.hit_rate * 100.0, 2),
        "profit_factor": s.profit_factor,
        "expectancy_r": s.expectancy_r,
    }


def _counterfactual_trade(
    sym: str,
    signal_d: date,
    d_pos: int,
    ref_close: float,
    stop_px: float,
    w: Any,
    all_sessions: list[date],
    bars_by_date: dict[date, dict[str, BarData]],
    horizon_sessions: int = 42,
) -> dict[str, Any] | None:
    if d_pos + 2 >= len(all_sessions):
        return None
    next_d = all_sessions[d_pos + 1]
    nb = bars_by_date.get(next_d, {}).get(sym)
    if nb is None or nb.open <= stop_px or nb.open > ref_close * (1.0 + w.entry_band_pct) or nb.open < ref_close * (1.0 - w.entry_band_pct):
        return None

    entry_px = nb.open
    risk_per_share = max(1e-6, entry_px - stop_px)
    cur_stop = stop_px
    max_hi = max(entry_px, nb.high)
    exit_px = nb.close

    if nb.low <= cur_stop:
        exit_px = min(nb.open, cur_stop)
    else:
        for step in range(2, horizon_sessions + 1):
            if d_pos + step >= len(all_sessions):
                break
            sd = all_sessions[d_pos + step]
            b = bars_by_date.get(sd, {}).get(sym)
            if b is None:
                continue
            if b.open <= cur_stop:
                exit_px = b.open
                break
            if b.low <= cur_stop:
                exit_px = cur_stop
                break
            max_hi = max(max_hi, b.high)
            if max_hi > entry_px * (1.0 + w.trail_activate_gain_pct):
                cand = round(max_hi * (1.0 - w.trail_drawdown_pct), 2)
                if cand > cur_stop:
                    cur_stop = cand
            exit_px = b.close

    qty = max(1, int(65_000.0 // entry_px))
    gross_in, gross_out = qty * entry_px, qty * exit_px
    costs = apply_costs(gross_in, gross_out).total
    net_ret_pct = 100.0 * ((gross_out - gross_in - costs) / gross_in)
    return {
        "symbol": sym,
        "signal_d": signal_d.isoformat(),
        "net_ret_pct": round(net_ret_pct, 3),
        "r_multiple": round((exit_px - entry_px) / risk_per_share, 3),
    }


def simulate_legacy_1pct_and_counterfactuals(
    panel_v2: pl.DataFrame,
    bars_by_date: dict[date, dict[str, BarData]],
    all_sessions: list[date],
    active_sessions: list[date],
    macro_reg_map: dict[date, str],
) -> dict[str, Any]:
    w = load_challenger_weights()
    corr_idx = ResidualCorrelationIndex(panel_v2)
    pit_univ = (
        panel_v2.with_columns(
            pl.col("adtv_126").fill_null(0.0).rank(method="ordinal", descending=True).over("d").alias("_lr")
        )
        .filter(pl.col("_lr") <= w.pit_liquidity_top_n)
        .drop("_lr")
    )
    day_panels = pit_univ.partition_by("d", as_dict=True, maintain_order=True)
    rebal_dates = monthly_rebalance_dates(all_sessions)
    session_idx_map = {d: idx for idx, d in enumerate(all_sessions)}

    broker = PaperBroker(initial_equity=500_000.0, arm=Arm.DETERMINISTIC)
    rejected_signals: list[dict[str, Any]] = []
    max_open_risk_pct = 0.0
    first_day = True

    for d in active_sessions:
        session_ts = datetime.combine(d, datetime.min.time(), tzinfo=UTC)
        btoday = bars_by_date.get(d, {})
        broker.step_session(d, btoday, run_id="LEGACY-1PCT", ts=session_ts)

        for oid, lot in list(broker.open_lots.items()):
            if lot.max_high_seen > lot.entry_price * (1.0 + w.trail_activate_gain_pct):
                cand_stop = round(lot.max_high_seen * (1.0 - w.trail_drawdown_pct), 2)
                if cand_stop > lot.stop:
                    broker.trail_stop(oid, cand_stop, run_id="LEGACY-1PCT", ts=session_ts)

        pf = broker.portfolio()
        open_risk = sum(max(0.0, (lot.last_price - lot.stop) * lot.qty) for lot in broker.open_lots.values())
        if pf.equity > 0:
            max_open_risk_pct = max(max_open_risk_pct, 100.0 * open_risk / pf.equity)

        dp = day_panels.get((d,), pit_univ.head(0))
        if not (d in rebal_dates or first_day):
            continue
        first_day = False
        if dp.is_empty():
            continue

        scored = score_universe_v2(dp, cfg=DEFAULT, weights=w, use_pit_liquidity=False)
        rank_map = {r["symbol"]: int(r["rank"]) for r in scored.select(["symbol", "rank"]).iter_rows(named=True)}
        s200_map = {r["symbol"]: float(r["sma200"] or 0.0) for r in dp.select(["symbol", "sma200"]).iter_rows(named=True)}

        surviving_syms: set[str] = set()
        ind_counts: dict[str, int] = {}
        exiting_notional = 0.0
        for oid, lot in list(broker.open_lots.items()):
            cur_c = btoday[lot.symbol].close if lot.symbol in btoday else lot.last_price
            s200 = s200_map.get(lot.symbol, 0.0)
            rk = rank_map.get(lot.symbol, 999_999)
            if rk > w.hold_rank_buffer or (s200 > 0 and cur_c < s200):
                broker.schedule_exit(oid, reason="RANK_BUFFER_OR_SMA200")
                exiting_notional += lot.qty * lot.last_price
            else:
                surviving_syms.add(lot.symbol)
                ind_counts[lot.industry] = ind_counts.get(lot.industry, 0) + 1

        buy_pool = scored.filter(
            (pl.col("close") > pl.col("sma200"))
            & (pl.col("sma50") > pl.col("sma200"))
            & (pl.col("close") > pl.col("sma50"))
            & (pl.col("mom_12_1") > w.min_mom_12_1)
            & (pl.col("ret_126d") > w.min_ret_126d)
        )
        pf = broker.portfolio()
        slots_free = 7 - len(surviving_syms)
        avail_cash = pf.cash + exiting_notional * 0.85 - pf.equity * 0.08
        wdates = corr_idx.window_dates_for(d)
        wset = set(wdates) if len(wdates) >= corr_idx.min_periods else None
        pending_syms = {o.symbol for o in broker.pending_orders.values()}
        d_pos = session_idx_map[d]
        orders: list[Order] = []

        for row in buy_pool.head(35).iter_rows(named=True):
            sym = str(row["symbol"])
            if sym in surviving_syms or sym in pending_syms:
                continue
            ind = str(row.get("industry") or "Unclassified")
            price = float(row["close"])
            stop_px = round(price * (1.0 - w.initial_stop_pct), 2)
            qty = min(
                int((pf.equity * 0.01) // max(1e-6, price - stop_px)),
                int((pf.equity * 0.92 / 7.0) // price),
            )
            notional = qty * price

            rej_reason = None
            if len(orders) >= max(0, slots_free):
                rej_reason = "SLOT_FULL_7_STOCKS"
            elif ind_counts.get(ind, 0) >= 1:
                rej_reason = "SECTOR_CAP_1PCT"
            elif wset and any(corr_idx.corr(d, sym, s_sym, _wset=wset) >= w.max_pair_corr for s_sym in surviving_syms):
                rej_reason = "CORR_CAP"
            elif avail_cash < DEFAULT.min_notional or notional < 25_000.0:
                rej_reason = "CASH_OR_NOTIONAL"

            if rej_reason is not None:
                if rej_reason in ("SLOT_FULL_7_STOCKS", "SECTOR_CAP_1PCT", "CORR_CAP") and int(row["rank"]) <= 25:
                    cf = _counterfactual_trade(sym, d, d_pos, price, stop_px, w, all_sessions, bars_by_date)
                    if cf is not None:
                        cf["reason"] = rej_reason
                        cf["industry"] = ind
                        cf["rank"] = int(row["rank"])
                        cf["regime"] = macro_reg_map.get(d, "UNKNOWN")
                        rejected_signals.append(cf)
                continue

            orders.append(
                Order(
                    order_id=f"LEG-{d.isoformat()}-{sym}",
                    symbol=sym,
                    industry=ind,
                    placed_d=d,
                    entry_lo=round(price * (1.0 - w.entry_band_pct), 2),
                    entry_hi=round(price * (1.0 + w.entry_band_pct), 2),
                    stop=stop_px,
                    target=round(price * w.target_multiple, 2),
                    qty=qty,
                    arm=Arm.DETERMINISTIC,
                    max_sessions=252,
                )
            )
            surviving_syms.add(sym)
            ind_counts[ind] = ind_counts.get(ind, 0) + 1
            avail_cash -= notional * 1.02

        if orders:
            broker.place_orders(orders, run_id="LEGACY-1PCT", ts=session_ts)

    by_reason: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_ind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cf in rejected_signals:
        by_reason[cf["reason"]].append(cf)
        by_ind[cf["industry"]].append(cf)

    def _cf_agg(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {"count": 0, "win_rate_pct": 0.0, "avg_ret_pct": 0.0, "avg_r": 0.0, "total_pnl_on_65k": 0.0}
        rets = [x["net_ret_pct"] for x in items]
        rs = [x["r_multiple"] for x in items]
        return {
            "count": len(items),
            "win_rate_pct": round(100.0 * sum(1 for r in rets if r > 0) / len(items), 2),
            "avg_ret_pct": round(float(np.mean(rets)), 3),
            "avg_r": round(float(np.mean(rs)), 3),
            "total_pnl_on_65k": round(sum(65_000.0 * (r / 100.0) for r in rets), 2),
        }

    top_missed = sorted(((ind, _cf_agg(v)) for ind, v in by_ind.items()), key=lambda kv: kv[1]["total_pnl_on_65k"], reverse=True)[:10]
    return {
        "stats": window_metrics_summary(broker.equity_curve, broker.outcomes, n_trials=36),
        "max_open_risk_pct": round(max_open_risk_pct, 2),
        "regime_attribution": compute_regime_attribution(broker.equity_curve, broker.outcomes, macro_reg_map),
        "rejected_summary": {k: _cf_agg(v) for k, v in by_reason.items()},
        "rejected_overall": _cf_agg(rejected_signals),
        "top_missed_industries": [{"industry": ind, **agg} for ind, agg in top_missed],
    }


def worst_drawdowns(equity_curve: list[dict[str, Any]], macro_reg_map: dict[date, str], top_k: int = 5) -> list[dict[str, Any]]:
    peak, peak_d, cur_trough, cur_trough_d, in_dd = -1.0, None, 1e18, None, False
    episodes: list[dict[str, Any]] = []
    for r in equity_curve:
        d = r["d"] if isinstance(r["d"], date) else date.fromisoformat(str(r["d"]))
        eq = float(r["equity"])
        if eq >= peak:
            if in_dd and peak_d and cur_trough_d:
                dd_pct = 100.0 * (peak - cur_trough) / peak
                if dd_pct >= 5.0:
                    episodes.append({
                        "start": peak_d.isoformat(),
                        "trough": cur_trough_d.isoformat(),
                        "recovery": d.isoformat(),
                        "dd_pct": round(dd_pct, 2),
                        "dominant_regime": macro_reg_map.get(cur_trough_d, "UNKNOWN"),
                    })
            peak, peak_d, cur_trough, cur_trough_d, in_dd = eq, d, eq, d, False
        else:
            in_dd = True
            if eq < cur_trough:
                cur_trough, cur_trough_d = eq, d
    if in_dd and peak_d and cur_trough_d and peak > 0:
        dd_pct = 100.0 * (peak - cur_trough) / peak
        if dd_pct >= 5.0:
            episodes.append({
                "start": peak_d.isoformat(),
                "trough": cur_trough_d.isoformat(),
                "recovery": "OPEN",
                "dd_pct": round(dd_pct, 2),
                "dominant_regime": macro_reg_map.get(cur_trough_d, "UNKNOWN"),
            })
    return sorted(episodes, key=lambda x: x["dd_pct"], reverse=True)[:top_k]


def main() -> None:
    t0 = pytime.perf_counter()
    audit = audit_psr_dsr_formulas()
    print("[1] PSR/DSR Audit:", json.dumps(audit, indent=2))

    with GuardedClient() as ch:
        caps = inventory_capabilities(ch)
        print("[0] Pure NSE Only:", caps["pure_nse_only"], "| Indices:", caps["total_official_indices"])
        end_d = latest_session(ch)
        a = AsOf(datetime.now(UTC))
        bars, members = load_panel_inputs(ch, end_d, a=a, lookback=1600)
        idx_df = ch.query_admin("SELECT d, index_name, open, high, low, close FROM md.index_1d WHERE index_name = 'Nifty 500' ORDER BY d")
        vix_df = ch.query_admin("SELECT d, index_name, close FROM md.index_1d WHERE index_name = 'India VIX' ORDER BY d")

    bars_eq = bars.filter(pl.col("series") == "EQ")
    all_sessions = sorted(bars_eq["d"].unique().to_list())
    active_1250 = all_sessions[-1250:]

    panel_full = _build_v2_panel(bars_eq, members)
    idx_feat = build_macro_regime_frame(idx_df, vix_df, panel_full)
    idx_1250 = idx_feat.filter(pl.col("d").is_in(active_1250))
    macro_reg_map = {r["d"]: r["macro_regime"] for r in idx_feat.iter_rows(named=True)}

    reg_counts = idx_1250.group_by("macro_regime").len().sort("len", descending=True)
    print("\n[2] 1250-Session Macro Regime Distribution (Nifty 500 + India VIX + Breadth):")
    print(reg_counts)

    window_results: dict[int, dict[str, Any]] = {}
    curve_1250: list[dict[str, Any]] = []
    for win in WINDOWS:
        res = run_backtest_v2(
            bars_eq,
            members,
            start_date=all_sessions[-win],
            end_date=end_d,
            arm=Arm.DETERMINISTIC,
            cfg=DEFAULT,
            n_trials=36,
            warmup_sessions=40,
            use_pit_liquidity=True,
        )
        wm = window_metrics_summary(res.equity_curve, res.outcomes, n_trials=36)
        wm["regime_attribution"] = compute_regime_attribution(res.equity_curve, res.outcomes, macro_reg_map)
        window_results[win] = wm
        if win == 1250:
            curve_1250 = res.equity_curve

    print("\n[3] Production V2 Baseline Across Rolling Windows (Pure NSE):")
    for win in WINDOWS:
        wm = window_results[win]
        print(
            f"  W={win:4d}: Ret={wm['total_return_pct']:6.1f}%, CAGR={wm['cagr_pct']:5.1f}%, "
            f"DailySR={wm['daily_sr']:.4f}, AnnSR={wm['sharpe_annualised']:.2f}, "
            f"PSR={wm['psr']:.4f}, DSR(N36)={wm['dsr_n36']:.4f} (sr0={wm['sr0_n36_ann']:.2f} ann), "
            f"MaxDD={wm['max_dd_pct']:.2f}%, WR={wm['win_rate_pct']:.1f}%"
        )

    print("\n[4] 1250-Session Regime Bleed Attribution (Production V2):")
    for reg, att in window_results[1250]["regime_attribution"].items():
        print(f"  {reg:22s}: {att}")

    dd_windows = worst_drawdowns(curve_1250, macro_reg_map)
    print("\n[5] Worst Drawdown Windows (1250-Session V2):", json.dumps(dd_windows, indent=2))

    bars_by_date: dict[date, dict[str, BarData]] = {}
    for r in panel_full.select(["symbol", "d", "open", "high", "low", "close"]).iter_rows(named=True):
        rd = r["d"]
        bars_by_date.setdefault(rd, {})[r["symbol"]] = BarData(
            symbol=str(r["symbol"]),
            d=rd,
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
        )

    legacy_1pct = simulate_legacy_1pct_and_counterfactuals(
        panel_full, bars_by_date, all_sessions, active_1250, macro_reg_map
    )
    print("\n[6] Legacy 7-Stock / 1%-Per-Stock / 1%-Per-Industry Replay & Counterfactual Alpha:")
    print("  Legacy 1% Stats:", json.dumps(legacy_1pct["stats"]))
    print("  Max Open Risk %:", legacy_1pct["max_open_risk_pct"])
    print("  Rejected Signal Summary:", json.dumps(legacy_1pct["rejected_summary"], indent=2))
    print("  Overall Rejected Counterfactual:", json.dumps(legacy_1pct["rejected_overall"]))
    print("  Top Missed Industries:", json.dumps(legacy_1pct["top_missed_industries"][:6], indent=2))

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_sec": round(pytime.perf_counter() - t0, 2),
        "system_capabilities": caps,
        "data_range": {
            "bars_loaded_sessions": len(all_sessions),
            "active_1250_start": active_1250[0].isoformat(),
            "active_1250_end": active_1250[-1].isoformat(),
            "macro_regime_distribution_1250": reg_counts.to_dicts(),
        },
        "psr_dsr_audit": audit,
        "v2_windows": {str(k): v for k, v in window_results.items()},
        "worst_drawdowns_1250": dd_windows,
        "legacy_1pct_and_counterfactuals": legacy_1pct,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"\nSaved Phase 1 diagnostics -> {OUT_PATH} in {payload['elapsed_sec']}s")


if __name__ == "__main__":
    main()
