"""The nightly run: bars in, ranked trade plans out. Zero LLM calls.

This is milestone M2's deliverable and it is deliberately complete on its own.
Every number on a recommendation card -- entry band, stop, target, size, cost,
expectancy, barrier probability -- is computed here in Python and recorded. The
LLM layer, when it arrives, selects among these pre-validated candidates and
explains them; it never produces a number and never widens a stop.

Order of operations, and why:

  1. factors + trend      - panel-wide, vectorised
  2. sector aggregates    - the heat map is ONE select from a precomputed table
  3. hard gates           - liquidity/surveillance/geometry, BEFORE scoring, so
                            an untradeable name never consumes ranking budget
  4. composite score      - sector-neutralised cross-sectional ranks
  5. barrier estimate     - refuses rather than guesses
  6. risk gates + sizing  - the capital-protection rules decide the size
  7. record               - plans and rejections both, because a gate nobody
                            can see is a gate nobody trusts
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

import polars as pl
from ta_core.costs import compute_costs
from ta_core.enums import (
    HORIZON_SESSIONS,
    TREND_FOLLOWING_SLEEVES,
    Action,
    Horizon,
    ProductType,
    ReasonCode,
    Regime,
    Sleeve,
    TrendState,
)
from ta_core.ports import (
    EdgeEstimatorPort,
    FeatureProviderPort,
    ProbabilityCalibratorPort,
)
from ta_ledger.risk import (
    DEFAULT,
    Candidate,
    Decision,
    Portfolio,
    Position,
    evaluate_entry,
    regime_caps,
)

from .barriers import build_path_library, estimate_barriers
from .factors.correlation import compute_pair_correlations
from .factors.price import add_price_factors
from .trend import add_trend_states

log = logging.getLogger(__name__)

__all__ = ["RunResult", "build_panel", "run_nightly", "score_universe"]

#: Stop distance in ATR, and the target. The screener does not get to tune
#: these per candidate: a per-candidate barrier is an unfalsifiable backtest.
STOP_ATR = 1.5
TARGET_ATR = 2.0

#: The barrier geometry MUST equal the live geometry. An earlier version
#: measured P(touch -1.0 ATR) while placing the stop at -1.5 ATR, which
#: overstated P(stop) badly and made every candidate look negative-expectancy.
#: This is the same class of error the plan warns about for the time stop --
#: "H must equal the live engine's time-stop exactly, or labels and execution
#: disagree" -- applied to the price barriers rather than the horizon.
K_UP, K_DN = TARGET_ATR, STOP_ATR

#: RISK-P1. Required expectancy margin, in ATR, AFTER full Indian costs.
#: At Rs 50,000 delivery the round trip is ~25 bps, which on a 2%-ATR stock is
#: ~0.13 ATR. Requiring +0.05 on top means the edge must beat its frictions by
#: a visible margin rather than merely tie them.
MIN_EXPECTANCY_ATR = 0.05

#: Rules that reject because the BOOK is full rather than because the candidate
#: is bad. These become ranked alternates: the user should be able to see that
#: a good idea was displaced by a cap, not silently dropped.
_BOOK_LEVEL_RULES = frozenset({
    "RISK-04",   # portfolio heat
    "RISK-05",   # position count / cash floor
    "RISK-06",   # industry concentration
    "RISK-08",   # pair correlation
})


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    as_of: datetime
    d: date
    recommendations: pl.DataFrame
    rejections: pl.DataFrame
    sector: pl.DataFrame
    breadth: pl.DataFrame
    trend: pl.DataFrame
    n_universe: int
    regime: Regime


def build_panel(
    bars: pl.DataFrame,
    members: pl.DataFrame,
    *,
    feature_providers: Sequence[FeatureProviderPort] = (),
) -> pl.DataFrame:
    """Factors + trend + industry labels for the whole panel.

    `members` carries the DATED industry label. Joining today's membership onto
    past dates is the survivorship bias the plan calls out by name, so the
    caller must pass a snapshot read through the PIT guard.
    """
    panel = add_trend_states(add_price_factors(bars))
    joined = panel.join(
        members.select(["symbol", "industry", "company_name"]).unique(subset=["symbol"]),
        on="symbol",
        how="left",
    ).with_columns(pl.col("industry").fill_null("Unclassified"))
    for provider in feature_providers:
        res = provider.compute(joined)
        if isinstance(res, pl.DataFrame):
            joined = res
    return joined


def sector_aggregates(
    panel: pl.DataFrame, d: date, universe: set[str] | None = None
) -> pl.DataFrame:
    """Industry rollup for the heat map. Precomputed so the endpoint is 1 SELECT.

    `universe` restricts the rollup to named index constituents. Without it the
    whole ~2,700-symbol market rolls up, 2,400 of which carry no industry label
    at all, and the heat map is dominated by a single meaningless
    "Unclassified" tile larger than every real sector combined.
    """
    day = panel.filter(pl.col("d") == d)
    if universe:
        day = day.filter(pl.col("symbol").is_in(sorted(universe)))
    day = day.filter(pl.col("industry") != "Unclassified")
    if day.height == 0:
        return pl.DataFrame()
    return (
        day.group_by("industry")
        .agg(
            pl.len().alias("n_symbols"),
            pl.col("turnover").sum().alias("turnover"),
            # Turnover-weighted, so a thin stock's 15% print does not move the
            # sector tile the way an equal-weight mean would.
            ((pl.col("ret_1d") * pl.col("turnover")).sum()
             / pl.col("turnover").sum()).alias("cap_weighted_ret"),
            pl.col("ret_1d").median().alias("median_ret"),
            (pl.col("ret_1d") > 0).mean().alias("breadth_pct"),
            (pl.col("ret_1d") > 0).sum().alias("adv_count"),
            (pl.col("ret_1d") <= 0).sum().alias("dec_count"),
            pl.col("ret_21d").median().alias("rs_21d"),
        )
        .with_columns(
            pl.lit(d).alias("d"),
            ((pl.col("cap_weighted_ret") - pl.col("cap_weighted_ret").mean())
             / pl.col("cap_weighted_ret").std()).fill_nan(0.0).fill_null(0.0)
            .alias("move_z"),
        )
        .sort("turnover", descending=True)
    )


def industry_trends(panel: pl.DataFrame, universe: set[str] | None = None) -> pl.DataFrame:
    """Trend state per INDUSTRY, from a synthetic turnover-weighted index.

    A card showing "this stock is trending up" is far less useful without
    "...and so is its industry" (confirmation) or "...while its industry is
    rolling over" (a warning). So each industry gets its own index series and
    is then run through exactly the same classifier the stocks use -- same
    thresholds, same hysteresis, same enum. Using a second, looser definition
    for industries would make the two incomparable on the card.

    The index is built by compounding the turnover-weighted daily return of
    its members, which is the same weighting the heat map uses.

    **Caveat, stated because it affects two factors:** a synthesised index has
    no intraday range, so `high = low = close`. True range collapses to
    |close - prev_close|, which makes ATR and ADX slightly understate an
    industry's volatility relative to a real index. Direction, the efficiency
    ratio and the drift t-stat are unaffected, and those carry the
    classification.
    """
    df = panel.filter(pl.col("industry") != "Unclassified")
    if universe:
        df = df.filter(pl.col("symbol").is_in(sorted(universe)))
    if df.is_empty():
        return pl.DataFrame()

    daily = (
        df.filter(pl.col("ret_1d").is_not_null() & (pl.col("turnover") > 0))
        .group_by(["industry", "d"])
        .agg(
            ((pl.col("ret_1d") * pl.col("turnover")).sum()
             / pl.col("turnover").sum()).alias("r"),
            pl.col("turnover").sum().alias("turnover"),
            pl.len().alias("n_trades"),
        )
        .sort(["industry", "d"])
    )
    if daily.is_empty():
        return pl.DataFrame()

    idx = daily.with_columns(
        (100.0 * (1.0 + pl.col("r")).cum_prod().over("industry")).alias("close")
    ).with_columns(
        pl.col("industry").alias("symbol"),
        pl.col("close").alias("open"),
        pl.col("close").alias("high"),
        pl.col("close").alias("low"),
        pl.col("close").shift(1).over("industry").alias("prev_close"),
        pl.lit("EQ").alias("series"),
        pl.lit(0).cast(pl.UInt64).alias("volume"),
    )

    out = add_trend_states(add_price_factors(idx))
    return out.select([
        "d", pl.col("symbol").alias("industry"),
        pl.col("trend_20").alias("ind_trend_20"),
        pl.col("trend_63").alias("ind_trend_63"),
        pl.col("trend_252").alias("ind_trend_252"),
        pl.col("er_63").alias("ind_er_63"),
        pl.col("t_drift_63").alias("ind_t_drift_63"),
    ])


def breadth_row(panel: pl.DataFrame, d: date, universe: str, symbols: set[str]) -> dict:
    day = panel.filter((pl.col("d") == d) & pl.col("symbol").is_in(sorted(symbols)))
    if day.height == 0:
        return {}
    return {
        "d": d, "universe": universe, "n_symbols": day.height,
        "advancers": int((day["ret_1d"] > 0).sum()),
        "decliners": int((day["ret_1d"] <= 0).sum()),
        "pct_above_sma50": float((day["close"] > day["sma50"]).mean() or 0.0),
        "pct_above_sma200": float((day["close"] > day["sma200"]).mean() or 0.0),
        "ret_dispersion_xs": float(day["ret_1d"].std() or 0.0),
        "median_ret": float(day["ret_1d"].median() or 0.0),
    }


def infer_regime(breadth: dict) -> tuple[Regime, str]:
    """A deliberately crude, auditable stand-in for the M10 HMM.

    Stated plainly on the dashboard as a heuristic, because calling this a
    "regime model" when it is four thresholds would be the kind of overclaim
    the rest of the system exists to prevent. It gates risk only -- it never
    flips a signal's side.
    """
    if not breadth:
        return Regime.UNKNOWN, "no breadth row for this session"
    above = breadth.get("pct_above_sma50", 0.0)
    disp = breadth.get("ret_dispersion_xs", 0.0)
    med = breadth.get("median_ret", 0.0)
    if disp > 0.030:
        return Regime.HIGH_VOL, f"cross-sectional dispersion {disp:.2%} > 3.0%"
    if above >= 0.60 and med > 0:
        return Regime.TREND_UP, f"{above:.0%} above 50-DMA"
    if above <= 0.35:
        return Regime.TREND_DOWN, f"only {above:.0%} above 50-DMA"
    return Regime.CHOP, f"{above:.0%} above 50-DMA, dispersion {disp:.2%}"


def _ind(table: dict[str, dict], industry: str, key: str) -> str:
    """Industry trend lookup. UNDEFINED, never RANGE, when absent -- an
    industry we could not measure is not an industry that went sideways."""
    row = table.get(industry)
    return (row or {}).get(key) or "UNDEFINED"


def _reasons(r: dict) -> list[str]:
    """Closed reason codes only. Free text would make attribution impossible."""
    out: list[str] = []
    if r.get("ma_stack") == 1:
        out.append(ReasonCode.MA_STACK_ALIGNED.value)
    if (r.get("adx14") or 0) >= 25:
        out.append(ReasonCode.ADX_TRENDING.value)
    if (r.get("mom_12_1") or 0) > 0.10 and (r.get("ret_63d") or 0) > 0:
        out.append(ReasonCode.MULTI_HORIZON_MOMENTUM.value)
    if (r.get("rvol_20") or 0) >= 1.5:
        out.append(ReasonCode.RVOL_SPIKE.value)
    if (r.get("turnover") or 0) > 1.5 * (r.get("adtv_20") or 1e18):
        out.append(ReasonCode.TURNOVER_EXPANSION.value)
    if (r.get("rs_sector") or 0) > 0:
        out.append(ReasonCode.RS_VS_SECTOR.value)
    if (r.get("dist_52w_high") or -1) > -0.05:
        out.append(ReasonCode.SECTOR_LEADERSHIP.value)
    if (r.get("bb_z") or 0) < -1.5:
        out.append(ReasonCode.OVERSOLD_ZSCORE.value)
    return out or [ReasonCode.QUALITY_COMPOSITE.value]


def _sleeve(r: dict) -> Sleeve:
    t = r.get("trend_63")
    if (r.get("bb_z") or 0) < -1.5 and t in (TrendState.UP, TrendState.UP_STRONG):
        return Sleeve.PULLBACK_IN_UPTREND
    if (r.get("dist_52w_high") or -1) > -0.03 and (r.get("rvol_20") or 0) >= 1.3:
        return Sleeve.MOMENTUM_BREAKOUT
    if (r.get("bb_z") or 0) < -2.0:
        return Sleeve.MEAN_REVERSION
    if (r.get("rs_sector") or 0) > 0.05:
        return Sleeve.SECTOR_ROTATION
    return Sleeve.MOMENTUM_BREAKOUT


def score_universe(panel: pl.DataFrame, d: date, tradeable: set[str]) -> pl.DataFrame:
    """Cross-sectional composite, sector-neutralised.

    Ranks are computed WITHIN industry so the screener does not simply return
    whichever sector ran hardest this month -- that is a sector bet wearing a
    stock-picking costume.
    """
    day = panel.filter((pl.col("d") == d) & pl.col("symbol").is_in(sorted(tradeable)))
    if day.height == 0:
        return day

    def z_in_sector(col: str) -> pl.Expr:
        c = pl.col(col)
        return ((c - c.mean().over("industry")) / c.std().over("industry")).fill_nan(0.0).fill_null(0.0)

    day = day.with_columns(
        (pl.col("ret_21d") - pl.col("ret_21d").mean().over("industry")).alias("rs_sector"),
    ).with_columns(
        z_in_sector("mom_12_1").alias("_z_mom"),
        z_in_sector("ret_63d").alias("_z_med"),
        z_in_sector("rs_sector").alias("_z_rs"),
        z_in_sector("adx14").alias("_z_adx"),
        (-z_in_sector("realised_vol_21d")).alias("_z_lowvol"),
    ).with_columns(
        (0.30 * pl.col("_z_mom") + 0.20 * pl.col("_z_med") + 0.25 * pl.col("_z_rs")
         + 0.15 * pl.col("_z_adx") + 0.10 * pl.col("_z_lowvol"))
        .fill_null(0.0).fill_nan(0.0).alias("composite")
    )
    return day.drop([c for c in day.columns if c.startswith("_z_")]).sort(
        "composite", descending=True
    )


def _pm_conviction(
    row: dict[str, Any],
    est: Any,
    pre_exp: float,
    cost_atr: float,
    ind_today: dict[str, dict],
    corr_map: dict[str, dict[str, float]] | None = None,
    open_positions: tuple[Position, ...] = (),
) -> float:
    """PM Conviction Score combining factor composite, conservative lower-bound
    expectancy (at 90% CI lower bound `ci_lo`), multi-timeframe stock + industry
    trend alignment, and marginal portfolio correlation penalty.
    """
    comp = float(row.get("composite") or 0.0)
    if est.ci_lo is not None and est.p_up is not None and est.p_dn is not None:
        p_up_cons = float(est.ci_lo)
        shift = max(0.0, float(est.p_up) - p_up_cons)
        p_dn_cons = min(1.0 - p_up_cons, float(est.p_dn) + shift)
        cons_exp = (TARGET_ATR * p_up_cons) - (STOP_ATR * p_dn_cons) - cost_atr
    else:
        cons_exp = pre_exp

    up_states = {TrendState.UP_STRONG.value, TrendState.UP.value}
    dn_states = {TrendState.DOWN_STRONG.value, TrendState.DOWN.value, TrendState.DOWN_DECAYING.value}

    align = 0.0
    t63 = str(row.get("trend_63") or "UNDEFINED")
    t252 = str(row.get("trend_252") or "UNDEFINED")
    if t63 in up_states:
        align += 0.25
    elif t63 in dn_states:
        align -= 0.25
    if t252 in up_states:
        align += 0.15
    elif t252 in dn_states:
        align -= 0.15

    ind = str(row.get("industry") or "Unclassified")
    ind63 = _ind(ind_today, ind, "ind_trend_63")
    ind20 = _ind(ind_today, ind, "ind_trend_20")
    if ind63 in up_states:
        align += 0.35
    elif ind63 in dn_states:
        align -= 0.35
    if ind20 in up_states:
        align += 0.25
    elif ind20 in dn_states:
        align -= 0.25

    sym = str(row.get("symbol") or "")
    max_corr = 0.0
    if open_positions:
        if corr_map and sym in corr_map:
            max_corr = max((corr_map[sym].get(p.symbol, 0.0) for p in open_positions), default=0.0)
        if any(p.industry == ind for p in open_positions):
            max_corr = max(max_corr, 0.35)

    return (
        0.45 * comp
        + 2.50 * max(cons_exp, -0.50)
        + 1.50 * max(pre_exp, -0.50)
        + 0.30 * align
        - 0.35 * max(0.0, max_corr)
    )


def run_nightly(
    bars: pl.DataFrame,
    members: pl.DataFrame,
    *,
    d: date,
    portfolio: Portfolio,
    tradeable_index: str = "NIFTY500",
    scored_index: str = "NIFTY500",
    max_cards: int = 25,
    cfg=DEFAULT,
    as_of: datetime | None = None,
    surveillance_symbols: set[str] | None = None,
    run_id: str | None = None,
    feature_providers: Sequence[FeatureProviderPort] = (),
    edge_estimator: EdgeEstimatorPort | None = None,
    calibrator: ProbabilityCalibratorPort | None = None,
) -> RunResult:
    """Produce the night's ranked trade plans and the rejection log.

    `as_of` is the decision instant and is RECORDED, never re-derived: a replay
    in 2027 must stamp the instant the decision was actually made, not the one
    the replay happens to run at.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    as_of = as_of or datetime.now(UTC)

    panel = (
        build_panel(bars, members, feature_providers=feature_providers)
        if feature_providers
        else build_panel(bars, members)
    )
    trade_syms = set(
        members.filter(pl.col("index_name") == tradeable_index)["symbol"].to_list()
    )
    configured_score_syms = set(
        members.filter(pl.col("index_name") == scored_index)["symbol"].to_list()
    )
    score_syms = configured_score_syms or trade_syms
    market_index = scored_index if configured_score_syms else tradeable_index

    sector = sector_aggregates(panel, d, score_syms or trade_syms)
    ind_tr = industry_trends(panel, score_syms or trade_syms)
    ind_today: dict[str, dict] = {}
    if not ind_tr.is_empty():
        ind_today = {
            r["industry"]: r
            for r in ind_tr.filter(pl.col("d") == d).iter_rows(named=True)
        }
    market_breadth = breadth_row(panel, d, market_index, score_syms)
    trade_breadth = breadth_row(panel, d, tradeable_index, trade_syms)
    breadth_rows = {
        row["universe"]: row
        for row in (trade_breadth, market_breadth)
        if row
    }
    regime, regime_note = infer_regime(market_breadth)
    log.info("regime=%s (%s)", regime.value, regime_note)
    caps = regime_caps(regime)
    new_cap = int(caps["new"])

    scored = score_universe(panel, d, score_syms)

    scored_syms = scored["symbol"].to_list() if not scored.is_empty() else []
    all_relevant_syms = sorted(set(scored_syms) | {p.symbol for p in portfolio.positions})
    corr_map = compute_pair_correlations(
        panel, d, candidate_symbols=scored_syms, open_symbols=all_relevant_syms
    )

    # Barrier library: built ONCE from the whole panel, not per candidate.
    lib = build_path_library(
        panel.filter(pl.col("symbol").is_in(sorted(trade_syms))),
        horizon=Horizon.SWING, k_up=K_UP, k_dn=K_DN,
    )
    log.info("path library: %d resolved paths", lib.height)

    recs: list[dict] = []
    rejs: list[dict] = []
    alternates: list[dict] = []
    max_alternates = max_cards
    prior_positions = tuple(p for p in portfolio.positions if p.opened < d)
    today_cost = sum(p.entry * p.qty for p in portfolio.positions if p.opened >= d)
    pf = replace(portfolio, cash=portfolio.cash + today_cost, positions=prior_positions)
    owned_syms = {p.symbol for p in prior_positions}

    # Open positions from prior sessions get HOLD or SELL cards (Invariant I-16: only owned symbols get HOLD/SELL)
    for pos in prior_positions:
        pos_df = panel.filter((pl.col("d") == d) & (pl.col("symbol") == pos.symbol))
        if pos_df.is_empty():
            continue
        pos_row = pos_df.to_dicts()[0]
        close = float(pos_row["close"])
        atr = float(pos_row.get("atr14") or 0.0)
        if atr <= 0:
            atr = close * 0.02

        sessions_held = int(
            panel.filter(
                (pl.col("symbol") == pos.symbol) & (pl.col("d") > pos.opened) & (pl.col("d") <= d)
            ).height
        )
        max_sessions = (
            HORIZON_SESSIONS.get(Horizon(pos.horizon), 10)
            if pos.horizon in Horizon._value2member_map_
            else 10
        )

        hit_stop = close <= pos.stop
        hit_target = pos.target > 0 and close >= pos.target
        hit_time_stop = sessions_held >= max_sessions
        hit_trend_break = (
            str(pos_row.get("trend_63") or "") in ("DOWN", "DOWN_STRONG")
            and str(pos_row.get("trend_20") or "") in ("DOWN", "DOWN_STRONG")
        )

        is_sell = hit_stop or hit_target or hit_time_stop or hit_trend_break
        if hit_stop:
            reason_code = ReasonCode.STOP_DISCIPLINE.value
            exit_note = "STOP_DISCIPLINE: price breached stop"
        elif hit_target:
            reason_code = ReasonCode.STOP_DISCIPLINE.value
            exit_note = "TARGET_REACHED: price reached target"
        elif hit_time_stop:
            reason_code = ReasonCode.TIME_STOP.value
            exit_note = f"TIME_STOP: held {sessions_held}/{max_sessions} sessions (vertical barrier expired)"
        elif hit_trend_break:
            reason_code = ReasonCode.REGIME_RISK_OFF.value
            exit_note = "TREND_REVERSAL: 20d and 63d trends turned DOWN"
        else:
            reason_code = ""
            exit_note = ""

        action = Action.SELL.value if is_sell else Action.HOLD.value
        reasons = [reason_code] if is_sell else _reasons(pos_row)
        afford_note = exit_note if is_sell else f"open position ({sessions_held}/{max_sessions} sessions held)"

        if is_sell:
            st = round(pos.stop, 2)
        else:
            raw_st = max(pos.stop, round(close - STOP_ATR * atr, 2)) if close > (pos.entry + atr) else pos.stop
            st = round(raw_st, 2)

        tgt = round(pos.entry + TARGET_ATR * atr, 2)
        e_lo = round(pos.entry * 0.995, 2)
        e_hi = round(pos.entry * 1.005, 2)
        if e_lo >= e_hi:
            e_hi = round(e_lo + 0.01, 2)

        if st >= e_lo:
            st = round(e_lo - 0.01, 2)
        if tgt <= e_hi:
            tgt = round(e_hi + 0.01, 2)

        notional = round(pos.qty * close, 2)
        sleeve = _sleeve(pos_row)

        recs.append({
            "rec_id": f"{run_id}-{pos.symbol}",
            "d": d,
            "symbol": pos.symbol,
            "name": pos_row.get("company_name") or pos.symbol,
            "industry": pos.industry,
            "arm": "DETERMINISTIC",
            "action": action,
            "sleeve": sleeve.value,
            "horizon": Horizon.SWING.value,
            "entry_lo": e_lo,
            "entry_hi": e_hi,
            "stop": st,
            "target": tgt,
            "qty": pos.qty,
            "notional": notional,
            "would_qty": pos.qty,
            "would_notional": notional,
            "would_risk_rupees": round(pos.qty * abs(pos.entry - st), 2),
            "afford_note": afford_note,
            "risk_rupees": round(pos.qty * max(0.0, close - st), 2),
            "risk_pct": round((pos.qty * max(0.0, close - st)) / pf.equity, 5) if pf.equity else 0.0,
            "rr": round((tgt - close) / max(0.01, close - st), 2) if close > st else 1.0,
            "atr14": round(atr, 4),
            "cost_bps": 0.0,
            "expectancy_atr": 0.0,
            "composite": round(float(pos_row.get("composite") or 0.0), 4),
            "reasons": reasons,
            "trend_20": str(pos_row.get("trend_20") or "UNDEFINED"),
            "trend_63": str(pos_row.get("trend_63") or "UNDEFINED"),
            "trend_252": str(pos_row.get("trend_252") or "UNDEFINED"),
            "ind_trend_20": _ind(ind_today, pos.industry, "ind_trend_20"),
            "ind_trend_63": _ind(ind_today, pos.industry, "ind_trend_63"),
            "ind_trend_252": _ind(ind_today, pos.industry, "ind_trend_252"),
            "barrier_wide": False,
            "state": "OK",
            "p_up": 0.5,
            "p_dn": 0.5,
            "p_vert": 0.0,
            "ci_lo": 0.4,
            "ci_hi": 0.6,
            "n_eff": 100.0,
            "barrier_state": "OK",
            "barrier_reason": "open position tracked by portfolio",
        })

    # Expectancy-aware candidate ordering
    nom_bps = compute_costs(75_000.0, 75_000.0, ProductType.DELIVERY).bps_of(75_000.0)
    top_scored = scored.head(min(80, scored.height)).to_dicts() if not scored.is_empty() else []
    candidate_rows: list[dict[str, Any]] = []
    seen_syms: set[str] = set()

    for row in top_scored:
        sym = row["symbol"]
        seen_syms.add(sym)
        if sym in owned_syms:
            continue
        price, atr = row.get("close"), row.get("atr14")
        if not price or not atr or atr <= 0:
            continue
        est = estimate_barriers(
            lib,
            atr_pct=float(atr) / float(price),
            horizon=Horizon.SWING,
            k_up=K_UP,
            k_dn=K_DN,
        )
        if est.p_up is not None and est.p_dn is not None:
            if edge_estimator is not None or calibrator is not None:
                edge_adj = float(edge_estimator.estimate_edge(row, est)) if edge_estimator is not None else 0.0
                raw_p_up = min(1.0, max(0.0, float(est.p_up) + edge_adj))
                p_up_eff = (
                    min(1.0, max(0.0, float(calibrator.calibrate(raw_p_up, horizon=Horizon.SWING.value, regime=regime.value))))
                    if calibrator is not None
                    else raw_p_up
                )
                p_dn_eff = max(0.0, min(1.0 - p_up_eff, float(est.p_dn) - (p_up_eff - float(est.p_up))))
            else:
                p_up_eff = float(est.p_up)
                p_dn_eff = float(est.p_dn)

            cost_atr_nom = (nom_bps / 10_000.0) * float(price) / float(atr)
            pre_exp = (
                (TARGET_ATR * p_up_eff)
                - (STOP_ATR * p_dn_eff)
                - cost_atr_nom
            )
        else:
            cost_atr_nom = 0.0
            pre_exp = -999.0
        row["_pre_exp"] = pre_exp
        row["_pm_conviction"] = _pm_conviction(
            row, est, pre_exp, cost_atr_nom, ind_today, corr_map, pf.positions
        )
        candidate_rows.append(row)

    candidate_rows.sort(
        key=lambda r: (
            r["_pre_exp"] >= MIN_EXPECTANCY_ATR,
            float(r.get("_pm_conviction") or 0.0),
        ),
        reverse=True,
    )

    if not scored.is_empty():
        for row in scored.iter_rows(named=True):
            if row["symbol"] not in seen_syms and row["symbol"] not in owned_syms:
                row["_pre_exp"] = -999.0
                candidate_rows.append(row)

    new_entries_today = 0
    rotated_syms: set[str] = set()

    for row in candidate_rows:
        if len(recs) >= max_cards:
            break
        sym = row["symbol"]
        if sym in owned_syms:
            continue
        price, atr = row.get("close"), row.get("atr14")
        if not price or not atr or atr <= 0:
            rejs.append({"symbol": sym, "rule_id": "DATA", "reason": "NO_ATR",
                         "detail": "insufficient history for ATR14"})
            continue

        stop = price - STOP_ATR * atr
        target = price + TARGET_ATR * atr
        sleeve = _sleeve(row)

        # TRD-03: a decaying trend blocks NEW trend-following entries. It does
        # not force an exit and never flips the side.
        if (sleeve.value in TREND_FOLLOWING_SLEEVES
                and row.get("trend_63") in (TrendState.UP_DECAYING.value,
                                            TrendState.DOWN_DECAYING.value)):
            rejs.append({"symbol": sym, "rule_id": "TRD-03", "reason": "TREND_DECAYING",
                         "detail": f"trend_63={row.get('trend_63')} blocks {sleeve.value}"})
            continue
        # TRD-05: a name that has changed its mind six times a quarter is not
        # trending, whatever today's state says.
        if (row.get("flips_63d") or 0) >= 6 and sleeve.value in TREND_FOLLOWING_SLEEVES:
            rejs.append({"symbol": sym, "rule_id": "TRD-05", "reason": "WHIPSAW",
                         "detail": f"{row.get('flips_63d')} state flips in 63 sessions"})
            continue

        cand = Candidate(
            symbol=sym,
            industry=row.get("industry") or "Unclassified",
            price=float(price), stop=float(stop), target=float(target),
            adtv_rupees=float(row.get("adtv_20") or 0.0),
            n_trades=int(row.get("n_trades") or 0),
            listed_sessions=int(row.get("bars_seen") or 0),
            series=str(row.get("series") or "EQ"),
            under_surveillance=(sym in (surveillance_symbols or set())),
            pair_corr={p.symbol: corr_map.get(sym, {}).get(p.symbol, 0.0) for p in pf.positions},
        )
        decision: Decision = evaluate_entry(pf, cand, regime=regime, cfg=cfg)
        affordable: Decision = evaluate_entry(portfolio, cand, regime=regime, cfg=cfg)

        pre_exp = float(row.get("_pre_exp", -999.0))
        if (
            not decision.allowed
            and decision.rule_id in ("RISK-05", "RISK-06", "RISK-01")
            and pre_exp >= MIN_EXPECTANCY_ATR
            and new_entries_today < new_cap
        ):
            eligible_positions = [
                p for p in pf.positions
                if p.opened < d and p.symbol not in rotated_syms
                and (
                    decision.rule_id == "RISK-05"
                    or (decision.rule_id == "RISK-06" and p.industry == cand.industry)
                    or (decision.rule_id == "RISK-01" and pf.industry_notional(cand.industry) > 0 and p.industry == cand.industry)
                )
            ]
            weak_candidates = []
            for p in eligible_positions:
                p_df = panel.filter((pl.col("d") == d) & (pl.col("symbol") == p.symbol))
                if p_df.is_empty():
                    continue
                p_row = p_df.to_dicts()[0]
                weak_comp = float(p_row.get("composite") or 0.0)
                cand_comp = float(row.get("composite") or 0.0)
                weak_t20 = str(p_row.get("trend_20") or "UNDEFINED")
                weak_r = (p.last - p.entry) / max(p.entry - p.stop, 0.01)
                if (cand_comp - weak_comp >= 0.35 or weak_t20 in ("DOWN_STEADY", "DOWN_DECAYING", "RANGE")) and weak_r < 0.75:
                    weak_candidates.append((weak_comp, p, cand_comp))

            if weak_candidates:
                weak_candidates.sort(key=lambda item: item[0])
                lowest_comp, weak_p, cand_comp = weak_candidates[0]
                for r in recs:
                    if r["symbol"] == weak_p.symbol:
                        r["action"] = Action.SELL.value
                        r["reasons"] = [ReasonCode.REGIME_CAP.value]
                        r["afford_note"] = f"ROTATE_WEAKEST -> displaced by {sym} (comp {cand_comp:+.2f} vs {lowest_comp:+.2f})"
                        break
                pf = replace(
                    pf,
                    cash=pf.cash + weak_p.notional,
                    positions=tuple(pos for pos in pf.positions if pos.symbol != weak_p.symbol),
                )
                rotated_syms.add(weak_p.symbol)
                cand = Candidate(
                    symbol=sym,
                    industry=row.get("industry") or "Unclassified",
                    price=float(price), stop=float(stop), target=float(target),
                    adtv_rupees=float(row.get("adtv_20") or 0.0),
                    n_trades=int(row.get("n_trades") or 0),
                    listed_sessions=int(row.get("bars_seen") or 0),
                    series=str(row.get("series") or "EQ"),
                    under_surveillance=(sym in (surveillance_symbols or set())),
                    pair_corr={p.symbol: corr_map.get(sym, {}).get(p.symbol, 0.0) for p in pf.positions},
                )
                decision = evaluate_entry(pf, cand, regime=regime, cfg=cfg)
                affordable = evaluate_entry(portfolio, cand, regime=regime, cfg=cfg)

        if not decision.allowed:
            rejs.append({"symbol": sym, "rule_id": decision.rule_id,
                         "reason": decision.reason, "detail": decision.detail})
            # A candidate blocked only by a BOOK-level cap is still a good
            # idea that did not fit; it is kept as a ranked alternate so the
            # user can see what the cap cost them.
            if decision.rule_id in _BOOK_LEVEL_RULES and len(alternates) < max_alternates:
                alternates.append({"symbol": sym, "row": row, "cand": cand,
                                   "sleeve": sleeve, "blocked_by": decision.rule_id,
                                   "detail": decision.detail})
            continue

        # Costs on the ACTUAL notional, not a round number: the DP charge is
        # flat, so bps depend on size and a Rs 50,000 position is materially
        # more expensive per rupee than a Rs 1,00,000 one.
        buy_turnover = decision.qty * float(price)
        costs = compute_costs(buy_turnover, decision.qty * float(target),
                              ProductType.DELIVERY)
        cost_bps = costs.bps_of(buy_turnover)

        est = estimate_barriers(
            lib, atr_pct=float(atr) / float(price),
            horizon=Horizon.SWING, k_up=K_UP, k_dn=K_DN,
        )

        if est.p_up is not None and est.p_dn is not None:
            if edge_estimator is not None or calibrator is not None:
                edge_adj = float(edge_estimator.estimate_edge(row, est)) if edge_estimator is not None else 0.0
                raw_p_up = min(1.0, max(0.0, float(est.p_up) + edge_adj))
                p_up_eff = (
                    min(1.0, max(0.0, float(calibrator.calibrate(raw_p_up, horizon=Horizon.SWING.value, regime=regime.value))))
                    if calibrator is not None
                    else raw_p_up
                )
                p_dn_eff = max(0.0, min(1.0 - p_up_eff, float(est.p_dn) - (p_up_eff - float(est.p_up))))
            else:
                p_up_eff = float(est.p_up)
                p_dn_eff = float(est.p_dn)
        else:
            p_up_eff = None
            p_dn_eff = None

        # Expectancy in ATR units, AFTER costs. A candidate that cannot clear
        # its own frictions is not a trade, however good the signal looks.
        gross_atr = ((TARGET_ATR * p_up_eff) - (STOP_ATR * p_dn_eff)) if p_up_eff is not None and p_dn_eff is not None else 0.0
        cost_atr = (cost_bps / 10_000.0) * float(price) / float(atr)
        expectancy = gross_atr - cost_atr if p_up_eff is not None else 0.0

        tradeable = expectancy >= MIN_EXPECTANCY_ATR
        if tradeable:
            new_entries_today += 1
            action = Action.BUY
            edge_note = (
                ""
                if new_entries_today <= new_cap
                else f"Actionable BUY above today's auto-buy pace cap ({new_cap}/day in {regime.value}); available for manual Paper buy."
            )
        else:
            action = Action.WATCH
            p_str = (
                f"p_up={est.p_up:.3f} p_dn={est.p_dn:.3f}"
                if est.p_up is not None and est.p_dn is not None
                else "barriers unavailable"
            )
            edge_note = (
                f"no demonstrated edge: E={expectancy:+.3f} ATR at base rates "
                f"({p_str}), "
                f"costs {cost_bps:.1f} bps = {cost_atr:.3f} ATR. "
                f"EDGE term is 0.0 until the system has its own track record."
            )

        recs.append({
            "rec_id": f"{run_id}-{sym}", "d": d, "symbol": sym,
            "name": row.get("company_name") or sym,
            "industry": cand.industry, "arm": "DETERMINISTIC",
            "action": action.value, "sleeve": sleeve.value,
            "horizon": Horizon.SWING.value,
            "entry_lo": round(float(price) * 0.995, 2),
            "entry_hi": round(float(price) * 1.005, 2),
            "stop": round(float(stop), 2), "target": round(float(target), 2),
            # Actual vs hypothetical size. A WATCH card advises nothing, so
            # its qty is 0; `would_*` preserves the shape of the trade for
            # display without reading as an instruction.
            "qty": decision.qty if tradeable else 0,
            "notional": round(decision.notional, 2) if tradeable else 0.0,
            "would_qty": affordable.qty if affordable.allowed else 0,
            "would_notional": round(affordable.notional, 2) if affordable.allowed else 0.0,
            "would_risk_rupees": (round(affordable.risk_rupees, 2)
                                  if affordable.allowed else 0.0),
            "afford_note": (affordable.detail if affordable.allowed
                            else f"{affordable.rule_id}: {affordable.reason}"),
            "risk_rupees": round(decision.risk_rupees, 2) if tradeable else 0.0,
            "risk_pct": round(decision.risk_rupees / pf.equity, 5) if tradeable else 0.0,
            "rr": round((target - price) / (price - stop), 2),
            "atr14": round(float(atr), 4),
            "cost_bps": round(cost_bps, 2),
            "expectancy_atr": round(expectancy, 4),
            "composite": round(float(row.get("composite") or 0.0), 4),
            "reasons": _reasons(row),
            # The stock's own trend, and its industry's, at the same three
            # lookbacks and from the same classifier -- so "is this name
            # moving with its sector or against it?" is answerable on the card.
            "trend_20": str(row.get("trend_20") or "UNDEFINED"),
            "trend_63": str(row.get("trend_63") or "UNDEFINED"),
            "trend_252": str(row.get("trend_252") or "UNDEFINED"),
            "ind_trend_20": _ind(ind_today, cand.industry, "ind_trend_20"),
            "ind_trend_63": _ind(ind_today, cand.industry, "ind_trend_63"),
            "ind_trend_252": _ind(ind_today, cand.industry, "ind_trend_252"),
            **est.as_dict(),
            "barrier_wide": bool(est.wide),
            "barrier_state": est.state.value,
            "barrier_reason": f"{est.reason}. {edge_note}".strip().rstrip("."),
        })

        if not tradeable:
            # A watchlist card consumes no capital, so the book does not grow
            # and the next candidate still faces the real constraint.
            continue

        # Grow the book so the next candidate faces the real constraint.
        pf = replace(
            pf,
            cash=pf.cash - decision.notional,
            positions=(*pf.positions, Position(
                symbol=sym, industry=cand.industry, qty=decision.qty,
                entry=float(price), last=float(price), stop=float(stop), opened=d,
            )),
        )

    # Alternates: good ideas the book had no room for. Shown as HOLD, never
    # BUY -- taking one would breach the cap that displaced it.
    for alt in alternates[: max(0, max_cards - len(recs)) + max_alternates]:
        if alt["symbol"] in owned_syms:
            continue
        row, cand = alt["row"], alt["cand"]
        price, atr = float(row["close"]), float(row["atr14"])
        recs.append({
            "rec_id": f"{run_id}-{alt['symbol']}-ALT", "d": d, "symbol": alt["symbol"],
            "name": row.get("company_name") or alt["symbol"],
            "industry": cand.industry, "arm": "DETERMINISTIC",
            "action": Action.WATCH.value, "sleeve": alt["sleeve"].value,
            "horizon": Horizon.SWING.value,
            "entry_lo": round(price * 0.995, 2), "entry_hi": round(price * 1.005, 2),
            "stop": round(cand.stop, 2), "target": round(cand.target, 2),
            "qty": 0, "notional": 0.0, "would_qty": 0, "would_notional": 0.0,
            "would_risk_rupees": 0.0,
            "afford_note": f"displaced by {alt['blocked_by']}",
            "risk_rupees": 0.0, "risk_pct": 0.0,
            "rr": round((cand.target - price) / (price - cand.stop), 2),
            "atr14": round(atr, 4), "cost_bps": 0.0, "expectancy_atr": 0.0,
            "composite": round(float(row.get("composite") or 0.0), 4),
            "reasons": _reasons(row),
            "trend_20": str(row.get("trend_20") or "UNDEFINED"),
            "trend_63": str(row.get("trend_63") or "UNDEFINED"),
            "trend_252": str(row.get("trend_252") or "UNDEFINED"),
            "ind_trend_20": str(_ind(ind_today, cand.industry, "ind_trend_20")),
            "ind_trend_63": str(_ind(ind_today, cand.industry, "ind_trend_63")),
            "ind_trend_252": str(_ind(ind_today, cand.industry, "ind_trend_252")),
            "barrier_wide": False,
            "state": "UNAVAILABLE", "p_up": None, "p_dn": None, "p_vert": None,
            "ci_lo": None, "ci_hi": None, "n_eff": 0.0,
            "barrier_state": "UNAVAILABLE",
            "barrier_reason": f"not actionable: displaced by {alt['blocked_by']} "
                              f"({alt['detail']})",
        })

    recs_df = pl.DataFrame(recs) if recs else pl.DataFrame()
    rejs_df = pl.DataFrame(rejs) if rejs else pl.DataFrame()

    trend_cols = [c for c in (
        "d", "symbol", "trend_20", "trend_63", "trend_252",
        "t_drift_20", "t_drift_63", "t_drift_252", "er_20", "er_63", "er_252",
        "adx14", "adx_delta_10", "flips_63d", "trend_enum_version",
    ) if c in panel.columns]

    return RunResult(
        run_id=run_id, as_of=as_of, d=d,
        recommendations=recs_df, rejections=rejs_df,
        sector=sector,
        breadth=pl.DataFrame(list(breadth_rows.values())) if breadth_rows else pl.DataFrame(),
        trend=panel.filter(pl.col("d") == d).select(trend_cols),
        n_universe=len(trade_syms), regime=regime,
    )
