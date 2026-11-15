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

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any

import polars as pl
from ta_core.costs import compute_costs
from ta_core.enums import Action, Arm, Horizon, ProductType, ReasonCode, Regime, Sleeve
from ta_ledger.broker.base import BarData, Order
from ta_ledger.broker.paper import PaperBroker
from ta_ledger.manifest import RunManifest
from ta_ledger.risk import DEFAULT, Portfolio, RiskConfig

from .barriers import BarrierModel
from .factors.correlation import ResidualCorrelationIndex
from .regime import EntryBlock, regime_on
from .stats import compute_backtest_stats
from .weights import ChallengerWeights, load_challenger_weights

if TYPE_CHECKING:
    from .backtest.engine import BacktestResult

__all__ = [
    "monthly_rebalance_dates",
    "run_backtest_v2",
    "score_universe_v2",
    "screen_session_v2",
]


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
    elif tradeable:
        df = df.filter(pl.col("symbol").is_in(sorted(tradeable)))

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

    # 1. Held positions -> SELL or HOLD (Invariant I-16)
    for pos in prior_positions:
        row = day_by_sym.get(pos.symbol, {})
        close = float(row.get("close") or pos.last)
        sma200 = float(row.get("sma200") or 0.0)
        atr = float(row.get("atr14") or (close * 0.02))
        rk = rank_map.get(pos.symbol, 999_999)

        hit_stop = close <= pos.stop
        hit_sma = sma200 > 0 and close < sma200
        hit_rank = is_rebalance_day and rk > w.hold_rank_buffer
        is_sell = hit_stop or hit_sma or hit_rank

        if is_sell:
            action = Action.SELL.value
            exiting_notional += pos.qty * close
            reasons = [ReasonCode.STOP_DISCIPLINE.value if hit_stop else ReasonCode.REGIME_RISK_OFF.value]
            note = (
                "STOP_DISCIPLINE: price breached stop"
                if hit_stop
                else ("TREND_REVERSAL: close broke below 200-DMA" if hit_sma else f"ROTATE: rank {rk} > {w.hold_rank_buffer}")
            )
            st = round(pos.stop, 2)
        else:
            action = Action.HOLD.value
            surviving_syms.add(pos.symbol)
            ind_counts[pos.industry] = ind_counts.get(pos.industry, 0) + 1
            reasons = [ReasonCode.MULTI_HORIZON_MOMENTUM.value, ReasonCode.MA_STACK_ALIGNED.value]
            note = f"HOLD: rank #{rk} <= {w.hold_rank_buffer}, above 200-DMA"
            if close > pos.entry * (1.0 + w.trail_activate_gain_pct):
                st = max(round(pos.stop, 2), round(close * (1.0 - w.trail_drawdown_pct), 2))
            else:
                st = round(pos.stop, 2)

        tgt = round(pos.target if pos.target > 0 else pos.entry * w.target_multiple, 2)
        e_lo = round(pos.entry * (1.0 - w.entry_band_pct), 2)
        e_hi = round(pos.entry * (1.0 + w.entry_band_pct), 2)
        notional = round(pos.qty * close, 2)

        recs.append({
            "rec_id": f"{run_id}-{pos.symbol}",
            "d": d,
            "symbol": pos.symbol,
            "name": str(row.get("company_name") or pos.symbol),
            "industry": pos.industry,
            "arm": Arm.DETERMINISTIC.value,
            "action": action,
            "sleeve": Sleeve.MOMENTUM_BREAKOUT.value,
            "horizon": Horizon.POSITIONAL.value,
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

    if scored.is_empty():
        return pl.DataFrame(recs) if recs else pl.DataFrame()

    eff_max_pos = min(w.max_positions, max(1, int((portfolio.equity * 0.92) // 55_000.0)))
    slots_free = max(0, eff_max_pos - len(surviving_syms))
    avail_cash = portfolio.cash + exiting_notional * 0.85 - portfolio.equity * 0.08
    target_slot_notional = max(55_000.0, (portfolio.equity * 0.92) / eff_max_pos)

    corr_wset: set[date] | None = None
    if corr_index is not None:
        wdates = corr_index.window_dates_for(d)
        if len(wdates) >= corr_index.min_periods:
            corr_wset = set(wdates)

    # 2. Unowned Stage-2 candidates -> BUY (on rebalance days with slots) or WATCH
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

        if cfg.min_listed_sessions >= 150:
            if not (close > sma200 and sma50 > sma200 and close > sma50 and mom12 > w.min_mom_12_1 and ret6m > w.min_ret_126d):
                continue

        max_ind = cfg.finserv_max_names if ind == cfg.finserv_industry else cfg.max_industry_names
        ind_ok = ind_counts.get(ind, 0) < max_ind
        corr_ok = True
        if corr_index is not None and corr_wset is not None and surviving_syms:
            corr_ok = all(
                corr_index.corr(d, sym, s_sym, _wset=corr_wset) < w.max_pair_corr
                for s_sym in surviving_syms
            )

        would_alloc = min(target_slot_notional, adtv_20 * cfg.max_pct_of_adtv)
        would_qty = int(would_alloc // close)
        would_notional = round(would_qty * close, 2)
        stop = round(close * (1.0 - w.initial_stop_pct), 2)
        target = round(close * w.target_multiple, 2)

        can_buy = (
            is_rebalance_day
            and not entries_blocked
            and buys_placed < slots_free
            and ind_ok
            and corr_ok
            and avail_cash >= cfg.min_notional
        )
        qty = 0
        notional = 0.0
        if can_buy:
            alloc = min(target_slot_notional, avail_cash / 1.02, adtv_20 * cfg.max_pct_of_adtv)
            cand_qty = int(alloc // close)
            cand_notional = round(cand_qty * close, 2)
            if cand_notional >= cfg.min_notional:
                action = Action.BUY.value
                qty = cand_qty
                notional = cand_notional
                buys_placed += 1
                surviving_syms.add(sym)
                ind_counts[ind] = ind_counts.get(ind, 0) + 1
                avail_cash -= cand_notional * 1.02
                afford_note = f"rebalance slot #{buys_placed} (rank #{row['rank']})"
            else:
                action = Action.WATCH.value
                afford_note = "RISK-01: below Rs 50,000 position floor"
        else:
            action = Action.WATCH.value
            if entries_blocked:
                afford_note = f"ENTRY-BLOCK: {block_note or 'new entries blocked by regime'}"
            elif not is_rebalance_day:
                afford_note = f"WATCH: awaiting monthly rebalance (rank #{row['rank']})"
            elif not ind_ok:
                afford_note = f"RISK-06: industry cap reached for {ind}"
            elif not corr_ok:
                afford_note = "RISK-08: pairwise residual correlation >= 0.60"
            else:
                afford_note = "RISK-05: portfolio slots full"

        est_dict = (
            bmodel.estimate(atr / close).as_dict()
            if bmodel is not None and close > 0
            else {
                "state": "UNAVAILABLE", "p_up": None, "p_dn": None, "p_vert": None,
                "ci_lo": None, "ci_hi": None, "n_eff": 0.0,
                "reason": "v2 rank-buffer screen", "wide": True,
            }
        )
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
            "sleeve": Sleeve.MOMENTUM_BREAKOUT.value,
            "horizon": Horizon.POSITIONAL.value,
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
            "reasons": [ReasonCode.MULTI_HORIZON_MOMENTUM.value, ReasonCode.MA_STACK_ALIGNED.value],
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
    n_trials: int = 36,
    warmup_sessions: int = 40,
    index_regime: pl.DataFrame | None = None,
    entry_block: EntryBlock | None = None,
    use_pit_liquidity: bool = True,
) -> BacktestResult:
    """Execute v2 Sortino-Momentum Monthly Rank-Buffer backtest via `PaperBroker`."""
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
