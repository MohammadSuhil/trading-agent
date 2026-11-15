"""V2 Challenger Strategy (ADR-0007): Sortino Risk-Adjusted Momentum with Monthly Rank-Buffer.

Replaces v1's high-churn 10-day SWING cap with:
  1. Universe-wide cross-sectional Sortino momentum (`mom_12_1 / dvol_252`, `ret_126d / dvol_252`,
     `dist_52w_high`) — penalizing downside crash volatility without truncating upside breakouts.
  2. Zero-survivorship Point-in-Time Top-350 liquidity universe (`adtv_126` rank <= 350 on session d).
  3. Stage-2 uptrend gate (`close > sma50 > sma200`, `mom_12_1 > 5%`, `ret_126d > 5%`).
  4. Anchored monthly rank-buffer rebalance (buy Top-7 slots, hold while ranked <= 25 and
     `close >= sma200`), protected daily by `12.5%` initial stop (`RISK-03`), `18%` trailing stop
     after `+8%` gain, `RISK-06` industry caps, and `RISK-08` residual correlation `< 0.60`.

Enforces Invariant P-01 & P-03: executes through `PaperBroker` (`simulate_fill` + `apply_costs`)
and emits a hash-chained `LedgerEvent` stream reconstructible via `rebuild_from_ledger`.
"""
from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any

import numpy as np
import polars as pl
from ta_core.costs import compute_costs
from ta_core.enums import Action, Arm, Horizon, ProductType, ReasonCode, Regime, Sleeve
from ta_ledger.broker.base import BarData, Order
from ta_ledger.broker.paper import PaperBroker
from ta_ledger.manifest import RunManifest
from ta_ledger.risk import DEFAULT, TIERED_DEFAULT, Portfolio, Position, RiskConfig, clamp_portfolio_principal_heat

from .barriers import BarrierModel, build_path_library
from .factors.correlation import ResidualCorrelationIndex
from .regime import EntryBlock, MacroRegime, classify_macro_regime_panel, regime_on
from .stats import compute_backtest_stats
from .weights import ChallengerWeights, load_challenger_weights

if TYPE_CHECKING:
    from .backtest.engine import BacktestResult

__all__ = [
    "compute_multivariant_bonus",
    "monthly_rebalance_dates",
    "run_backtest_v2",
    "score_universe_v2",
    "screen_session_v2",
]


def compute_multivariant_bonus(
    res_mom_63: float,
    er_63: float,
    sym_barrier_edge: float,
    up_dn_vol_20: float,
) -> float:
    """Additive orthogonal meta-voter composite bonus over Sortino baseline rank."""
    return (
        0.25 * max(-1.5, min(2.5, float(res_mom_63)))
        + 0.80 * (float(er_63) - 0.30)
        + 2.5 * float(sym_barrier_edge)
        + 0.20 * max(-1.0, min(1.5, float(up_dn_vol_20) - 1.0))
    )


def monthly_rebalance_dates(sessions: Sequence[date]) -> set[date]:
    """Return the first observed session of each `(year, month)` across sorted sessions.

    >>> from datetime import date
    >>> dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 2, 2), date(2026, 2, 3)]
    >>> sorted(monthly_rebalance_dates(dates)) == [date(2026, 1, 5), date(2026, 2, 2)]
    True
    """
    rebal: set[date] = set()
    seen_months: set[tuple[int, int]] = set()
    for d in sorted(sessions):
        key = (d.year, d.month)
        if key not in seen_months:
            rebal.add(d)
            seen_months.add(key)
    return rebal


def _ind_val(table: dict[str, dict] | None, industry: str, key: str) -> str:
    if not table:
        return "UNDEFINED"
    return str((table.get(industry) or {}).get(key) or "UNDEFINED")


def score_universe_v2(
    day_panel: pl.DataFrame,
    *,
    tradeable: set[str] | None = None,
    cfg: RiskConfig = DEFAULT,
    weights: ChallengerWeights | None = None,
    use_pit_liquidity: bool = False,
) -> pl.DataFrame:
    """Cross-sectional Sortino-momentum ranking with hard liquidity/price gates."""
    if day_panel.is_empty():
        return day_panel

    w = weights or load_challenger_weights()
    df = day_panel

    if tradeable:
        df = df.filter(pl.col("symbol").is_in(sorted(tradeable)))

    if use_pit_liquidity and "adtv_126" in df.columns and df.height > w.pit_liquidity_top_n:
        df = (
            df.with_columns(
                pl.col("adtv_126")
                .fill_null(0.0)
                .rank(method="ordinal", descending=True)
                .alias("_liq_rank")
            )
            .filter(pl.col("_liq_rank") <= w.pit_liquidity_top_n)
            .drop("_liq_rank")
        )

    if df.is_empty():
        return df

    strict_prod = cfg.min_listed_sessions >= 150
    min_bars = max(cfg.min_listed_sessions, 252) if strict_prod else cfg.min_listed_sessions
    min_px = max(cfg.min_price, w.min_price) if strict_prod else cfg.min_price

    df = df.filter(
        (pl.col("series") == "EQ")
        & (pl.col("adtv_20") >= cfg.min_adtv_rupees)
        & (pl.col("n_trades") >= cfg.min_trades_per_day)
        & (pl.col("close") >= min_px)
        & (pl.col("bars_seen") >= min_bars)
    )
    if strict_prod and all(c in df.columns for c in ("trend_20", "trend_63", "trend_252")):
        df = df.filter(
            (pl.col("trend_20") != "UNDEFINED")
            & (pl.col("trend_63") != "UNDEFINED")
            & (pl.col("trend_252") != "UNDEFINED")
        )
    if df.is_empty():
        return df

    if strict_prod:
        df = df.filter(
            pl.col("mom_12_1").is_not_null()
            & pl.col("ret_126d").is_not_null()
            & pl.col("dvol_252").is_not_null()
            & pl.col("dist_52w_high").is_not_null()
        )
    else:
        # Support compact synthetic unit tests with <252 bars
        mom12 = pl.col("mom_12_1") if "mom_12_1" in df.columns else pl.lit(None, dtype=pl.Float64)
        ret6m = pl.col("ret_126d") if "ret_126d" in df.columns else pl.lit(None, dtype=pl.Float64)
        dvol = pl.col("dvol_252") if "dvol_252" in df.columns else pl.lit(None, dtype=pl.Float64)
        dist52 = pl.col("dist_52w_high") if "dist_52w_high" in df.columns else pl.lit(0.0)
        fallback_ret = pl.col("ret_21d") if "ret_21d" in df.columns else pl.lit(0.05)
        fallback_vol = pl.col("realised_vol_21d") if "realised_vol_21d" in df.columns else pl.lit(0.15)
        df = df.with_columns(
            pl.coalesce([mom12, fallback_ret]).alias("mom_12_1"),
            pl.coalesce([ret6m, fallback_ret]).alias("ret_126d"),
            pl.coalesce([dvol, fallback_vol, pl.lit(0.15)]).alias("dvol_252"),
            dist52.fill_null(0.0).alias("dist_52w_high"),
        ).filter(pl.col("mom_12_1").is_not_null())

    if df.is_empty():
        return df

    def z_xs(e: pl.Expr) -> pl.Expr:
        if df.height <= 1:
            return pl.lit(0.0)
        std = e.std()
        return ((e - e.mean()) / pl.when(std > 0).then(std).otherwise(1.0)).fill_nan(0.0).fill_null(0.0)

    so_12_1 = pl.col("mom_12_1") / pl.col("dvol_252").clip(lower_bound=0.10)
    so_6m = pl.col("ret_126d") / pl.col("dvol_252").clip(lower_bound=0.10)
    cw = w.composite_weights

    df = (
        df.with_columns(
            (
                float(cw.get("sortino_mom_12_1", 0.45)) * z_xs(so_12_1)
                + float(cw.get("sortino_mom_6m", 0.35)) * z_xs(so_6m)
                + float(cw.get("dist_52w_high", 0.20)) * z_xs(pl.col("dist_52w_high"))
            ).alias("composite")
        )
        .sort(["composite", "symbol"], descending=[True, False])
        .with_columns(pl.int_range(1, pl.len() + 1).alias("rank"))
    )
    return df.drop([c for c in df.columns if c.startswith("_")])


def screen_session_v2(
    day_panel: pl.DataFrame,
    *,
    d: date,
    portfolio: Portfolio,
    is_rebalance_day: bool = True,
    tradeable: set[str] | None = None,
    use_pit_liquidity: bool = False,
    corr_index: ResidualCorrelationIndex | None = None,
    ind_today: dict[str, dict] | None = None,
    bmodel: BarrierModel | None = None,
    cfg: RiskConfig = DEFAULT,
    weights: ChallengerWeights | None = None,
    entries_blocked: bool = False,
    block_note: str = "",
    max_cards: int = 25,
    run_id: str | None = None,
) -> pl.DataFrame:
    """Produce live recommendation cards matching `pipeline.run_nightly` schema."""
    w = weights or load_challenger_weights()
    run_id = run_id or uuid.uuid4().hex[:12]

    scored = score_universe_v2(
        day_panel,
        tradeable=tradeable,
        cfg=cfg,
        weights=w,
        use_pit_liquidity=use_pit_liquidity,
    )
    rank_map: dict[str, int] = (
        {r["symbol"]: int(r["rank"]) for r in scored.select(["symbol", "rank"]).iter_rows(named=True)}
        if not scored.is_empty()
        else {}
    )
    day_by_sym: dict[str, dict[str, Any]] = {
        r["symbol"]: r for r in day_panel.iter_rows(named=True)
    } if not day_panel.is_empty() else {}

    recs: list[dict[str, Any]] = []
    exiting_notional = 0.0
    surviving_syms: set[str] = set()
    ind_counts: dict[str, int] = {}
    prior_positions = tuple(p for p in portfolio.positions if p.opened < d)
    owned_all = {p.symbol for p in portfolio.positions}
    strict_prod = cfg.min_listed_sessions >= 150
    hold_positions: list[Position] = []
    hold_rec_idx: dict[str, int] = {}
    hold_meta: dict[str, tuple[float, float]] = {}

    # 1. Held positions -> SELL or HOLD (Invariant I-16)
    for pos in prior_positions:
        row = day_by_sym.get(pos.symbol, {})
        close = float(row.get("close") or pos.last)
        sma200 = float(row.get("sma200") or 0.0)
        atr = float(row.get("atr14") or (close * 0.02))
        rk = rank_map.get(pos.symbol, 999_999)
        ret_z20_pos = float(row.get("ret_z_20") or 0.0)
        hold_days = (d - pos.opened).days if hasattr(pos.opened, "year") else 0

        hit_stop = close <= pos.stop
        hit_sma = sma200 > 0 and close < sma200
        hit_rank = is_rebalance_day and rk > w.hold_rank_buffer
        hit_swing_exit = (
            strict_prod
            and pos.horizon == Horizon.SWING.value
            and ((close >= pos.entry * 1.03 and ret_z20_pos >= 0.45) or hold_days >= 18)
        )
        is_sell = hit_stop or hit_sma or hit_rank or hit_swing_exit

        if is_sell:
            action = Action.SELL.value
            exiting_notional += pos.qty * close
            reasons = [ReasonCode.STOP_DISCIPLINE.value if hit_stop else ReasonCode.REGIME_RISK_OFF.value]
            if hit_stop:
                note = "STOP_DISCIPLINE: price breached stop"
            elif hit_sma:
                note = "TREND_REVERSAL: close broke below 200-DMA"
            elif hit_swing_exit:
                note = (
                    "TIME_STOP: swing holding limit reached"
                    if hold_days >= 18
                    else "TARGET_REACHED: OU equilibrium mean-reversion exit"
                )
            else:
                note = f"ROTATE: rank {rk} > {w.hold_rank_buffer}"
            st = round(pos.stop, 2)
        else:
            action = Action.HOLD.value
            surviving_syms.add(pos.symbol)
            ind_counts[pos.industry] = ind_counts.get(pos.industry, 0) + 1
            reasons = [ReasonCode.MULTI_HORIZON_MOMENTUM.value, ReasonCode.MA_STACK_ALIGNED.value]
            if rk <= 10_000:
                note = f"HOLD: rank #{rk} <= {w.hold_rank_buffer}, above 200-DMA"
            else:
                note = f"HOLD: {pos.horizon} position above 200-DMA"
            if close > pos.entry * (1.0 + w.trail_activate_gain_pct):
                st = max(round(pos.stop, 2), round(close * (1.0 - w.trail_drawdown_pct), 2))
            else:
                st = round(pos.stop, 2)

        tgt = round(pos.target if pos.target > 0 else pos.entry * w.target_multiple, 2)
        e_lo = round(pos.entry * (1.0 - w.entry_band_pct), 2)
        e_hi = round(pos.entry * (1.0 + w.entry_band_pct), 2)
        notional = round(pos.qty * close, 2)

        if not is_sell:
            hold_rec_idx[pos.symbol] = len(recs)
            hold_positions.append(
                Position(
                    symbol=pos.symbol,
                    industry=pos.industry,
                    qty=pos.qty,
                    entry=pos.entry,
                    last=close,
                    stop=st,
                    opened=pos.opened,
                    target=tgt,
                    horizon=pos.horizon,
                )
            )
            hold_meta[pos.symbol] = (close, tgt)

        recs.append({
            "rec_id": f"{run_id}-{pos.symbol}",
            "d": d,
            "symbol": pos.symbol,
            "name": str(row.get("company_name") or pos.symbol),
            "industry": pos.industry,
            "arm": Arm.DETERMINISTIC.value,
            "action": action,
            "sleeve": Sleeve.MOMENTUM_BREAKOUT.value,
            "horizon": pos.horizon if strict_prod and pos.horizon else Horizon.POSITIONAL.value,
            "entry_lo": e_lo,
            "entry_hi": e_hi,
            "stop": st,
            "target": tgt,
            "qty": pos.qty,
            "notional": notional,
            "would_qty": pos.qty,
            "would_notional": notional,
            "would_risk_rupees": round(pos.qty * max(0.0, close - st), 2),
            "afford_note": note,
            "risk_rupees": round(pos.qty * max(0.0, close - st), 2),
            "risk_pct": round((pos.qty * max(0.0, close - st)) / portfolio.equity, 5) if portfolio.equity > 0 else 0.0,
            "rr": round((tgt - close) / max(0.01, close - st), 2),
            "atr14": round(atr, 4),
            "cost_bps": 0.0,
            "expectancy_atr": 0.0,
            "composite": round(float(row.get("composite") or 0.0), 4),
            "reasons": reasons,
            "trend_20": str(row.get("trend_20") or "UNDEFINED"),
            "trend_63": str(row.get("trend_63") or "UNDEFINED"),
            "trend_252": str(row.get("trend_252") or "UNDEFINED"),
            "ind_trend_20": _ind_val(ind_today, pos.industry, "ind_trend_20"),
            "ind_trend_63": _ind_val(ind_today, pos.industry, "ind_trend_63"),
            "ind_trend_252": _ind_val(ind_today, pos.industry, "ind_trend_252"),
            "state": "OK",
            "p_up": 0.5,
            "p_dn": 0.5,
            "p_vert": 0.0,
            "ci_lo": 0.4,
            "ci_hi": 0.6,
            "n_eff": 100.0,
            "reason": "open position tracked by portfolio",
            "wide": False,
            "barrier_wide": False,
            "barrier_state": "OK",
            "barrier_reason": "open position tracked by portfolio",
        })

    ind_notional: dict[str, float] = {}
    ind_risk_rs: dict[str, float] = {}
    cur_open_risk_rs = 0.0
    if strict_prod and hold_positions:
        hold_pf = Portfolio(
            equity=portfolio.equity,
            cash=portfolio.cash,
            positions=tuple(hold_positions),
            watermark=portfolio.watermark,
        )
        clamped_positions = clamp_portfolio_principal_heat(
            hold_pf, max_heat_pct=w.tiered_max_portfolio_heat_pct
        )
        for orig_p, cp in zip(hold_positions, clamped_positions):
            new_st = round(cp.stop, 2)
            idx_rec = hold_rec_idx[cp.symbol]
            c_px, tgt_px = hold_meta[cp.symbol]
            if new_st > round(orig_p.stop, 2):
                rk_rs = round(cp.qty * max(0.0, c_px - new_st), 2)
                recs[idx_rec]["stop"] = new_st
                recs[idx_rec]["risk_rupees"] = rk_rs
                recs[idx_rec]["would_risk_rupees"] = rk_rs
                recs[idx_rec]["risk_pct"] = round(rk_rs / portfolio.equity, 5) if portfolio.equity > 0 else 0.0
                recs[idx_rec]["rr"] = round((tgt_px - c_px) / max(0.01, c_px - new_st), 2)
            ind_notional[cp.industry] = ind_notional.get(cp.industry, 0.0) + cp.qty * c_px
            r_prin = cp.principal_risk_rupees
            ind_risk_rs[cp.industry] = ind_risk_rs.get(cp.industry, 0.0) + r_prin
            cur_open_risk_rs += r_prin
    else:
        for cp in hold_positions:
            c_px, _ = hold_meta[cp.symbol]
            ind_notional[cp.industry] = ind_notional.get(cp.industry, 0.0) + cp.qty * c_px
            r_prin = cp.principal_risk_rupees
            ind_risk_rs[cp.industry] = ind_risk_rs.get(cp.industry, 0.0) + r_prin
            cur_open_risk_rs += r_prin

    if scored.is_empty():
        return pl.DataFrame(recs) if recs else pl.DataFrame()

    eff_max_pos = (
        w.tiered_max_positions
        if strict_prod
        else min(w.max_positions, max(1, int((portfolio.equity * 0.92) // 55_000.0)))
    )
    slots_free = max(0, eff_max_pos - len(surviving_syms))
    cash_floor_frac = TIERED_DEFAULT.cash_floor_pct if strict_prod else 0.08
    avail_cash = portfolio.cash + exiting_notional * 0.85 - portfolio.equity * cash_floor_frac
    target_slot_notional = (
        max(cfg.min_notional, (portfolio.equity * 0.93) / 9.0)
        if strict_prod
        else max(55_000.0, (portfolio.equity * 0.92) / eff_max_pos)
    )
    max_pair_corr = 0.65 if strict_prod else w.max_pair_corr

    corr_wset: set[date] | None = None
    if corr_index is not None:
        wdates = corr_index.window_dates_for(d)
        if len(wdates) >= corr_index.min_periods:
            corr_wset = set(wdates)

    # 2. Unowned V1..V5 candidates -> BUY (daily/monthly with 4-Tier Risk) or WATCH
    buys_placed = 0
    for row in scored.iter_rows(named=True):
        if len(recs) >= max_cards:
            break
        sym = str(row["symbol"])
        if sym in owned_all or sym in surviving_syms:
            continue

        close = float(row["close"])
        sma50 = float(row.get("sma50") or 0.0)
        sma200 = float(row.get("sma200") or 0.0)
        mom12 = float(row.get("mom_12_1") or 0.0)
        ret6m = float(row.get("ret_126d") or 0.0)
        atr = float(row.get("atr14") or (close * 0.02))
        adtv_20 = float(row.get("adtv_20") or 0.0)
        ind = str(row.get("industry") or "Unclassified")
        natr = atr / close if close > 0 else 0.025

        est_dict = (
            bmodel.estimate(natr).as_dict()
            if bmodel is not None and close > 0
            else {
                "state": "UNAVAILABLE", "p_up": None, "p_dn": None, "p_vert": None,
                "ci_lo": None, "ci_hi": None, "n_eff": 0.0,
                "reason": "v2 rank-buffer screen", "wide": True,
            }
        )
        sym_edge = (
            float(est_dict["p_up"]) - float(est_dict["p_dn"])
            if est_dict.get("state") == "OK"
            and est_dict.get("p_up") is not None
            and est_dict.get("p_dn") is not None
            else 0.0
        )

        if strict_prod:
            er63 = float(row.get("er_63") or 0.0)
            td63 = float(row.get("t_drift_63") or 0.0)
            ret63 = float(row.get("ret_63d") or 0.0)
            vol63 = float(row.get("realised_vol_63d") or 0.20)
            res_mom = ret63 / max(vol63, 0.10)
            ret_z20 = float(row.get("ret_z_20") or 0.0)
            cir = float(row.get("close_in_range") or 0.5)
            up_dn_vol = float(row.get("up_down_vol_ratio_20") or 1.0)
            ov_spread = float(row.get("overnight_ret_21d") or 0.0) - float(row.get("intraday_ret_21d") or 0.0)
            ret1d = float(row.get("ret_1d") or 0.0)

            if is_rebalance_day and close > sma200 and sma50 > sma200 and close > sma50 and mom12 > w.min_mom_12_1 and ret6m > w.min_ret_126d:
                variant_code = "V1"
                cand_sleeve = Sleeve.MOMENTUM_BREAKOUT.value
                cand_horizon = Horizon.POSITIONAL.value
                cand_reasons = ["MV_ENSEMBLE", "V1_SORTINO_MOM", ReasonCode.MA_STACK_ALIGNED.value]
                stop_pct = min(0.120, max(0.062, 2.8 * natr))
                target_mult = w.target_multiple
            elif close > sma200 and sma50 > sma200 and close > sma50 and sym_edge > 0.025 and up_dn_vol >= 1.15 and er63 >= 0.28 and cir >= 0.55:
                variant_code = "V4"
                cand_sleeve = Sleeve.SECTOR_ROTATION.value
                cand_horizon = Horizon.POSITIONAL.value
                cand_reasons = ["MV_ENSEMBLE", "V4_DIRICHLET_BARRIER", "V4_ACCUMULATION_THRUST"]
                stop_pct = 0.075
                target_mult = 2.2
            elif close > sma200 and td63 >= 1.5 and er63 >= 0.30 and res_mom > 0.25 and mom12 > w.min_mom_12_1:
                variant_code = "V2"
                cand_sleeve = Sleeve.MOMENTUM_BREAKOUT.value
                cand_horizon = Horizon.POSITIONAL.value
                cand_reasons = ["MV_ENSEMBLE", "V2_RESIDUAL_HAC", "V2_LOW_BETA_ALPHA"]
                stop_pct = 0.075
                target_mult = 2.2
            elif close > sma200 and ret_z20 <= -1.15 and er63 >= 0.20 and cir >= 0.50 and ret1d > -0.04:
                variant_code = "V3"
                cand_sleeve = Sleeve.MEAN_REVERSION.value
                cand_horizon = Horizon.SWING.value
                cand_reasons = ["MV_ENSEMBLE", "V3_OU_EQUILIBRIUM", "V3_BARRIER_FILTER"]
                stop_pct = 0.060
                target_mult = 1.35
            elif ov_spread > 0.0 and er63 >= 0.22 and ret_z20 <= -1.00 and cir >= 0.58 and ret1d > 0.0:
                variant_code = "V5"
                cand_sleeve = Sleeve.PULLBACK_IN_UPTREND.value
                cand_horizon = Horizon.SWING.value
                cand_reasons = ["MV_ENSEMBLE", "V5_CRISIS_SHIELD", "V5_OVERNIGHT_INFLECTION"]
                stop_pct = 0.060
                target_mult = 1.35
            elif close > sma200 and sma50 > sma200 and close > sma50 and mom12 > w.min_mom_12_1 and ret6m > w.min_ret_126d and er63 >= 0.22:
                variant_code = "V2"
                cand_sleeve = Sleeve.MOMENTUM_BREAKOUT.value
                cand_horizon = Horizon.POSITIONAL.value
                cand_reasons = ["MV_ENSEMBLE", "V2_RESIDUAL_HAC", ReasonCode.MA_STACK_ALIGNED.value]
                stop_pct = min(0.100, max(0.062, 2.5 * natr))
                target_mult = w.target_multiple
            else:
                continue
        else:
            variant_code = "V1"
            cand_sleeve = Sleeve.MOMENTUM_BREAKOUT.value
            cand_horizon = Horizon.POSITIONAL.value
            cand_reasons = [ReasonCode.MULTI_HORIZON_MOMENTUM.value, ReasonCode.MA_STACK_ALIGNED.value]
            stop_pct = w.initial_stop_pct
            target_mult = w.target_multiple

        max_ind = (
            max(cfg.max_industry_names, w.tiered_max_per_industry)
            if strict_prod
            else (cfg.finserv_max_names if ind == cfg.finserv_industry else cfg.max_industry_names)
        )
        ind_cap_pct = cfg.finserv_max_pct if ind == cfg.finserv_industry else 0.25
        rem_sec_rs = TIERED_DEFAULT.max_industry_heat_pct * portfolio.equity - ind_risk_rs.get(ind, 0.0)
        rem_heat_rs = w.tiered_max_portfolio_heat_pct * portfolio.equity - cur_open_risk_rs
        ind_ok = (
            (ind_counts.get(ind, 0) < max_ind)
            and (ind_notional.get(ind, 0.0) / portfolio.equity < ind_cap_pct if portfolio.equity > 0 else True)
            and (rem_sec_rs >= portfolio.equity * 0.003 if strict_prod else True)
        )
        heat_ok = (rem_heat_rs >= portfolio.equity * 0.003) if strict_prod else True
        rebal_ok = True if strict_prod else is_rebalance_day

        corr_ok = True
        if corr_index is not None and corr_wset is not None and surviving_syms:
            corr_ok = all(
                corr_index.corr(d, sym, s_sym, _wset=corr_wset) < max_pair_corr
                for s_sym in surviving_syms
            )

        would_alloc = min(target_slot_notional, adtv_20 * cfg.max_pct_of_adtv)
        would_qty = int(would_alloc // close)
        would_notional = round(would_qty * close, 2)
        stop = round(close * (1.0 - stop_pct), 2)
        target = round(close * target_mult, 2)

        can_buy = (
            rebal_ok
            and not entries_blocked
            and buys_placed < slots_free
            and ind_ok
            and corr_ok
            and heat_ok
            and avail_cash >= cfg.min_notional
        )
        qty = 0
        notional = 0.0
        if can_buy:
            max_trade_risk_rs = (0.0098 if variant_code == "V1" else 0.0085) * portfolio.equity
            risk_cap_alloc = min(max_trade_risk_rs, rem_heat_rs, rem_sec_rs) / stop_pct if strict_prod else target_slot_notional
            alloc = min(target_slot_notional, risk_cap_alloc, avail_cash / 1.02, adtv_20 * cfg.max_pct_of_adtv)
            cand_qty = int(alloc // close)
            cand_notional = round(cand_qty * close, 2)
            if cand_notional >= cfg.min_notional:
                action = Action.BUY.value
                qty = cand_qty
                notional = cand_notional
                buys_placed += 1
                surviving_syms.add(sym)
                ind_counts[ind] = ind_counts.get(ind, 0) + 1
                trade_risk_rs = round(cand_notional * stop_pct, 2)
                ind_notional[ind] = ind_notional.get(ind, 0.0) + cand_notional
                ind_risk_rs[ind] = ind_risk_rs.get(ind, 0.0) + trade_risk_rs
                cur_open_risk_rs += trade_risk_rs
                avail_cash -= cand_notional * 1.02
                afford_note = (
                    f"{variant_code} slot #{len(surviving_syms)} (rank #{row['rank']}, heat {cur_open_risk_rs / portfolio.equity * 100:.2f}%)"
                    if strict_prod and portfolio.equity > 0
                    else f"rebalance slot #{buys_placed} (rank #{row['rank']})"
                )
            else:
                action = Action.WATCH.value
                rank_tag = f"(rank #{row['rank']}, {variant_code})"
                if avail_cash / 1.02 < cfg.min_notional:
                    afford_note = f"RISK-05c: cash headroom below Rs 50,000 floor {rank_tag}"
                elif strict_prod and rem_heat_rs / stop_pct < cfg.min_notional:
                    afford_note = f"TIER-04: portfolio heat headroom below Rs 50,000 floor {rank_tag}"
                elif strict_prod and rem_sec_rs / stop_pct < cfg.min_notional:
                    afford_note = f"TIER-03: {ind} risk headroom below Rs 50,000 floor {rank_tag}"
                else:
                    afford_note = f"RISK-01: below Rs 50,000 position floor {rank_tag}"
        else:
            action = Action.WATCH.value
            rank_tag = f"(rank #{row['rank']}, {variant_code})"
            if entries_blocked:
                afford_note = f"ENTRY-BLOCK: {block_note or 'new entries blocked by regime'} {rank_tag}"
            elif not rebal_ok:
                afford_note = f"WATCH: awaiting monthly rebalance {rank_tag}"
            elif not heat_ok:
                afford_note = f"TIER-04: portfolio principal heat ceiling reached {rank_tag}"
            elif not ind_ok:
                afford_note = f"TIER-03: industry cap reached for {ind} {rank_tag}"
            elif not corr_ok:
                afford_note = f"TIER-04: pairwise residual correlation >= {max_pair_corr:.2f} {rank_tag}"
            elif avail_cash < cfg.min_notional:
                afford_note = f"RISK-05c: deployable cash below Rs 50,000 floor {rank_tag}"
            else:
                afford_note = f"TIER-02: portfolio slots full {rank_tag}"

        b_reason = f"ENTRY-BLOCK: {block_note}" if entries_blocked else str(est_dict["reason"])
        costs = compute_costs(max(notional, cfg.min_notional), max(notional, cfg.min_notional), ProductType.DELIVERY)

        recs.append({
            "rec_id": f"{run_id}-{sym}",
            "d": d,
            "symbol": sym,
            "name": str(row.get("company_name") or sym),
            "industry": ind,
            "arm": Arm.DETERMINISTIC.value,
            "action": action,
            "sleeve": cand_sleeve,
            "horizon": cand_horizon,
            "entry_lo": round(close * (1.0 - w.entry_band_pct), 2),
            "entry_hi": round(close * (1.0 + w.entry_band_pct), 2),
            "stop": stop,
            "target": target,
            "qty": qty,
            "notional": notional,
            "would_qty": would_qty,
            "would_notional": would_notional,
            "would_risk_rupees": round(would_qty * (close - stop), 2),
            "afford_note": afford_note,
            "risk_rupees": round(qty * (close - stop), 2) if qty > 0 else 0.0,
            "risk_pct": round((qty * (close - stop)) / portfolio.equity, 5) if qty > 0 and portfolio.equity > 0 else 0.0,
            "rr": round((target - close) / max(0.01, close - stop), 2),
            "atr14": round(atr, 4),
            "cost_bps": round(costs.bps_of(max(notional, cfg.min_notional)), 2),
            "expectancy_atr": 0.0,
            "composite": round(float(row["composite"]), 4),
            "reasons": cand_reasons,
            "trend_20": str(row.get("trend_20") or "UP"),
            "trend_63": str(row.get("trend_63") or "UP"),
            "trend_252": str(row.get("trend_252") or "UP"),
            "ind_trend_20": _ind_val(ind_today, ind, "ind_trend_20"),
            "ind_trend_63": _ind_val(ind_today, ind, "ind_trend_63"),
            "ind_trend_252": _ind_val(ind_today, ind, "ind_trend_252"),
            **est_dict,
            "barrier_wide": bool(est_dict["wide"]),
            "barrier_state": str(est_dict["state"]),
            "barrier_reason": b_reason,
        })

    return pl.DataFrame(recs) if recs else pl.DataFrame()


def _build_v2_panel(bars: pl.DataFrame, members: pl.DataFrame) -> pl.DataFrame:
    """Vectorised strictly-trailing Sortino momentum + SMA + liquidity panel."""
    s = pl.col("symbol")
    close = pl.col("close")

    has_turnover = "turnover" in bars.columns
    has_trades = "n_trades" in bars.columns
    has_series = "series" in bars.columns

    mem_cols = [c for c in ("symbol", "industry", "company_name") if c in members.columns]
    mem_dedup = members.select(mem_cols).unique(subset=["symbol"]) if mem_cols else pl.DataFrame({"symbol": pl.Series([], dtype=pl.Utf8)})
    if "company_name" not in mem_dedup.columns:
        mem_dedup = mem_dedup.with_columns(pl.col("symbol").alias("company_name"))
    if "industry" not in mem_dedup.columns:
        mem_dedup = mem_dedup.with_columns(pl.lit("Unclassified").alias("industry"))

    df = bars.sort(["symbol", "d"])
    if not has_turnover:
        df = df.with_columns((pl.col("volume").cast(pl.Float64) * close).alias("turnover"))
    if not has_trades:
        df = df.with_columns(pl.lit(10_000).alias("n_trades"))
    if not has_series:
        df = df.with_columns(pl.lit("EQ").alias("series"))

    return (
        df.join(mem_dedup, on="symbol", how="left")
        .with_columns(
            pl.col("industry").fill_null("Unclassified"),
            pl.col("company_name").fill_null(pl.col("symbol")),
        )
        .sort(["symbol", "d"])
        .with_columns(
            (close / close.shift(1).over(s) - 1.0).alias("ret_1d"),
            (close / close.shift(21).over(s) - 1.0).alias("ret_21d"),
            ((close / close.shift(1).over(s)).log().clip(upper_bound=0.0) ** 2).alias("_neg_r2"),
            ((close / close.shift(1).over(s)).log()).alias("_log_r"),
            pl.int_range(pl.len()).over(s).alias("bars_seen"),
            pl.col("turnover").rolling_mean(20).over(s).alias("adtv_20"),
            pl.col("turnover").rolling_mean(126).over(s).alias("adtv_126"),
            close.rolling_mean(50).over(s).alias("sma50"),
            close.rolling_mean(200).over(s).alias("sma200"),
            close.rolling_max(252).over(s).alias("_hi52"),
            (close.shift(21).over(s) / close.shift(252).over(s) - 1.0).alias("mom_12_1"),
            (close / close.shift(126).over(s) - 1.0).alias("ret_126d"),
        )
        .with_columns(
            (pl.col("_neg_r2").rolling_mean(252).over(s).sqrt() * (252.0 ** 0.5)).alias("dvol_252"),
            (pl.col("_log_r").rolling_std(21).over(s) * (252.0 ** 0.5)).alias("realised_vol_21d"),
            (close / pl.col("_hi52") - 1.0).alias("dist_52w_high"),
        )
        .drop(["_neg_r2", "_log_r", "_hi52"])
    )


@dataclass(slots=True)
class _TacticalCandidate:
    variant: str
    symbol: str
    sym_idx: int
    industry: str
    score: float
    close: float
    adtv20: float
    horizon: Horizon
    sleeve: Sleeve
    reasons: tuple[str, ...]


@dataclass(slots=True)
class _EnsembleStore:
    dates: list[date]
    date_to_idx: dict[date, int]
    symbols: list[str]
    sym_to_idx: dict[str, int]
    industries: list[str]
    bars_by_date: dict[date, dict[str, BarData]]
    macro_map: dict[date, dict[str, Any]]
    scored_by_rebal: dict[date, pl.DataFrame]
    close_m: np.ndarray
    atr14_m: np.ndarray
    natr14_m: np.ndarray
    adtv20_m: np.ndarray
    trades_m: np.ndarray
    listed_m: np.ndarray
    ret1d_m: np.ndarray
    close_in_range_m: np.ndarray
    overnight_spread_21_m: np.ndarray
    up_dn_vol_20_m: np.ndarray
    amihud_21_m: np.ndarray
    sma50_above_m: np.ndarray
    sma200_above_m: np.ndarray
    t_drift_63_m: np.ndarray
    er_63_m: np.ndarray
    beta63_m: np.ndarray
    res_ret_m: np.ndarray
    res_mom_63_m: np.ndarray
    ou_s_m: np.ndarray
    ou_tau_m: np.ndarray
    sym_barrier_edge_m: np.ndarray
    tercile_barriers_by_date: dict[date, dict[int, tuple[float, float, float, bool]]]
    tercile_cuts_by_date: dict[date, tuple[float, float]]


def _z_arr(vals: np.ndarray) -> np.ndarray:
    if len(vals) <= 1:
        return np.zeros_like(vals)
    mu = float(np.nanmean(vals))
    sd = float(np.nanstd(vals))
    if not math.isfinite(sd) or sd < 1e-9:
        return np.zeros_like(vals)
    return np.clip((np.nan_to_num(vals, nan=mu) - mu) / sd, -3.5, 3.5)


def _pit_pair_corr_60d(res_ret_m: np.ndarray, ti: int, si_a: int, si_b: int, lookback: int = 60) -> float:
    if ti < lookback:
        return 0.0
    ra = res_ret_m[ti - lookback + 1 : ti + 1, si_a]
    rb = res_ret_m[ti - lookback + 1 : ti + 1, si_b]
    sa = float(np.std(ra))
    sb = float(np.std(rb))
    if sa < 1e-8 or sb < 1e-8:
        return 0.0
    return float(np.dot(ra - np.mean(ra), rb - np.mean(rb)) / (len(ra) * sa * sb))


def _lot_open_risk_rupees(lot: Any, cur_price: float) -> float:
    ref_p = min(float(lot.entry_price), float(cur_price))
    return max(0.0, ref_p - float(lot.stop)) * int(lot.qty)


def _build_ensemble_store(
    bars: pl.DataFrame,
    members: pl.DataFrame,
    *,
    w: ChallengerWeights,
    cfg: RiskConfig,
    rebal_dates: set[date],
    use_pit_liquidity: bool,
    index_bars: pl.DataFrame | None,
    vix_bars: pl.DataFrame | None,
) -> _EnsembleStore | None:
    """Build strictly trailing PIT numpy factor/residual/barrier matrices for MetaVoter."""
    from .pipeline import build_panel

    bars_eq = bars.filter(pl.col("series") == "EQ") if "series" in bars.columns else bars
    mem_syms = set(members["symbol"].to_list()) if not members.is_empty() else set()
    bars_turnover = (
        bars_eq
        if "turnover" in bars_eq.columns
        else bars_eq.with_columns((pl.col("volume").cast(pl.Float64) * pl.col("close")).alias("turnover"))
    )
    bars_liq_syms = (
        bars_turnover.group_by("symbol")
        .agg(pl.col("turnover").max().alias("_max_t"))
        .filter((pl.col("_max_t") >= 3e7) | pl.col("symbol").is_in(sorted(mem_syms)))["symbol"]
        .to_list()
    )
    if not bars_liq_syms:
        return None
    bars_sub = bars_eq.filter(pl.col("symbol").is_in(bars_liq_syms)).sort(["symbol", "d"])
    s_col = pl.col("symbol")
    c_col = pl.col("close")
    panel = (
        build_panel(bars_sub, members)
        .sort(["symbol", "d"])
        .with_columns(
            pl.int_range(pl.len()).over(s_col).alias("bars_seen"),
            pl.col("turnover").rolling_mean(126).over(s_col).alias("adtv_126"),
            (c_col.shift(21).over(s_col) / c_col.shift(252).over(s_col) - 1.0).alias("mom_12_1"),
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
    panel_ind = panel.filter(pl.col("industry").is_not_null() & (pl.col("industry") != "Unclassified"))
    if panel_ind.is_empty():
        panel_ind = panel

    elig_syms = (
        panel_ind.group_by("symbol")
        .agg(
            pl.col("adtv_20").max().alias("max_adtv"),
            pl.col("industry").drop_nulls().last().alias("industry"),
        )
        .filter(pl.col("max_adtv") >= 3e7)
        .sort("symbol")
    )
    if elig_syms.is_empty():
        return None

    dates = sorted(bars_eq["d"].unique().to_list())
    date_to_idx = {d: i for i, d in enumerate(dates)}
    symbols = elig_syms["symbol"].to_list()
    industries = [str(x or "Unclassified") for x in elig_syms["industry"].to_list()]
    sym_to_idx = {s: i for i, s in enumerate(symbols)}
    T, S = len(dates), len(symbols)

    # Causal macro regime map via ta_quant.regime.classify_macro_regime_panel
    breadth_df = (
        panel.filter(pl.col("sma50").is_not_null() & pl.col("ret_21d").is_not_null())
        .group_by("d")
        .agg((pl.col("close") > pl.col("sma50")).mean().alias("breadth_50"))
    )
    breadth_map = {r["d"]: float(r["breadth_50"] or 0.5) for r in breadth_df.iter_rows(named=True)}

    if index_bars is not None and not index_bars.is_empty():
        idx_src = index_bars
    else:
        idx_src = (
            bars_sub.group_by("d")
            .agg(
                pl.col("open").mean().alias("open"),
                pl.col("high").mean().alias("high"),
                pl.col("low").mean().alias("low"),
                pl.col("close").mean().alias("close"),
            )
            .with_columns(pl.lit("Nifty 500").alias("index_name"))
            .sort("d")
        )
    mdf = classify_macro_regime_panel(idx_src, vix_bars, breadth_by_date=breadth_map)
    macro_map: dict[date, dict[str, Any]] = {}
    if not mdf.is_empty():
        m_rows = mdf.sort("d").to_dicts()
        b_hist: list[float] = []
        for idx_i, r in enumerate(m_rows):
            d_i = r["d"]
            c_i = float(r["close"] or 1.0)
            b50 = float(r["breadth_above_sma50"] or 0.5)
            b_hist.append(b50)
            b_turn = b50 - min(b_hist[max(0, idx_i - 5) : idx_i + 1])
            c_prev1 = float(m_rows[idx_i - 1]["close"] or c_i) if idx_i >= 1 else c_i
            c_prev5 = float(m_rows[idx_i - 5]["close"] or c_i) if idx_i >= 5 else c_i
            c_prev20 = float(m_rows[idx_i - 20]["close"] or c_i) if idx_i >= 20 else c_i
            sma20_c = sum(float(m_rows[j]["close"] or c_i) for j in range(max(0, idx_i - 19), idx_i + 1)) / min(20, idx_i + 1)
            v_i = float(r["vix"] or 15.0)
            v_prev5 = float(m_rows[idx_i - 5]["vix"] or v_i) if idx_i >= 5 else v_i
            idx_ret1d = (c_i / c_prev1 - 1.0) if c_prev1 > 0 else 0.0
            idx_ret5d = (c_i / c_prev5 - 1.0) if c_prev5 > 0 else 0.0
            idx_ret20d = (c_i / c_prev20 - 1.0) if c_prev20 > 0 else 0.0
            vix_chg5d = (v_i / v_prev5 - 1.0) if v_prev5 > 0 else 0.0
            macro_map[d_i] = {
                "macro_regime": str(r["macro_regime"]),
                "vix_pct": float(r["vix_pct252"] or 50.0),
                "breadth_50": b50,
                "breadth_turn": b_turn,
                "idx_ret_1d": idx_ret1d,
                "idx_ret_20d": idx_ret20d,
                "idx_log_ret": math.log(max(c_i / max(c_prev1, 1e-6), 1e-6)),
                "macro_recovery": (c_i > sma20_c) and (idx_ret5d > 0.012) and (b_turn >= 0.025) and (vix_chg5d < -0.03),
            }

    def _empty(fill: float = np.nan) -> np.ndarray:
        return np.full((T, S), fill, dtype=np.float64)

    close_m = _empty()
    atr14_m = _empty()
    natr14_m = _empty()
    adtv20_m = _empty(0.0)
    trades_m = _empty(0.0)
    listed_m = _empty(0.0)
    ret1d_m = _empty(0.0)
    close_in_range_m = _empty()
    overnight_spread_21_m = _empty()
    up_dn_vol_20_m = _empty()
    amihud_21_m = _empty()
    sma50_above_m = _empty(0.0)
    sma200_above_m = _empty(0.0)
    t_drift_63_m = _empty(0.0)
    er_63_m = _empty(0.0)

    bars_by_date: dict[date, dict[str, BarData]] = {}
    for r in bars_eq.select(["symbol", "d", "open", "high", "low", "close"]).iter_rows(named=True):
        rd = r["d"]
        bars_by_date.setdefault(rd, {})[str(r["symbol"])] = BarData(
            symbol=str(r["symbol"]), d=rd,
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]), close=float(r["close"]),
        )

    cols_needed = [
        "symbol", "d", "close", "atr14", "adtv_20", "n_trades", "bars_seen", "ret_1d",
        "close_in_range", "overnight_ret_21d", "intraday_ret_21d", "up_down_vol_ratio_20",
        "amihud_21d", "sma50", "sma200", "t_drift_63", "er_63",
    ]
    for r in panel_ind.filter(pl.col("symbol").is_in(symbols)).select(cols_needed).iter_rows(named=True):
        ti = date_to_idx.get(r["d"])
        si = sym_to_idx.get(r["symbol"])
        if ti is None or si is None:
            continue
        cl = float(r["close"])
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
        if r["close_in_range"] is not None:
            close_in_range_m[ti, si] = float(r["close_in_range"])
        ov, intr = r["overnight_ret_21d"], r["intraday_ret_21d"]
        if ov is not None and intr is not None:
            overnight_spread_21_m[ti, si] = float(ov) - float(intr)
        if r["up_down_vol_ratio_20"] is not None:
            up_dn_vol_20_m[ti, si] = float(r["up_down_vol_ratio_20"])
        if r["amihud_21d"] is not None:
            amihud_21_m[ti, si] = float(r["amihud_21d"])
        s50, s200 = r["sma50"], r["sma200"]
        sma50_above_m[ti, si] = 1.0 if (s50 is not None and cl > float(s50)) else 0.0
        sma200_above_m[ti, si] = 1.0 if (s200 is not None and cl > float(s200)) else 0.0
        if r["t_drift_63"] is not None:
            t_drift_63_m[ti, si] = float(r["t_drift_63"])
        if r["er_63"] is not None:
            er_63_m[ti, si] = float(r["er_63"])

    mkt_ret = np.array(
        [math.exp(macro_map[d]["idx_log_ret"]) - 1.0 if d in macro_map else 0.0 for d in dates],
        dtype=np.float64,
    )
    beta63_m = _empty(1.0)
    ret_clean = np.nan_to_num(ret1d_m, nan=0.0)
    mkt_col = mkt_ret[:, None]
    for t in range(63, T):
        rw = ret_clean[t - 62 : t + 1]
        mw = mkt_col[t - 62 : t + 1]
        mw_var = float(np.var(mw))
        if mw_var > 1e-10:
            cov = np.mean((rw - np.mean(rw, axis=0)) * (mw - float(np.mean(mw))), axis=0)
            beta63_m[t] = np.clip(cov / mw_var, 0.05, 2.0)

    mkt_stripped = ret_clean - beta63_m * mkt_col
    ind_to_cols: dict[str, list[int]] = {}
    for si, ind in enumerate(industries):
        ind_to_cols.setdefault(ind, []).append(si)
    res_ret_m = mkt_stripped.copy()
    valid_mask = ~np.isnan(close_m)
    for cols in ind_to_cols.values():
        if len(cols) >= 2:
            sub = mkt_stripped[:, cols]
            vm = valid_mask[:, cols]
            cnt = np.maximum(np.sum(vm, axis=1, keepdims=True), 1.0)
            ind_mean = np.sum(np.where(vm, sub, 0.0), axis=1, keepdims=True) / cnt
            res_ret_m[:, cols] = np.where(vm, sub - ind_mean, 0.0)

    res_mom_63_m = _empty(0.0)
    for t in range(63, T):
        w63 = res_ret_m[t - 62 : t + 1]
        dn63 = np.sqrt(np.mean(np.minimum(w63, 0.0) ** 2, axis=0)) + 0.002
        res_mom_63_m[t] = (np.mean(w63, axis=0) * 63.0) / (dn63 * math.sqrt(63.0))

    cum_res = np.cumsum(res_ret_m, axis=0)
    ou_s_m = _empty(0.0)
    ou_tau_m = _empty(99.0)
    for t in range(70, T):
        x_win = cum_res[t - 59 : t + 1] - cum_res[t - 60 : t - 59]
        x0, x1 = x_win[:-1], x_win[1:]
        m0, m1 = np.mean(x0, axis=0), np.mean(x1, axis=0)
        d0, d1 = x0 - m0, x1 - m1
        var0 = np.sum(d0 * d0, axis=0)
        b = np.where(var0 > 1e-10, np.sum(d0 * d1, axis=0) / np.maximum(var0, 1e-10), 1.0)
        a_coef = m1 - b * m0
        sig_eta = np.std(x1 - (a_coef + b * x0), axis=0, ddof=1)
        stationary = (b > 0.05) & (b < 0.94) & (sig_eta > 1e-6)
        b_safe = np.where(stationary, b, 0.5)
        mu = a_coef / (1.0 - b_safe)
        sig_eq = sig_eta / np.sqrt(np.maximum(1.0 - b_safe * b_safe, 1e-6))
        tau = math.log(2.0) / np.maximum(-np.log(b_safe), 1e-4)
        s_val = (x_win[-1] - mu) / np.maximum(sig_eq, 1e-6)
        ou_tau_m[t] = np.where(stationary, tau, 99.0)
        ou_s_m[t] = np.where(stationary, s_val, 0.0)

    # Strictly PIT BarrierModel posteriors (resolved exit_d <= d)
    path_lib = build_path_library(
        panel_ind.filter(pl.col("symbol").is_in(symbols)).select(["symbol", "d", "close", "high", "low", "atr14"]),
        horizon=Horizon.SWING, k_up=1.5, k_dn=1.5,
    )
    lib_by_exit = {d: df for (d,), df in path_lib.partition_by("exit_d", as_dict=True).items()}
    tercile_barriers_by_date: dict[date, dict[int, tuple[float, float, float, bool]]] = {}
    tercile_cuts_by_date: dict[date, tuple[float, float]] = {}
    sym_barrier_edge_m = _empty(0.0)
    exit_window: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
    sym_up_cnt = np.zeros(S, dtype=np.float64)
    sym_dn_cnt = np.zeros(S, dtype=np.float64)
    sym_tot_w = np.zeros(S, dtype=np.float64)
    w_each = 1.0 / 20.0

    for ti, d in enumerate(dates):
        part = lib_by_exit.get(d)
        if part is not None and not part.is_empty():
            syms_p = [sym_to_idx.get(s, -1) for s in part["symbol"].to_list()]
            valid_idx = [i for i, sidx in enumerate(syms_p) if sidx >= 0]
            if valid_idx:
                s_arr = np.array([syms_p[i] for i in valid_idx], dtype=np.int32)
                outs = part["outcome"].to_list()
                o_arr = np.array([1 if outs[i] == "UP" else (-1 if outs[i] == "DOWN" else 0) for i in valid_idx], dtype=np.int8)
                ap_arr = np.nan_to_num(part["atr_pct"].to_numpy()[valid_idx].astype(np.float64), nan=0.02)
                va_arr = np.nan_to_num(part["v_atr"].to_numpy()[valid_idx].astype(np.float64), nan=0.0)
                np.add.at(sym_up_cnt, s_arr[o_arr == 1], w_each)
                np.add.at(sym_dn_cnt, s_arr[o_arr == -1], w_each)
                np.add.at(sym_tot_w, s_arr, w_each)
                exit_window.append((s_arr, o_arr, ap_arr, va_arr))
        while len(exit_window) > 252:
            s_old, o_old, _, _ = exit_window.pop(0)
            np.add.at(sym_up_cnt, s_old[o_old == 1], -w_each)
            np.add.at(sym_dn_cnt, s_old[o_old == -1], -w_each)
            np.add.at(sym_tot_w, s_old, -w_each)
        if exit_window and (d in rebal_dates or ti % 4 == 0):
            all_o = np.concatenate([x[1] for x in exit_window])
            all_ap = np.concatenate([x[2] for x in exit_window])
            all_va = np.concatenate([x[3] for x in exit_window])
            q1, q2 = float(np.quantile(all_ap, 1.0 / 3.0)), float(np.quantile(all_ap, 2.0 / 3.0))
            tercile_cuts_by_date[d] = (q1, q2)
            p_up_g = float(np.sum(all_o == 1) + 1.0) / float(len(all_o) + 3.0)
            p_dn_g = float(np.sum(all_o == -1) + 1.0) / float(len(all_o) + 3.0)
            tmap: dict[int, tuple[float, float, float, bool]] = {}
            for tc in (0, 1, 2):
                mask = (all_ap <= q1) if tc == 0 else ((all_ap > q1) & (all_ap <= q2) if tc == 1 else (all_ap > q2))
                denom = float(np.sum(mask)) * w_each + 30.0
                pu = (float(np.sum(mask & (all_o == 1))) * w_each + 30.0 * p_up_g) / max(denom, 1.0)
                pd = (float(np.sum(mask & (all_o == -1))) * w_each + 30.0 * p_dn_g) / max(denom, 1.0)
                tmap[tc] = (pu, pd, float(np.mean(all_va[mask])) if np.any(mask) else 0.0, float(np.sum(mask)) * w_each >= 50.0)
            tercile_barriers_by_date[d] = tmap
            sym_pu = (sym_up_cnt + 6.0 * p_up_g) / np.maximum(sym_tot_w + 6.0, 1.0)
            sym_pd = (sym_dn_cnt + 6.0 * p_dn_g) / np.maximum(sym_tot_w + 6.0, 1.0)
            raw_edge = sym_pu - sym_pd
            sym_barrier_edge_m[ti] = raw_edge - float(np.nanmedian(raw_edge))

    # Pre-score monthly rebalance panels matching exact score_universe_v2
    v2_panel = _build_v2_panel(bars_sub, members)
    scored_by_rebal: dict[date, pl.DataFrame] = {}
    rebal_parts = v2_panel.filter(pl.col("d").is_in(sorted(rebal_dates))).partition_by("d", as_dict=True)
    for rd in sorted(rebal_dates):
        dp = rebal_parts.get((rd,))
        if dp is not None and not dp.is_empty():
            sc = score_universe_v2(dp, cfg=cfg, weights=w, use_pit_liquidity=use_pit_liquidity)
            if not sc.is_empty() and "rank" in sc.columns:
                scored_by_rebal[rd] = sc

    return _EnsembleStore(
        dates=dates, date_to_idx=date_to_idx, symbols=symbols, sym_to_idx=sym_to_idx,
        industries=industries, bars_by_date=bars_by_date, macro_map=macro_map,
        scored_by_rebal=scored_by_rebal, close_m=close_m, atr14_m=atr14_m, natr14_m=natr14_m,
        adtv20_m=adtv20_m, trades_m=trades_m, listed_m=listed_m, ret1d_m=ret1d_m,
        close_in_range_m=close_in_range_m, overnight_spread_21_m=overnight_spread_21_m,
        up_dn_vol_20_m=up_dn_vol_20_m, amihud_21_m=amihud_21_m,
        sma50_above_m=sma50_above_m, sma200_above_m=sma200_above_m,
        t_drift_63_m=t_drift_63_m, er_63_m=er_63_m, beta63_m=beta63_m,
        res_ret_m=res_ret_m, res_mom_63_m=res_mom_63_m, ou_s_m=ou_s_m, ou_tau_m=ou_tau_m,
        sym_barrier_edge_m=sym_barrier_edge_m,
        tercile_barriers_by_date=tercile_barriers_by_date,
        tercile_cuts_by_date=tercile_cuts_by_date,
    )


def _tactical_candidates(ds: _EnsembleStore, d: date, variant: str, cfg: RiskConfig) -> list[_TacticalCandidate]:
    """Generate causal tactical candidates for orthogonal variants V3, V5, V4, V2."""
    ti = ds.date_to_idx[d]
    macro = ds.macro_map.get(d, {"macro_regime": MacroRegime.SIDEWAYS_CHOP.value, "vix_pct": 50.0, "breadth_50": 0.5, "breadth_turn": 0.0, "idx_ret_20d": 0.0, "idx_ret_1d": 0.0, "macro_recovery": False})
    reg = macro["macro_regime"]
    vix_pct = float(macro.get("vix_pct", 50.0))
    b50 = float(macro.get("breadth_50", 0.5))
    b_turn = float(macro.get("breadth_turn", 0.0))
    idx_ret20 = float(macro.get("idx_ret_20d", 0.0))
    idx_ret1d = float(macro.get("idx_ret_1d", 0.0))
    macro_rec = bool(macro.get("macro_recovery", False))

    elig = (
        ~np.isnan(ds.close_m[ti])
        & (ds.close_m[ti] >= cfg.min_price)
        & (ds.atr14_m[ti] > 0.0)
        & (ds.natr14_m[ti] <= 0.065)
        & (ds.adtv20_m[ti] >= cfg.min_adtv_rupees)
        & (ds.trades_m[ti] >= cfg.min_trades_per_day)
        & (ds.listed_m[ti] >= cfg.min_listed_sessions)
    )
    if not np.any(elig):
        return []

    if variant == "V3":
        if reg != MacroRegime.SIDEWAYS_CHOP.value or b50 < 0.42 or (ti % 3 != 0):
            return []
        valid_am = elig & ~np.isnan(ds.amihud_21_m[ti])
        am_med = float(np.nanmedian(ds.amihud_21_m[ti, elig])) if np.any(valid_am) else 1e9
        mask = elig & (ds.sma200_above_m[ti] > 0.5) & (ds.ou_tau_m[ti] >= 2.0) & (ds.ou_tau_m[ti] <= 14.0) & (ds.ou_s_m[ti] <= -1.25) & (ds.er_63_m[ti] >= 0.20) & (ds.sym_barrier_edge_m[ti] >= -0.01) & (ds.close_in_range_m[ti] >= 0.50) & (ds.ret1d_m[ti] > -0.04) & (np.nan_to_num(ds.amihud_21_m[ti], nan=am_med) <= am_med * 1.40)
        idxs = np.where(mask)[0]
        if not len(idxs):
            return []
        raw = 0.50 * _z_arr(-ds.ou_s_m[ti, idxs]) + 0.30 * _z_arr(ds.sym_barrier_edge_m[ti, idxs]) + 0.20 * _z_arr(ds.er_63_m[ti, idxs])
        limit, hor, slv, rsn = 18, Horizon.SWING, Sleeve.MEAN_REVERSION, ("V3_OU_EQUILIBRIUM", "V3_BARRIER_FILTER")
    elif variant == "V5":
        ok_shock = reg == MacroRegime.BEAR_HIGH_VOL_SHOCK.value and (macro_rec or (vix_pct >= 75.0 and idx_ret1d > 0.005 and b_turn >= 0.025))
        ok_wash = reg != MacroRegime.BEAR_HIGH_VOL_SHOCK.value and (b50 < 0.44 and b_turn >= 0.015)
        if not (ok_shock or ok_wash):
            return []
        mask = elig & (ds.overnight_spread_21_m[ti] > 0.0) & (ds.er_63_m[ti] >= 0.22) & (ds.ou_s_m[ti] <= -1.10) & (ds.close_in_range_m[ti] >= 0.58) & (ds.ret1d_m[ti] > 0.0)
        idxs = np.where(mask)[0]
        if not len(idxs):
            return []
        raw = 0.40 * _z_arr(ds.overnight_spread_21_m[ti, idxs]) + 0.35 * _z_arr(ds.er_63_m[ti, idxs]) + 0.25 * _z_arr(-ds.ou_s_m[ti, idxs])
        limit, hor, slv, rsn = 15, Horizon.SWING, Sleeve.PULLBACK_IN_UPTREND, ("V5_CRISIS_SHIELD", "V5_OVERNIGHT_INFLECTION")
    elif variant == "V4":
        trend_ok = reg in (MacroRegime.LOW_VOL_TREND.value, MacroRegime.HIGH_VOL_TREND.value) or (reg == MacroRegime.SIDEWAYS_CHOP.value and b50 >= 0.62 and idx_ret20 > 0.02)
        tmap, cuts = ds.tercile_barriers_by_date.get(d), ds.tercile_cuts_by_date.get(d)
        if not trend_ok or not tmap or not cuts:
            return []
        q1, q2 = cuts
        mask = elig & (ds.sma200_above_m[ti] > 0.5) & (ds.sma50_above_m[ti] > 0.5) & (ds.sym_barrier_edge_m[ti] > 0.025) & (ds.up_dn_vol_20_m[ti] >= 1.15) & (ds.er_63_m[ti] >= 0.28) & (ds.close_in_range_m[ti] >= 0.55)
        idxs = np.array([int(si) for si in np.where(mask)[0] if (t := tmap[0 if ds.natr14_m[ti, si] <= q1 else (1 if ds.natr14_m[ti, si] <= q2 else 2)])[3] and t[2] >= -0.05], dtype=np.int32)
        if not len(idxs):
            return []
        raw = 0.50 * _z_arr(ds.sym_barrier_edge_m[ti, idxs]) + 0.30 * _z_arr(ds.up_dn_vol_20_m[ti, idxs]) + 0.20 * _z_arr(ds.er_63_m[ti, idxs])
        limit, hor, slv, rsn = 25, Horizon.POSITIONAL, Sleeve.SECTOR_ROTATION, ("V4_DIRICHLET_BARRIER", "V4_ACCUMULATION_THRUST")
    else:  # V2
        if reg == MacroRegime.BEAR_HIGH_VOL_SHOCK.value or b50 < 0.42:
            return []
        beta_cap = 0.85 if reg == MacroRegime.HIGH_VOL_TREND.value else 0.90
        mask = elig & (ds.sma200_above_m[ti] > 0.5) & (ds.beta63_m[ti] <= beta_cap) & (ds.res_mom_63_m[ti] > 0.25) & (ds.er_63_m[ti] >= 0.30) & (ds.t_drift_63_m[ti] >= 1.5)
        idxs = np.where(mask)[0]
        if not len(idxs):
            return []
        raw = 0.45 * _z_arr(ds.res_mom_63_m[ti, idxs]) + 0.35 * _z_arr(ds.er_63_m[ti, idxs]) + 0.20 * _z_arr(ds.t_drift_63_m[ti, idxs])
        limit, hor, slv, rsn = 25, Horizon.POSITIONAL, Sleeve.MOMENTUM_BREAKOUT, ("V2_RESIDUAL_HAC", "V2_LOW_BETA_ALPHA")

    out: list[_TacticalCandidate] = []
    for pos in np.argsort(-raw)[:limit]:
        si = int(idxs[pos])
        out.append(_TacticalCandidate(
            variant=variant, symbol=ds.symbols[si], sym_idx=si, industry=ds.industries[si],
            score=float(2.0 + raw[pos]), close=float(ds.close_m[ti, si]), adtv20=float(ds.adtv20_m[ti, si]),
            horizon=hor, sleeve=slv, reasons=rsn,
        ))
    return out


def _run_metavoter_4tier_backtest(
    ds: _EnsembleStore,
    active_sessions: list[date],
    *,
    initial_equity: float,
    arm_obj: Arm,
    cfg: RiskConfig,
    w: ChallengerWeights,
    n_trials: int,
    scored_index: str,
    entry_block: EntryBlock | None,
    index_regime: pl.DataFrame | None,
) -> BacktestResult:
    """Execute the proven 5-variant MetaVoter + 4-Tier Hierarchical Risk Engine."""
    from .backtest.engine import BacktestResult

    start_d, end_d = active_sessions[0], active_sessions[-1]
    broker = PaperBroker(initial_equity=initial_equity, arm=arm_obj)
    run_id = f"BT-V2-{arm_obj.value}"
    broker.record_manifest(
        RunManifest(
            run_id=run_id, d=start_d, as_of=datetime.combine(start_d, time(15, 30), tzinfo=UTC),
            arm=arm_obj.value, git_sha="BACKTEST", uv_lock_sha="BACKTEST",
            weights_sha=w.sha256, config_sha="BACKTEST",
        )
    )

    max_slots = w.tiered_max_positions
    max_per_ind = w.tiered_max_per_industry
    max_sec_risk_pct = TIERED_DEFAULT.max_industry_heat_pct
    max_port_heat_entry = w.tiered_max_portfolio_heat_pct
    max_v1_risk_pct = 0.0098
    max_tac_risk_pct = 0.0075
    min_v1_notional = TIERED_DEFAULT.min_notional
    min_tac_notional = 20_000.0
    bear_tr = w.bear_shock_winner_trail_pct
    v24_tr = 0.17

    lot_var: dict[str, str] = {}
    session_regimes: dict[date, str] = {}
    monthly_rebal_set = monthly_rebalance_dates(ds.dates)
    first_day = True
    seq = 0

    for d in active_sessions:
        ti = ds.date_to_idx[d]
        bmap = ds.bars_by_date.get(d, {})
        session_ts = datetime.combine(d, time(15, 30), tzinfo=UTC)
        broker.step_session(d, bmap, run_id=run_id, ts=session_ts)

        macro = ds.macro_map.get(d, {"macro_regime": MacroRegime.SIDEWAYS_CHOP.value, "breadth_50": 0.5})
        reg = macro["macro_regime"]
        b50 = float(macro.get("breadth_50", 0.5))
        b_reg = (
            Regime.TREND_DOWN if reg == MacroRegime.BEAR_HIGH_VOL_SHOCK.value
            else (Regime.HIGH_VOL if reg == MacroRegime.HIGH_VOL_TREND.value
                  else (Regime.CHOP if reg == MacroRegime.SIDEWAYS_CHOP.value else Regime.TREND_UP))
        )
        session_regimes[d] = b_reg.value

        # Daily trailing stop update (variant-aware + Bear Shock adaptive tightening)
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
                        broker.trail_stop(oid, cs, run_id=run_id, ts=session_ts)
                if vn == "V3" and si >= 0 and lot.sessions_held >= 4 and float(ds.ou_s_m[ti, si]) >= 0.45:
                    if oid not in broker.pending_exits:
                        broker.schedule_exit(oid, reason="OU_EXIT")
            else:
                eff_trail = bear_tr if (reg == MacroRegime.BEAR_HIGH_VOL_SHOCK.value and b50 < 0.35) else v24_tr
                if lot.max_high_seen > lot.entry_price * 1.08:
                    cs = round(lot.max_high_seen * (1.0 - eff_trail), 2)
                    if cs > lot.stop and cs < lp * 0.998:
                        broker.trail_stop(oid, cs, run_id=run_id, ts=session_ts)

        # Tier-1 Hard Principal Heat Ratchet (<= 7.0% hard ceiling)
        eq = broker.cash + sum((bmap[l.symbol].close if l.symbol in bmap else l.last_price) * l.qty for l in broker.open_lots.values())
        open_risk_rs = sum(_lot_open_risk_rupees(l, bmap[l.symbol].close if l.symbol in bmap else l.last_price) for l in broker.open_lots.values())
        if eq > 0 and open_risk_rs > 0.0694 * eq and broker.open_lots:
            scale = (0.0690 * eq) / open_risk_rs
            for oid, l in list(broker.open_lots.items()):
                cp = bmap[l.symbol].close if l.symbol in bmap else l.last_price
                ref_p = min(l.entry_price, cp)
                if ref_p > l.stop:
                    ns = round(ref_p - (ref_p - l.stop) * scale, 2)
                    if ns > l.stop and ns < cp * 0.995:
                        broker.trail_stop(oid, ns, run_id=run_id, ts=session_ts)

        is_rebal = (d in monthly_rebal_set) or first_day
        is_tactical = ti % 4 == 0
        if not (is_rebal or is_tactical):
            continue
        first_day = False

        idx_today = regime_on(index_regime, d)[0] if index_regime is not None else None
        blocked = entry_block is not None and entry_block.blocks(b_reg, idx_today)

        sc_df = ds.scored_by_rebal.get(d)
        rank_map = {r["symbol"]: int(r["rank"]) for r in sc_df.select(["symbol", "rank"]).iter_rows(named=True)} if (sc_df is not None and not sc_df.is_empty()) else {}

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
            if is_rebal and vn == "V1" and (rk > w.hold_rank_buffer or not s200_ok):
                broker.schedule_exit(oid, reason="RANK_BUFFER_EXIT" if rk > w.hold_rank_buffer else "SMA200_BREAK")
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

        if blocked or len(surviving_syms) >= max_slots:
            continue
        avail_cash = broker.cash + exiting_notional * 0.85 - eq * TIERED_DEFAULT.cash_floor_pct
        if avail_cash < min_tac_notional:
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
            scored_rows: list[tuple[float, dict[str, Any]]] = []
            for r in rows:
                si = ds.sym_to_idx.get(str(r["symbol"]), -1)
                base_c = float(r["composite"])
                if si >= 0:
                    rm = float(ds.res_mom_63_m[ti, si]) if not math.isnan(ds.res_mom_63_m[ti, si]) else 0.0
                    er = float(ds.er_63_m[ti, si]) if not math.isnan(ds.er_63_m[ti, si]) else 0.3
                    be = float(ds.sym_barrier_edge_m[ti, si]) if not math.isnan(ds.sym_barrier_edge_m[ti, si]) else 0.0
                    uv = float(ds.up_dn_vol_20_m[ti, si]) if not math.isnan(ds.up_dn_vol_20_m[ti, si]) else 1.0
                    scored_rows.append((base_c + compute_multivariant_bonus(rm, er, be, uv), r))
                else:
                    scored_rows.append((base_c, r))
            scored_rows.sort(key=lambda x: -x[0])

            target_slot_notional = max(35_000.0, (eq * 0.93) / 9.0)
            for _sc, row in scored_rows:
                if len(surviving_syms) >= max_slots or avail_cash < min_v1_notional:
                    break
                sym = str(row["symbol"])
                if sym in surviving_syms:
                    continue
                si = ds.sym_to_idx.get(sym, -1)
                ind = str(row.get("industry") or "Unclassified")
                s_cap_lim = cfg.finserv_max_pct if ind == cfg.finserv_industry else 0.25
                if ind_counts.get(ind, 0) >= max_per_ind or sec_cap.get(ind, 0.0) / eq >= s_cap_lim:
                    continue
                rem_sec = max_sec_risk_pct * eq - sec_risk.get(ind, 0.0)
                rem_heat = max_port_heat_entry * eq - cur_open_risk_rs
                if rem_sec < eq * 0.002 or rem_heat < eq * 0.003:
                    continue
                if si >= 0 and any(_pit_pair_corr_60d(ds.res_ret_m, ti, si, osi, 60) >= 0.65 for osi in open_sis):
                    continue
                price = float(row["close"])
                natr = float(ds.natr14_m[ti, si]) if (si >= 0 and not math.isnan(ds.natr14_m[ti, si])) else 0.025
                stp_i = min(0.120, max(0.062, 2.8 * natr))
                alloc = min(target_slot_notional, min(max_v1_risk_pct * eq, rem_heat, rem_sec) / stp_i, avail_cash / 1.02, float(row["adtv_20"]) * cfg.max_pct_of_adtv)
                qty = int(alloc // price)
                notional = qty * price
                if notional < min_v1_notional:
                    continue
                seq += 1
                oid = f"{d.isoformat()}-{sym}-{seq}"
                orders.append(Order(
                    order_id=oid, symbol=sym, industry=ind, placed_d=d,
                    entry_lo=round(price * (1.0 - w.entry_band_pct), 2),
                    entry_hi=round(price * (1.0 + w.entry_band_pct), 2),
                    stop=round(price * (1.0 - stp_i), 2),
                    target=round(price * w.target_multiple, 2),
                    qty=qty, horizon=Horizon.POSITIONAL, sleeve=Sleeve.MOMENTUM_BREAKOUT,
                    arm=arm_obj, reasons=("MV_ENSEMBLE", ReasonCode.MA_STACK_ALIGNED.value),
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
            for vk in ("V3", "V5", "V4", "V2"):
                for cand in _tactical_candidates(ds, d, vk, cfg)[:4]:
                    if len(surviving_syms) >= max_slots or avail_cash < min_tac_notional:
                        break
                    if cand.symbol in surviving_syms:
                        continue
                    ind = cand.industry
                    s_cap_lim = cfg.finserv_max_pct if ind == cfg.finserv_industry else 0.25
                    if ind_counts.get(ind, 0) >= max_per_ind or sec_cap.get(ind, 0.0) / eq >= s_cap_lim:
                        continue
                    rem_sec = max_sec_risk_pct * eq - sec_risk.get(ind, 0.0)
                    rem_heat = max_port_heat_entry * eq - cur_open_risk_rs
                    if rem_sec < eq * 0.002 or rem_heat < eq * 0.003:
                        continue
                    if any(_pit_pair_corr_60d(ds.res_ret_m, ti, cand.sym_idx, osi, 60) >= 0.65 for osi in open_sis):
                        continue
                    stop_p = 0.060 if vk in ("V3", "V5") else 0.075
                    alloc = min(eq * 0.095, min(max_tac_risk_pct * eq, rem_heat, rem_sec) / stop_p, avail_cash / 1.02, cand.adtv20 * cfg.max_pct_of_adtv)
                    qty = int(alloc // cand.close)
                    notional = qty * cand.close
                    if notional < min_tac_notional:
                        continue
                    seq += 1
                    oid = f"{d.isoformat()}-{cand.symbol}-{seq}"
                    orders.append(Order(
                        order_id=oid, symbol=cand.symbol, industry=ind, placed_d=d,
                        entry_lo=round(cand.close * 0.97, 2),
                        entry_hi=round(cand.close * 1.03, 2),
                        stop=round(cand.close * (1.0 - stop_p), 2),
                        target=round(cand.close * (1.35 if vk in ("V3", "V5") else 2.2), 2),
                        qty=qty, horizon=cand.horizon, sleeve=cand.sleeve, arm=arm_obj,
                        reasons=cand.reasons, max_sessions=(20 if vk in ("V3", "V5") else w.v24_max_sessions),
                    ))
                    lot_var[oid] = vk if vk in ("V3", "V5") else "V24"
                    surviving_syms.add(cand.symbol)
                    open_sis.append(cand.sym_idx)
                    ind_counts[ind] = ind_counts.get(ind, 0) + 1
                    sec_cap[ind] = sec_cap.get(ind, 0.0) + notional
                    cur_open_risk_rs += notional * stop_p
                    sec_risk[ind] = sec_risk.get(ind, 0.0) + notional * stop_p
                    avail_cash -= notional * 1.02

        if orders:
            broker.place_orders(orders, run_id=run_id, ts=session_ts)

    stats = compute_backtest_stats(
        broker.equity_curve, broker.outcomes, n_trials=n_trials, initial_equity=initial_equity,
    )
    return BacktestResult(
        arm=arm_obj, start_date=start_d, end_date=end_d, initial_equity=initial_equity,
        final_equity=broker.portfolio().equity, equity_curve=broker.equity_curve,
        outcomes=broker.outcomes, events=tuple(broker.events),
        stats=stats, regimes=session_regimes,
    )


def run_backtest_v2(
    bars: pl.DataFrame,
    members: pl.DataFrame,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    initial_equity: float = 500_000.0,
    arm: Arm = Arm.DETERMINISTIC,
    tradeable_index: str = "NIFTY500",
    scored_index: str = "NIFTY500",
    cfg: RiskConfig = DEFAULT,
    weights: ChallengerWeights | None = None,
    n_trials: int = 5,
    warmup_sessions: int = 40,
    index_regime: pl.DataFrame | None = None,
    entry_block: EntryBlock | None = None,
    use_pit_liquidity: bool = True,
    index_bars: pl.DataFrame | None = None,
    vix_bars: pl.DataFrame | None = None,
) -> BacktestResult:
    """Execute v2 MetaVoter + 4-Tier Risk Engine backtest via `PaperBroker`."""
    from .backtest.engine import BacktestResult
    from .pipeline import breadth_row, infer_regime

    all_sessions = sorted(bars["d"].unique().to_list())
    if not all_sessions:
        raise ValueError("bars DataFrame has no session dates")

    if warmup_sessions > 0 and len(all_sessions) > warmup_sessions:
        active_sessions = all_sessions[warmup_sessions:]
    else:
        active_sessions = list(all_sessions)

    if start_date is not None:
        active_sessions = [s for s in active_sessions if s >= start_date]
    if end_date is not None:
        active_sessions = [s for s in active_sessions if s <= end_date]

    if not active_sessions:
        raise ValueError("No active sessions to backtest after warmup and date filtering")

    w = weights or load_challenger_weights()
    start_d, end_d = active_sessions[0], active_sessions[-1]
    arm_obj = arm if isinstance(arm, Arm) else Arm(arm)

    if cfg.min_listed_sessions >= 150:
        rebal_needed = monthly_rebalance_dates(all_sessions) | {active_sessions[0]}
        ds = _build_ensemble_store(
            bars, members, w=w, cfg=cfg, rebal_dates=rebal_needed,
            use_pit_liquidity=use_pit_liquidity, index_bars=index_bars, vix_bars=vix_bars,
        )
        if ds is not None:
            return _run_metavoter_4tier_backtest(
                ds, active_sessions, initial_equity=initial_equity, arm_obj=arm_obj,
                cfg=cfg, w=w, n_trials=n_trials, scored_index=scored_index,
                entry_block=entry_block, index_regime=index_regime,
            )

    panel_full = _build_v2_panel(bars, members)
    trade_syms: set[str] = (
        set(members.filter(pl.col("index_name") == tradeable_index)["symbol"].to_list())
        if "index_name" in members.columns and not members.is_empty()
        else set()
    )

    if use_pit_liquidity and "adtv_126" in panel_full.columns:
        ranked_liq = panel_full.with_columns(
            pl.col("adtv_126").fill_null(0.0).rank(method="ordinal", descending=True).over("d").alias("_lr")
        )
        pit_univ = ranked_liq.filter(pl.col("_lr") <= w.pit_liquidity_top_n).drop("_lr")
        ever_syms = set(pit_univ["symbol"].unique().to_list()) | trade_syms
    else:
        pit_univ = panel_full.filter(pl.col("symbol").is_in(sorted(trade_syms))) if trade_syms else panel_full
        ever_syms = set(pit_univ["symbol"].unique().to_list())

    sub_full = panel_full.filter(pl.col("symbol").is_in(sorted(ever_syms))) if ever_syms else panel_full
    corr_idx = ResidualCorrelationIndex(sub_full)

    bars_by_date: dict[date, dict[str, BarData]] = {}
    sma200_by_date: dict[date, dict[str, float]] = {}
    for r in sub_full.select(["symbol", "d", "open", "high", "low", "close", "sma200"]).iter_rows(named=True):
        rd = r["d"]
        if rd not in bars_by_date:
            bars_by_date[rd] = {}
            sma200_by_date[rd] = {}
        bars_by_date[rd][r["symbol"]] = BarData(
            symbol=str(r["symbol"]),
            d=rd,
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
        )
        sma200_by_date[rd][r["symbol"]] = float(r["sma200"] or 0.0)

    day_panels = pit_univ.partition_by("d", as_dict=True, maintain_order=True)
    empty_day = pit_univ.head(0)
    rebal_dates = monthly_rebalance_dates(all_sessions)

    broker = PaperBroker(initial_equity=initial_equity, arm=arm_obj)
    run_id = f"BT-V2-{arm_obj.value}"
    broker.record_manifest(
        RunManifest(
            run_id=run_id,
            d=start_d,
            as_of=datetime.combine(start_d, time(15, 30), tzinfo=UTC),
            arm=arm_obj.value,
            git_sha="BACKTEST",
            uv_lock_sha="BACKTEST",
            weights_sha=w.sha256,
            config_sha="BACKTEST",
        )
    )

    session_regimes: dict[date, str] = {}
    first_day = True

    for d in active_sessions:
        session_ts = datetime.combine(d, time(15, 30), tzinfo=UTC)
        btoday = bars_by_date.get(d, {})
        stoday = sma200_by_date.get(d, {})

        # 1. Morning/Intraday fills, stops, scheduled exits & zombie-lot cleanup
        broker.step_session(d, btoday, run_id=run_id, ts=session_ts)

        # 2. Daily trailing stop update (activates after +8% gain, trails 18% below peak)
        for oid, lot in list(broker.open_lots.items()):
            if lot.max_high_seen > lot.entry_price * (1.0 + w.trail_activate_gain_pct):
                cand_stop = round(lot.max_high_seen * (1.0 - w.trail_drawdown_pct), 2)
                if cand_stop > lot.stop:
                    broker.trail_stop(oid, cand_stop, run_id=run_id, ts=session_ts)

        # 3. Record session breadth regime
        dp = day_panels.get((d,), empty_day)
        b_syms = set(dp["symbol"].to_list()) if not dp.is_empty() else set()
        b_reg, _ = infer_regime(breadth_row(dp, d, scored_index, b_syms)) if b_syms else (Regime.UNKNOWN, "")
        session_regimes[d] = b_reg.value

        # 4. Anchored monthly rebalance (plus initial active session)
        if not (d in rebal_dates or first_day):
            continue
        first_day = False

        if dp.is_empty():
            continue

        scored = score_universe_v2(
            dp,
            tradeable=(None if use_pit_liquidity else (trade_syms or None)),
            cfg=cfg,
            weights=w,
            use_pit_liquidity=False,  # dp is already PIT-liquidity filtered above
        )
        rank_map: dict[str, int] = (
            {r["symbol"]: int(r["rank"]) for r in scored.select(["symbol", "rank"]).iter_rows(named=True)}
            if not scored.is_empty()
            else {}
        )

        exiting_notional = 0.0
        surviving_syms: set[str] = set()
        ind_counts: dict[str, int] = {}
        for oid, lot in list(broker.open_lots.items()):
            s200 = stoday.get(lot.symbol, 0.0)
            cur_c = btoday[lot.symbol].close if lot.symbol in btoday else lot.last_price
            rk = rank_map.get(lot.symbol, 999_999)
            if rk > w.hold_rank_buffer or (s200 > 0 and cur_c < s200):
                broker.schedule_exit(
                    oid,
                    reason="RANK_BUFFER_EXIT" if rk > w.hold_rank_buffer else "SMA200_BREAK",
                )
                exiting_notional += lot.qty * lot.last_price
            else:
                surviving_syms.add(lot.symbol)
                ind_counts[lot.industry] = ind_counts.get(lot.industry, 0) + 1

        idx_today = regime_on(index_regime, d)[0] if index_regime is not None else None
        if entry_block is not None and entry_block.blocks(b_reg, idx_today):
            continue

        if scored.is_empty():
            continue

        pf = broker.portfolio()
        eff_max_pos = min(w.max_positions, max(1, int((pf.equity * 0.92) // 55_000.0)))
        slots_free = eff_max_pos - len(surviving_syms)
        if slots_free <= 0:
            continue

        avail_cash = pf.cash + exiting_notional * 0.85 - pf.equity * 0.08
        target_slot_notional = max(55_000.0, (pf.equity * 0.92) / eff_max_pos)
        if avail_cash < cfg.min_notional:
            continue

        if cfg.min_listed_sessions >= 150:
            buy_pool = scored.filter(
                (pl.col("close") > pl.col("sma200"))
                & (pl.col("sma50") > pl.col("sma200"))
                & (pl.col("close") > pl.col("sma50"))
                & (pl.col("mom_12_1") > w.min_mom_12_1)
                & (pl.col("ret_126d") > w.min_ret_126d)
            )
        else:
            buy_pool = scored

        wdates = corr_idx.window_dates_for(d)
        wset = set(wdates) if len(wdates) >= corr_idx.min_periods else None
        pending_syms = {o.symbol for o in broker.pending_orders.values()}

        orders: list[Order] = []
        for row in buy_pool.head(40).iter_rows(named=True):
            if len(orders) >= slots_free or avail_cash < cfg.min_notional:
                break
            sym = str(row["symbol"])
            if sym in surviving_syms or sym in pending_syms or any(lot.symbol == sym for lot in broker.open_lots.values()):
                continue
            ind = str(row.get("industry") or "Unclassified")
            max_ind = cfg.finserv_max_names if ind == cfg.finserv_industry else cfg.max_industry_names
            if ind_counts.get(ind, 0) >= max_ind:
                continue
            if wset and any(corr_idx.corr(d, sym, s_sym, _wset=wset) >= w.max_pair_corr for s_sym in surviving_syms):
                continue

            price = float(row["close"])
            alloc = min(target_slot_notional, avail_cash / 1.02, float(row["adtv_20"]) * cfg.max_pct_of_adtv)
            qty = int(alloc // price)
            notional = qty * price
            if notional < cfg.min_notional:
                continue

            orders.append(
                Order(
                    order_id=f"{d.isoformat()}-{sym}",
                    symbol=sym,
                    industry=ind,
                    placed_d=d,
                    entry_lo=round(price * (1.0 - w.entry_band_pct), 2),
                    entry_hi=round(price * (1.0 + w.entry_band_pct), 2),
                    stop=round(price * (1.0 - w.initial_stop_pct), 2),
                    target=round(price * w.target_multiple, 2),
                    qty=qty,
                    horizon=Horizon.POSITIONAL,
                    sleeve=Sleeve.MOMENTUM_BREAKOUT,
                    arm=arm_obj,
                    reasons=(ReasonCode.MULTI_HORIZON_MOMENTUM.value, ReasonCode.MA_STACK_ALIGNED.value),
                    max_sessions=252,
                )
            )
            surviving_syms.add(sym)
            ind_counts[ind] = ind_counts.get(ind, 0) + 1
            avail_cash -= notional * 1.02

        if orders:
            broker.place_orders(orders, run_id=run_id, ts=session_ts)

    stats = compute_backtest_stats(
        broker.equity_curve,
        broker.outcomes,
        n_trials=n_trials,
        initial_equity=initial_equity,
    )
    return BacktestResult(
        arm=arm_obj,
        start_date=start_d,
        end_date=end_d,
        initial_equity=initial_equity,
        final_equity=broker.portfolio().equity,
        equity_curve=broker.equity_curve,
        outcomes=broker.outcomes,
        events=tuple(broker.events),
        stats=stats,
        regimes=session_regimes,
    )
