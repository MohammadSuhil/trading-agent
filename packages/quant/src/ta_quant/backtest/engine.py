"""Historical backtest engine matching paper broker execution semantics.

Enforces Invariant P-01:
  Paper broker and backtester call the SAME simulate_fill() / apply_costs() objects
  (identity, not copies).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from typing import Any

import polars as pl
from ta_core.costs import compute_costs
from ta_core.enums import (
    TREND_FOLLOWING_SLEEVES,
    Action,
    Arm,
    Horizon,
    ProductType,
    Sleeve,
    TrendState,
)
from ta_ledger.broker.base import BarData, Order
from ta_ledger.broker.paper import PaperBroker, apply_costs, simulate_fill
from ta_ledger.events import LedgerEvent
from ta_ledger.manifest import RunManifest
from ta_ledger.risk import (
    DEFAULT,
    Candidate,
    Portfolio,
    Position,
    RiskConfig,
    evaluate_entry,
    regime_caps,
)

from ..barriers import build_path_library, estimate_barriers
from ..pipeline import (
    _BOOK_LEVEL_RULES,
    K_DN,
    K_UP,
    MIN_EXPECTANCY_ATR,
    STOP_ATR,
    TARGET_ATR,
    _ind,
    _pm_conviction,
    _reasons,
    _sleeve,
    breadth_row,
    build_panel,
    industry_trends,
    infer_regime,
    score_universe,
)
from ..stats import BacktestStats, compute_backtest_stats

__all__ = [
    "BacktestResult",
    "_screen_session",
    "apply_costs",
    "orders_from_recommendations",
    "run_backtest",
    "simulate_fill",
]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Historical backtest execution result and performance dossier."""

    arm: Arm
    start_date: date
    end_date: date
    initial_equity: float
    final_equity: float
    equity_curve: list[dict[str, Any]]
    outcomes: list[dict[str, Any]]
    events: tuple[LedgerEvent, ...]
    stats: BacktestStats


def orders_from_recommendations(
    recs: pl.DataFrame | Sequence[Mapping[str, Any]],
    d: date,
    *,
    arm: Arm = Arm.DETERMINISTIC,
    include_watch_for_backtest: bool = False,
    force_entry_on_positive_rr: bool = False,
) -> list[Order]:
    """Convert recommendation records into actionable next-session Orders.

    Shared between live paper execution and historical backtesting to ensure
    identical order placement semantics.
    """
    if isinstance(recs, pl.DataFrame):
        if recs.is_empty():
            return []
        rows = recs.iter_rows(named=True)
    elif isinstance(recs, Sequence):
        rows = list(recs)
    else:
        return []

    orders: list[Order] = []
    for row in rows:
        action = str(row.get("action", ""))
        qty = int(row.get("qty", 0))
        would_qty = int(row.get("would_qty", 0))
        rr = float(row.get("rr", 0.0))

        order_qty = 0
        if action == Action.BUY.value:
            order_qty = qty if qty > 0 else would_qty
        elif include_watch_for_backtest or force_entry_on_positive_rr:
            if would_qty > 0 and (rr > 0.0 or not force_entry_on_positive_rr):
                order_qty = would_qty
            elif qty > 0:
                order_qty = qty

        if order_qty <= 0:
            continue

        sym = str(row["symbol"])
        order_id = str(row.get("rec_id") or f"{d.isoformat()}-{sym}")
        industry = str(row.get("industry", "Unclassified"))
        entry_lo = float(row["entry_lo"])
        entry_hi = float(row["entry_hi"])
        stop = float(row["stop"])
        target = float(row["target"])

        sleeve_val = str(row.get("sleeve", Sleeve.MOMENTUM_BREAKOUT.value))
        try:
            sleeve = Sleeve(sleeve_val)
        except ValueError:
            sleeve = Sleeve.MOMENTUM_BREAKOUT

        horizon_val = str(row.get("horizon", Horizon.SWING.value))
        try:
            horizon = Horizon(horizon_val)
        except ValueError:
            horizon = Horizon.SWING

        arm_val = str(row.get("arm", arm.value if isinstance(arm, Arm) else arm))
        try:
            arm_obj = Arm(arm_val)
        except ValueError:
            arm_obj = arm if isinstance(arm, Arm) else Arm(arm)

        raw_reasons = row.get("reasons", ())
        reasons = tuple(str(r) for r in raw_reasons) if isinstance(raw_reasons, (list, tuple)) else ()

        orders.append(
            Order(
                order_id=order_id,
                symbol=sym,
                industry=industry,
                placed_d=d,
                entry_lo=entry_lo,
                entry_hi=entry_hi,
                stop=stop,
                target=target,
                qty=order_qty,
                horizon=horizon,
                sleeve=sleeve,
                arm=arm_obj,
                reasons=reasons,
            )
        )

    return orders


def _screen_session(
    panel: pl.DataFrame,
    members: pl.DataFrame,
    *,
    d: date,
    portfolio: Portfolio,
    cfg: RiskConfig = DEFAULT,
    tradeable_index: str = "NIFTY500",
    scored_index: str = "NIFTY500",
    max_cards: int = 25,
    lib: pl.DataFrame | None = None,
    ind_tr: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Screen candidates for session d using precomputed factor panel."""
    trade_syms = set(members.filter(pl.col("index_name") == tradeable_index)["symbol"].to_list())
    configured_score_syms = set(members.filter(pl.col("index_name") == scored_index)["symbol"].to_list())
    score_syms = configured_score_syms or trade_syms
    market_index = scored_index if configured_score_syms else tradeable_index

    market_breadth = breadth_row(panel, d, market_index, score_syms)
    regime, _ = infer_regime(market_breadth)
    caps = regime_caps(regime)
    new_cap = int(caps["new"])

    scored = score_universe(panel, d, score_syms)
    if scored.is_empty():
        return pl.DataFrame()

    if lib is not None:
        lib_today = lib.filter(pl.col("d") < d)
    else:
        lib_today = build_path_library(
            panel.filter(pl.col("symbol").is_in(list(trade_syms))),
            horizon=Horizon.SWING,
            k_up=K_UP,
            k_dn=K_DN,
        )

    if ind_tr is None:
        ind_tr = industry_trends(panel, score_syms or trade_syms)
    ind_today: dict[str, dict] = {}
    if not ind_tr.is_empty():
        ind_today = {
            r["industry"]: r
            for r in ind_tr.filter(pl.col("d") == d).iter_rows(named=True)
        }

    recs: list[dict[str, Any]] = []
    alternates: list[dict[str, Any]] = []
    pf = portfolio
    new_entries_today = 0

    candidate_rows: list[dict[str, Any]] = []
    nom_bps = compute_costs(75_000.0, 75_000.0, ProductType.DELIVERY).bps_of(75_000.0)
    top_scored = scored.head(min(80, scored.height)).to_dicts()
    for row in top_scored:
        price, atr = row.get("close"), row.get("atr14")
        if not price or not atr or atr <= 0:
            continue
        est = estimate_barriers(
            lib_today,
            atr_pct=float(atr) / float(price),
            horizon=Horizon.SWING,
            k_up=K_UP,
            k_dn=K_DN,
        )
        if est.p_up is not None and est.p_dn is not None:
            cost_atr_nom = (nom_bps / 10_000.0) * float(price) / float(atr)
            pre_exp = (
                (TARGET_ATR * (est.p_up or 0.0))
                - (STOP_ATR * (est.p_dn or 0.0))
                - cost_atr_nom
            )
        else:
            cost_atr_nom = 0.0
            pre_exp = -999.0
        row["_pre_exp"] = pre_exp
        row["_pm_conviction"] = _pm_conviction(
            row, est, pre_exp, cost_atr_nom, ind_today, None, pf.positions
        )
        candidate_rows.append(row)

    candidate_rows.sort(
        key=lambda r: (
            r["_pre_exp"] >= MIN_EXPECTANCY_ATR,
            float(r.get("_pm_conviction") or 0.0),
        ),
        reverse=True,
    )
    if len(candidate_rows) < max_cards and scored.height > 80:
        for row in scored.slice(80).to_dicts():
            row["_pre_exp"] = -999.0
            candidate_rows.append(row)

    for row in candidate_rows:
        if len(recs) >= max_cards:
            break
        sym = row["symbol"]
        price, atr = row.get("close"), row.get("atr14")
        if not price or not atr or atr <= 0:
            continue

        stop = price - STOP_ATR * atr
        target = price + TARGET_ATR * atr
        sleeve = _sleeve(row)

        if sleeve.value in TREND_FOLLOWING_SLEEVES and row.get("trend_63") in (
            TrendState.UP_DECAYING.value,
            TrendState.DOWN_DECAYING.value,
        ):
            continue
        if (row.get("flips_63d") or 0) >= 6 and sleeve.value in TREND_FOLLOWING_SLEEVES:
            continue

        cand = Candidate(
            symbol=sym,
            industry=row.get("industry") or "Unclassified",
            price=float(price),
            stop=float(stop),
            target=float(target),
            adtv_rupees=float(row.get("adtv_20") or 0.0),
            n_trades=int(row.get("n_trades") or 0),
            listed_sessions=int(row.get("bars_seen") or 0),
            series=str(row.get("series") or "EQ"),
        )

        decision = evaluate_entry(pf, cand, regime=regime, cfg=cfg)
        affordable = evaluate_entry(portfolio, cand, regime=regime, cfg=cfg)

        if not decision.allowed:
            if decision.rule_id in _BOOK_LEVEL_RULES and len(alternates) < max_cards:
                alternates.append({
                    "symbol": sym,
                    "row": row,
                    "cand": cand,
                    "sleeve": sleeve,
                    "blocked_by": decision.rule_id,
                    "detail": decision.detail,
                })
            continue

        buy_turnover = decision.qty * float(price)
        costs = compute_costs(buy_turnover, decision.qty * float(target), ProductType.DELIVERY)
        cost_bps = costs.bps_of(buy_turnover)

        est = estimate_barriers(
            lib_today,
            atr_pct=float(atr) / float(price),
            horizon=Horizon.SWING,
            k_up=K_UP,
            k_dn=K_DN,
        )

        gross_atr = (TARGET_ATR * (est.p_up or 0.0)) - (STOP_ATR * (est.p_dn or 0.0))
        cost_atr = (cost_bps / 10_000.0) * float(price) / float(atr)
        expectancy = gross_atr - cost_atr if est.p_up is not None else 0.0

        tradeable = expectancy >= MIN_EXPECTANCY_ATR
        if tradeable and new_entries_today >= new_cap:
            tradeable = False
            action = Action.WATCH
        elif tradeable:
            new_entries_today += 1
            action = Action.BUY
        else:
            action = Action.WATCH

        recs.append({
            "rec_id": f"{d.isoformat()}-{sym}",
            "d": d,
            "symbol": sym,
            "name": row.get("company_name") or sym,
            "industry": cand.industry,
            "arm": "DETERMINISTIC",
            "action": action.value,
            "sleeve": sleeve.value,
            "horizon": Horizon.SWING.value,
            "entry_lo": round(float(price) * 0.995, 2),
            "entry_hi": round(float(price) * 1.005, 2),
            "stop": round(float(stop), 2),
            "target": round(float(target), 2),
            "qty": decision.qty if tradeable else 0,
            "notional": round(decision.notional, 2) if tradeable else 0.0,
            "would_qty": affordable.qty if affordable.allowed else 0,
            "would_notional": round(affordable.notional, 2) if affordable.allowed else 0.0,
            "would_risk_rupees": round(affordable.risk_rupees, 2) if affordable.allowed else 0.0,
            "afford_note": affordable.detail if affordable.allowed else f"{affordable.rule_id}: {affordable.reason}",
            "risk_rupees": round(decision.risk_rupees, 2) if tradeable else 0.0,
            "risk_pct": round(decision.risk_rupees / pf.equity, 5) if tradeable and pf.equity > 0 else 0.0,
            "rr": round((target - price) / (price - stop), 2) if (price - stop) > 0 else 0.0,
            "atr14": round(float(atr), 4),
            "cost_bps": round(cost_bps, 2),
            "expectancy_atr": round(expectancy, 4),
            "composite": round(float(row.get("composite") or 0.0), 4),
            "reasons": _reasons(row),
            "trend_20": str(row.get("trend_20") or "UNDEFINED"),
            "trend_63": str(row.get("trend_63") or "UNDEFINED"),
            "trend_252": str(row.get("trend_252") or "UNDEFINED"),
            "ind_trend_20": _ind(ind_today, cand.industry, "ind_trend_20"),
            "ind_trend_63": _ind(ind_today, cand.industry, "ind_trend_63"),
            "ind_trend_252": _ind(ind_today, cand.industry, "ind_trend_252"),
            **est.as_dict(),
            "barrier_wide": bool(est.wide),
            "barrier_state": est.state.value,
            "barrier_reason": est.reason,
        })

        if tradeable:
            pf = replace(
                pf,
                cash=pf.cash - decision.notional,
                positions=(
                    *pf.positions,
                    Position(
                        symbol=sym,
                        industry=cand.industry,
                        qty=decision.qty,
                        entry=float(price),
                        last=float(price),
                        stop=float(stop),
                        opened=d,
                    ),
                ),
            )

    return pl.DataFrame(recs) if recs else pl.DataFrame()


def run_backtest(
    bars: pl.DataFrame,
    members: pl.DataFrame,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    initial_equity: float = 500_000.0,
    arm: Arm = Arm.FROZEN_CONTROL,
    tradeable_index: str = "NIFTY500",
    scored_index: str = "NIFTY500",
    max_cards: int = 25,
    cfg: RiskConfig = DEFAULT,
    n_trials: int = 1,
    warmup_sessions: int = 30,
    include_watch_for_backtest: bool = False,
    force_entry_on_positive_rr: bool = False,
) -> BacktestResult:
    """Execute historical backtest over daily bars without lookahead bias.

    Builds the trailing factor panel once, steps through sessions sequentially
    via PaperBroker, and computes deflated risk statistics.
    """
    all_sessions = sorted(bars["d"].unique().to_list())
    if not all_sessions:
        raise ValueError("bars DataFrame has no session dates")

    if warmup_sessions > 0 and len(all_sessions) > warmup_sessions:
        active_sessions = all_sessions[warmup_sessions:]
    else:
        active_sessions = all_sessions

    if start_date is not None:
        active_sessions = [s for s in active_sessions if s >= start_date]
    if end_date is not None:
        active_sessions = [s for s in active_sessions if s <= end_date]

    if not active_sessions:
        raise ValueError("No active sessions to backtest after warmup and date filtering")

    start_d = active_sessions[0]
    end_d = active_sessions[-1]

    # Pre-index bar observations by date and symbol for deterministic simulation
    bars_by_date: dict[date, dict[str, BarData]] = {}
    for row in bars.iter_rows(named=True):
        row_d = row["d"]
        if row_d not in bars_by_date:
            bars_by_date[row_d] = {}
        bars_by_date[row_d][row["symbol"]] = BarData(
            symbol=str(row["symbol"]),
            d=row_d,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )

    # Build the full factor and trend panel once (strictly trailing per symbol)
    panel = build_panel(bars, members)

    trade_syms = set(members.filter(pl.col("index_name") == tradeable_index)["symbol"].to_list())
    configured_score_syms = set(members.filter(pl.col("index_name") == scored_index)["symbol"].to_list())
    score_syms = configured_score_syms or trade_syms

    # Precompute industry trends once across the panel
    precomputed_ind_tr = industry_trends(panel, score_syms or trade_syms)

    # Precompute path library once across tradeable universe
    trade_panel = panel.filter(pl.col("symbol").is_in(list(trade_syms)))
    precomputed_lib = build_path_library(
        trade_panel,
        horizon=Horizon.SWING,
        k_up=K_UP,
        k_dn=K_DN,
    )

    arm_obj = arm if isinstance(arm, Arm) else Arm(arm)
    broker = PaperBroker(initial_equity=initial_equity, arm=arm_obj)
    run_id = f"BT-{arm_obj.value}"
    manifest = RunManifest(
        run_id=run_id,
        d=start_d,
        as_of=datetime.combine(start_d, time(15, 30), tzinfo=UTC),
        arm=arm_obj.value,
        git_sha="BACKTEST",
        uv_lock_sha="BACKTEST",
        weights_sha="BACKTEST",
        config_sha="BACKTEST",
    )
    broker.record_manifest(manifest)

    for d in active_sessions:
        session_ts = datetime.combine(d, time(15, 30), tzinfo=UTC)
        bars_today = bars_by_date.get(d, {})

        # Step 1: Morning/Intraday execution on session d
        broker.step_session(d, bars_today, run_id=run_id, ts=session_ts)

        # Step 2: Evening screen after session d close
        panel_upto_d = panel.filter(pl.col("d") <= d)
        recs_df = _screen_session(
            panel_upto_d,
            members,
            d=d,
            portfolio=broker.portfolio(),
            cfg=cfg,
            tradeable_index=tradeable_index,
            scored_index=scored_index,
            max_cards=max_cards,
            lib=precomputed_lib,
            ind_tr=precomputed_ind_tr,
        )

        if not recs_df.is_empty():
            orders = orders_from_recommendations(
                recs_df,
                d,
                arm=arm_obj,
                include_watch_for_backtest=include_watch_for_backtest,
                force_entry_on_positive_rr=force_entry_on_positive_rr,
            )
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
    )
