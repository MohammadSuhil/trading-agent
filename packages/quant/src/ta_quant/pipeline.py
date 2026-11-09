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
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

import polars as pl
from ta_core.costs import compute_costs
from ta_core.enums import (
    TREND_FOLLOWING_SLEEVES,
    Action,
    Horizon,
    ProductType,
    ReasonCode,
    Regime,
    Sleeve,
    TrendState,
)
from ta_ledger.risk import (
    DEFAULT,
    Candidate,
    Decision,
    Portfolio,
    Position,
    evaluate_entry,
)

from .barriers import build_path_library, estimate_barriers
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


def build_panel(bars: pl.DataFrame, members: pl.DataFrame) -> pl.DataFrame:
    """Factors + trend + industry labels for the whole panel.

    `members` carries the DATED industry label. Joining today's membership onto
    past dates is the survivorship bias the plan calls out by name, so the
    caller must pass a snapshot read through the PIT guard.
    """
    panel = add_trend_states(add_price_factors(bars))
    return panel.join(
        members.select(["symbol", "industry", "company_name"]).unique(subset=["symbol"]),
        on="symbol",
        how="left",
    ).with_columns(pl.col("industry").fill_null("Unclassified"))


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
        day = day.filter(pl.col("symbol").is_in(list(universe)))
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
        df = df.filter(pl.col("symbol").is_in(list(universe)))
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
    day = panel.filter((pl.col("d") == d) & pl.col("symbol").is_in(list(symbols)))
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
    day = panel.filter((pl.col("d") == d) & pl.col("symbol").is_in(list(tradeable)))
    if day.height == 0:
        return day

    def z_in_sector(col: str) -> pl.Expr:
        c = pl.col(col)
        return ((c - c.mean().over("industry")) / c.std().over("industry")).fill_nan(0.0)

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
) -> RunResult:
    """Produce the night's ranked trade plans and the rejection log.

    `as_of` is the decision instant and is RECORDED, never re-derived: a replay
    in 2027 must stamp the instant the decision was actually made, not the one
    the replay happens to run at.
    """
    run_id = uuid.uuid4().hex[:12]
    as_of = as_of or datetime.now(UTC)

    panel = build_panel(bars, members)
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

    scored = score_universe(panel, d, score_syms)

    # Barrier library: built ONCE from the whole panel, not per candidate.
    lib = build_path_library(
        panel.filter(pl.col("symbol").is_in(list(trade_syms))),
        horizon=Horizon.SWING, k_up=K_UP, k_dn=K_DN,
    )
    log.info("path library: %d resolved paths", lib.height)

    recs: list[dict] = []
    rejs: list[dict] = []
    alternates: list[dict] = []
    max_alternates = max_cards
    pf = portfolio

    for row in scored.iter_rows(named=True):
        if len(recs) >= max_cards:
            break
        sym = row["symbol"]
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
            # Real listed history. This read `_bars_seen`, which the trend
            # stage dropped along with every other underscore-prefixed
            # column, so the value was always the 999 fallback and RISK-11's
            # minimum-history gate could never fire -- a gate that renders
            # green and never binds, which is exactly what the risk-test
            # docstring warns about.
            listed_sessions=int(row.get("bars_seen") or 0),
            series=str(row.get("series") or "EQ"),
        )
        # Sequential construction against a RUNNING book, not against the
        # starting portfolio each time. Sizing every candidate as though it
        # were the only position is how a screener produces 25 cards that
        # jointly breach every cap it claims to enforce -- the industry cap,
        # the heat cap and the position count only bind if the book grows as
        # candidates are accepted.
        # TWO sizings, answering two different questions. Collapsing them was
        # confusing: a card showed a size that shrank as earlier cards above it
        # consumed the hypothetical book, so the same stock was "worth Rs 74k"
        # at rank 3 and "Rs 0" at rank 20, for reasons invisible on the card.
        #
        #   decision   - what you are advised to buy NOW, gated against the
        #                book as it fills. This is the instruction.
        #   affordable - the most you could put into this name today given your
        #                REAL portfolio: your risk appetite, the cash you hold,
        #                and the headroom left in its industry. This is the
        #                question "how much of this could I buy?" and it does
        #                not change based on what ranked above it.
        decision: Decision = evaluate_entry(pf, cand, regime=regime, cfg=cfg)
        affordable: Decision = evaluate_entry(portfolio, cand, regime=regime, cfg=cfg)
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

        # Expectancy in ATR units, AFTER costs. A candidate that cannot clear
        # its own frictions is not a trade, however good the signal looks.
        gross_atr = (TARGET_ATR * (est.p_up or 0.0)) - (STOP_ATR * (est.p_dn or 0.0))
        cost_atr = (cost_bps / 10_000.0) * float(price) / float(atr)
        expectancy = gross_atr - cost_atr if est.p_up is not None else 0.0

        # RISK-P1, applied as a DOWNGRADE rather than a rejection, and the
        # reason matters more than the rule.
        #
        # The barrier estimate is the arm-invariant BASE rate: by design it is
        # symbol-agnostic, and the EDGE term that would differentiate this
        # candidate contributes exactly 0.0 until the system has resolved
        # enough of its own trades (n_eff >= 30) to have earned it. With
        # symmetric-ish barriers a base rate is ~zero expectancy BEFORE costs
        # and necessarily negative after them. So a hard +0.05 ATR gate on
        # BASE alone rejects 100% of candidates on every night forever: that
        # is not a risk control, it is a shutdown with extra steps.
        #
        # The honest treatment is to publish the plan and label it truthfully.
        # A card marked NO_DEMONSTRATED_EDGE is informative; an empty screen
        # that silently swallowed 367 candidates is not.
        tradeable = expectancy >= MIN_EXPECTANCY_ATR
        action = Action.BUY if tradeable else Action.WATCH
        edge_note = "" if tradeable else (
            f"no demonstrated edge: E={expectancy:+.3f} ATR at base rates "
            f"(p_up={est.p_up:.3f} p_dn={est.p_dn:.3f}), "
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
