"""Database I/O for the nightly run. The pipeline itself stays pure.

Separated on purpose: `pipeline.run_nightly` takes frames and returns frames,
so it is testable without a database, and every read here goes through the PIT
guard with an explicit as-of rather than `now()`.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, date, datetime, time
from typing import Any

import polars as pl
from ta_core.asof import AsOf
from ta_core.costs import compute_costs
from ta_core.enums import Arm, ProductType
from ta_core.timefmt import IST
from ta_datafeed.adjust import apply_adjustments, infer_corporate_actions
from ta_datafeed.sources.surveillance import load_surveillance_symbols
from ta_datafeed.store import GuardedClient
from ta_ledger.events import GENESIS_HASH, EventKind, LedgerEvent, events_to_df
from ta_ledger.risk import DEFAULT, Portfolio, Position

from .pipeline import RunResult, run_nightly

log = logging.getLogger(__name__)

__all__ = [
    "as_of_for",
    "current_portfolio",
    "execute_backtest",
    "execute_nightly",
    "latest_session",
    "load_panel_inputs",
    "persist",
    "resolve_past_recommendations",
    "step_paper_book",
]

#: The bhavcopy lands ~18:30 IST. Any as-of earlier than this on the same day
#: cannot legitimately see that day's bar.
PUBLISH_IST = time(18, 30)

#: How much history the factor stack needs before its longest window (252)
#: produces anything. Two years of sessions comfortably covers it.
LOOKBACK_SESSIONS = 420


def as_of_for(d: date) -> AsOf:
    """The decision instant for a REPLAY of session `d`: 18:30 IST, in UTC.

    >>> as_of_for(date(2026, 10, 30)).sql()
    "'2026-10-30 13:00:00'"

    Note what this is NOT. For a live run the decision instant is *now*, not
    the session's publish time: deciding on Monday evening from Friday's close
    legitimately sees everything learned over the weekend, including an index
    membership snapshot taken this morning. Conflating the two was the first
    bug this separation caught -- a live run found zero index members, because
    today's snapshot is correctly invisible to Friday's as-of.
    """
    return AsOf(datetime.combine(d, PUBLISH_IST, tzinfo=IST).astimezone(UTC))


def latest_session(ch: GuardedClient) -> date:
    """Most recent session present in the store."""
    df = ch.query_df("SELECT max(d) AS d FROM md.bars_1d WHERE knowledge_time <= '2099-01-01 00:00:00'")
    if not df.height or df["d"][0] is None or df["d"][0] <= date(1970, 1, 2):
        raise RuntimeError("no market data ingested in md.bars_1d")
    return df["d"][0]


def load_panel_inputs(
    ch: GuardedClient, d: date, *, a: AsOf, lookback: int = LOOKBACK_SESSIONS
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Bars and dated index membership, both PIT-bounded at `a`.

    `d` bounds the BAR DATE; `a` bounds what was KNOWABLE. They are different
    axes and the plan is emphatic about it: knowledge_time != event date.
    """
    start = ch.query_df(
        f"SELECT min(d) AS d FROM (SELECT DISTINCT d FROM md.bars_1d "
        f"WHERE knowledge_time <= {a.sql()} ORDER BY d DESC LIMIT {lookback})",
        a,
    )["d"][0]

    bars = ch.query_pit(
        "md.bars_1d", a,
        columns="d, symbol, series, open, high, low, close, prev_close, "
                "volume, turnover, n_trades",
        where=f"d >= '{start}' AND d <= '{d}'",
        order_by="symbol, d",
    )
    # ReplacingMergeTree collapses duplicates only on a background merge whose
    # timing is not guaranteed, so re-ingesting a session leaves two rows for
    # (symbol, d) until then. That is not a cosmetic problem here: a duplicated
    # bar shifts every trailing window by one, so SMA20 silently becomes a
    # 20-row average of 19 distinct sessions and the whole factor stack is
    # quietly wrong. Dedupe on read rather than trusting the engine's timing.
    before = bars.height
    bars = bars.unique(subset=["symbol", "d"], keep="last").sort(["symbol", "d"])
    if bars.height != before:
        log.warning("dropped %d duplicate bar rows (unmerged ReplacingMergeTree parts)",
                    before - bars.height)

    # Infer corporate actions and merge stored corporate actions at `a`
    inferred_actions = infer_corporate_actions(bars)
    try:
        stored_actions = ch.query_pit(
            "md.corporate_actions", a,
            columns="symbol, ex_date, action_type, ratio_from, ratio_to, adj_factor, dividend, knowledge_time",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("md.corporate_actions read failed (%s); using inferred only", e)
        stored_actions = pl.DataFrame()

    if not stored_actions.is_empty():
        if not inferred_actions.is_empty():
            actions = pl.concat([inferred_actions, stored_actions]).unique(
                subset=["symbol", "ex_date"], keep="last"
            )
        else:
            actions = stored_actions
    else:
        actions = inferred_actions

    if not actions.is_empty():
        bars = apply_adjustments(bars, actions, as_of=a)

    members = ch.query_pit(
        "md.index_members", a,
        columns="index_name, symbol, company_name, industry, snapshot_date",
    )
    # Keep only the latest snapshot at or before `d` for each (index, symbol):
    # this is what makes membership DATED rather than current.
    if members.height:
        members = (
            members.sort("snapshot_date")
            .group_by(["index_name", "symbol"])
            .last()
        )
    return bars, members


def current_portfolio(ch: GuardedClient, *, equity: float = 500_000.0) -> Portfolio:
    """Rebuild the open book. Empty on a fresh install, which is the honest
    starting state -- not a simulated history."""
    try:
        pos_df = ch.query_df(
            "SELECT as_of, symbol, industry, qty, entry, last, stop, target, opened, horizon, sleeve, status, pnl "
            "FROM ev.positions"
        )
        if not pos_df.is_empty():
            pos_df = (
                pos_df.sort("as_of")
                .unique(subset=["symbol", "opened"], keep="last")
                .filter(pl.col("status") == "OPEN")
            )
    except Exception as e:  # noqa: BLE001 - a missing table must not kill the run
        log.warning("positions unavailable (%s); starting flat", e)
        pos_df = pl.DataFrame()

    realised_pnl = 0.0
    try:
        out_df = ch.query_df(
            "SELECT coalesce(sum(pnl_rupees), 0.0) AS realised FROM ev.outcomes "
            "WHERE arm = 'DETERMINISTIC' AND knowledge_time <= '2099-01-01 00:00:00'"
        )
        if not out_df.is_empty():
            realised_pnl = float(out_df["realised"][0] or 0.0)
    except Exception as e:  # noqa: BLE001
        log.debug("outcomes query unavailable: %s", e)

    items = tuple(
        Position(
            symbol=str(r["symbol"]),
            industry=str(r["industry"]),
            qty=int(r["qty"]),
            entry=float(r["entry"]),
            last=float(r["last"]),
            stop=float(r["stop"]),
            opened=r["opened"],
            target=float(r.get("target") or 0.0),
            horizon=str(r.get("horizon") or "SWING"),
        )
        for r in pos_df.iter_rows(named=True)
    ) if not pos_df.is_empty() else ()

    cost_basis = sum(p.entry * p.qty for p in items)
    deployed = sum(p.last * p.qty for p in items)
    cash = (equity + realised_pnl) - cost_basis
    total_equity = cash + deployed
    return Portfolio(
        equity=total_equity,
        cash=cash,
        positions=items,
        watermark=max(equity, total_equity),
    )


#: ADR-0004: the index regime + REG-01 bear block was tested out of sample
#: (2021-02 .. 2025-07) and in sample (2025-07 .. 2026-11) and was WORSE in
#: both (Sharpe 0.04 vs 0.09 OOS; -1.38 vs -0.97 IS). The death-cross BEAR
#: state fires after the fall, so it blocks recovery entries. So the live arm
#: keeps the breadth regime for gating; the index regime is computed and
#: recorded nightly as INFORMATION only. Flip this only with a new ADR and a
#: rule that beats the control out of sample.
REG01_ENABLED = False


#: ADR-0005: every strategy -- the frozen control, the live arm, and any model
#: added later -- is judged against this index as well as against the control.
#: Nifty 500 because it IS the universe traded; price index because the free
#: daily archive publishes no total-return series (dividends ~1.2%/yr missing,
#: so beating it narrowly is a tie).
BENCHMARK_INDEX = "Nifty 500"


def benchmark_return(ch: GuardedClient, a: AsOf, start: date, end: date) -> float | None:
    """Benchmark return over a backtest's dates, measured like the strategy's.

    The strategy's total return runs from starting capital BEFORE its first
    session to the last mark, so the index runs from the close of the session
    before `start` to the close of `end`. None if the index is missing there.
    """
    try:
        idx = ch.query_pit(
            "md.index_1d", a, columns="d, close, knowledge_time",
            where=f"index_name = '{BENCHMARK_INDEX}' AND d <= '{end}'", order_by="d",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("could not load benchmark: %s", e)
        return None
    if idx.is_empty():
        return None
    idx = idx.sort("knowledge_time").unique(subset=["d"], keep="last").sort("d")
    before = idx.filter(pl.col("d") < start).tail(1)
    first = before if not before.is_empty() else idx.filter(pl.col("d") >= start).head(1)
    last = idx.filter(pl.col("d") <= end).tail(1)
    if first.is_empty() or last.is_empty() or float(first["close"][0]) <= 0:
        return None
    return float(last["close"][0]) / float(first["close"][0]) - 1.0


def load_index_regime(ch: GuardedClient, d: date, a: AsOf) -> pl.DataFrame:
    """Regime series for the live arm, from index bars knowable at `a` (PIT).

    Empty when md.index_1d has no rows yet; `regime_on` then answers UNKNOWN,
    whose caps are the conservative TREND_DOWN ones -- an unmeasured market is
    treated as a dangerous one, but REG-01's hard block needs an actual BEAR.
    """
    from .regime import REGIME_INDEX, regime_series

    try:
        idx = ch.query_pit(
            "md.index_1d", a, columns="d, index_name, open, high, low, close",
            where=f"index_name = '{REGIME_INDEX}' AND d <= '{d}'", order_by="d",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("could not load index bars for regime: %s", e)
        return regime_series(pl.DataFrame())
    return regime_series(idx) if not idx.is_empty() else regime_series(pl.DataFrame())


def _persist_regime(ch: GuardedClient, series: pl.DataFrame, d: date, a: AsOf) -> None:
    from .regime import REGIME_INDEX

    row = series.filter(pl.col("d") == d)
    if row.is_empty():
        return
    try:
        ch.insert_df("md.regime_daily", row.select(
            "d", pl.lit("INDEX_RULES_V1").alias("model"), pl.lit(REGIME_INDEX).alias("index_name"),
            "regime", "raw_regime", "note", "sma50", "sma200", "vol20", "natr14",
            pl.lit(a.ts).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time"),
        ))
    except Exception as e:  # noqa: BLE001
        log.warning("could not persist regime: %s", e)


def execute_nightly(
    d: date | None = None, *, equity: float = 500_000.0, replay: bool = False
) -> RunResult:
    """Run tonight's screen.

    `replay=True` pins the decision instant to the session's own 18:30 IST, so
    a past day is reconstructed with only what was knowable then. The default
    is a LIVE run, whose decision instant is now.
    """
    with GuardedClient() as ch:
        day = d or latest_session(ch)
        a = as_of_for(day) if replay else AsOf(datetime.now(UTC))
        bars, members = load_panel_inputs(ch, day, a=a)
        log.info("as_of=%s bars=%d members=%d session=%s",
                 a.sql(), bars.height, members.height, day)
        pf = current_portfolio(ch, equity=equity)
        surv_syms, _ = load_surveillance_symbols(ch, day, a)
        n_sessions = int(bars["d"].n_unique()) if not bars.is_empty() else 0
        cfg = (
            replace(DEFAULT, min_listed_sessions=max(20, int(n_sessions * 0.9)))
            if 0 < n_sessions < DEFAULT.min_listed_sessions
            else DEFAULT
        )
        index_regime = load_index_regime(ch, day, a)
        res = run_nightly(
            bars,
            members,
            d=day,
            portfolio=pf,
            cfg=cfg,
            as_of=a.ts,
            surveillance_symbols=surv_syms,
            index_regime=index_regime if REG01_ENABLED else None,
        )
        persist(ch, res)
        if not replay:
            _persist_regime(ch, index_regime, day, a)
        try:
            step_paper_book(ch, res, bars, auto_buy=True)
        except Exception as e:  # noqa: BLE001
            log.error("step_paper_book failed: %s", e)
        return res


def resolve_past_recommendations(
    ch: GuardedClient, bars: pl.DataFrame, d: date, as_of: datetime
) -> int:
    """Evaluate past BUY and WATCH recommendations against historical paths to record outcomes.

    Enables tracking of all recommendations (including unexecuted BUYs and WATCH candidates)
    for model calibration and learning feedback loop.
    """
    try:
        recs_df = ch.query_df(
            f"SELECT as_of, rec_id, d, symbol, arm, action, horizon, entry_lo, entry_hi, stop, target, qty, would_qty, p_up "
            f"FROM ev.recommendations "
            f"WHERE d < '{d}' AND action IN ('BUY', 'WATCH') AND knowledge_time <= '2099-01-01 00:00:00'"
        )
    except Exception as e:  # noqa: BLE001
        log.warning("could not query ev.recommendations: %s", e)
        return 0

    if recs_df.is_empty():
        return 0

    recs_df = recs_df.sort("as_of").unique(subset=["rec_id"], keep="last")

    try:
        resolved_df = ch.query_df("SELECT rec_id FROM ev.outcomes WHERE knowledge_time <= '2099-01-01 00:00:00'")
        resolved_ids = set(resolved_df["rec_id"].to_list()) if not resolved_df.is_empty() else set()
    except Exception as e:  # noqa: BLE001
        log.warning("could not query ev.outcomes: %s", e)
        resolved_ids = set()

    unresolved_recs = [
        r for r in recs_df.iter_rows(named=True)
        if r["rec_id"] not in resolved_ids
    ]
    if not unresolved_recs:
        return 0

    outcomes_to_insert: list[dict[str, Any]] = []
    kt = as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of.astimezone(UTC)

    for rec in unresolved_recs:
        rec_d = rec["d"] if isinstance(rec["d"], date) else datetime.strptime(str(rec["d"]), "%Y-%m-%d").date()
        sub_bars = (
            bars.filter(
                (pl.col("symbol") == rec["symbol"])
                & (pl.col("d") > rec_d)
                & (pl.col("d") <= d)
            )
            .sort("d")
        )
        if sub_bars.is_empty():
            continue

        entry_price = round((float(rec["entry_lo"]) + float(rec["entry_hi"])) / 2.0, 2)
        stop = float(rec["stop"])
        target = float(rec.get("target") or 0.0)
        horizon_str = str(rec.get("horizon") or "SWING")
        max_sessions = 60 if horizon_str == "POSITIONAL" else (1 if horizon_str == "INTRADAY" else 10)

        min_low = float("inf")
        max_high = float("-inf")
        resolved = False
        barrier_hit = ""
        exit_price = 0.0
        exit_date = None
        holding_days = 0

        for idx, b in enumerate(sub_bars.iter_rows(named=True), 1):
            low_b = float(b["low"])
            high_b = float(b["high"])
            close_b = float(b["close"])
            min_low = min(min_low, low_b)
            max_high = max(max_high, high_b)

            if low_b <= stop:
                barrier_hit = "DOWN"
                exit_price = stop
                exit_date = b["d"]
                holding_days = idx
                resolved = True
                break
            elif target > 0 and high_b >= target:
                barrier_hit = "UP"
                exit_price = target
                exit_date = b["d"]
                holding_days = idx
                resolved = True
                break
            elif idx >= max_sessions:
                barrier_hit = "VERTICAL"
                exit_price = close_b
                exit_date = b["d"]
                holding_days = idx
                resolved = True
                break

        if resolved and exit_date is not None:
            risk_unit = max(entry_price - stop, 0.01)
            r_multiple = round((exit_price - entry_price) / risk_unit, 4)
            mae_r = round((min_low - entry_price) / risk_unit, 4)
            mfe_r = round((max_high - entry_price) / risk_unit, 4)
            eval_qty = int(rec.get("qty") or rec.get("would_qty") or 1)
            costs = compute_costs(entry_price * eval_qty, exit_price * eval_qty, ProductType.DELIVERY).total
            pnl_rupees = round((exit_price - entry_price) * eval_qty - costs, 2)
            arm = Arm.SHADOW_BUY.value if rec.get("action") == "BUY" else Arm.SHADOW_WATCH.value
            p_up = float(rec.get("p_up") or 0.0)
            exit_d = exit_date if isinstance(exit_date, date) else datetime.strptime(str(exit_date), "%Y-%m-%d").date()

            outcomes_to_insert.append({
                "rec_id": str(rec["rec_id"]),
                "symbol": str(rec["symbol"]),
                "arm": arm,
                "horizon": horizon_str,
                "entry_date": rec_d,
                "entry_price": entry_price,
                "exit_date": exit_d,
                "exit_price": exit_price,
                "barrier_hit": barrier_hit,
                "r_multiple": r_multiple,
                "pnl_rupees": pnl_rupees,
                "mae_r": mae_r,
                "mfe_r": mfe_r,
                "holding_days": max(0, holding_days),
                "benchmark_ret": 0.0,
                "stated_conf": p_up,
                "calibrated_p": p_up,
                "uniqueness_w": 1.0,
                "regime": "NORMAL",
                "degraded_mode": 0,
                "knowledge_time": kt,
            })

    if outcomes_to_insert:
        out_df = pl.DataFrame(outcomes_to_insert, schema={
            "rec_id": pl.Utf8,
            "symbol": pl.Utf8,
            "arm": pl.Utf8,
            "horizon": pl.Utf8,
            "entry_date": pl.Date,
            "entry_price": pl.Float64,
            "exit_date": pl.Date,
            "exit_price": pl.Float64,
            "barrier_hit": pl.Utf8,
            "r_multiple": pl.Float32,
            "pnl_rupees": pl.Float64,
            "mae_r": pl.Float32,
            "mfe_r": pl.Float32,
            "holding_days": pl.UInt16,
            "benchmark_ret": pl.Float32,
            "stated_conf": pl.Float32,
            "calibrated_p": pl.Float32,
            "uniqueness_w": pl.Float32,
            "regime": pl.Utf8,
            "degraded_mode": pl.UInt8,
            "knowledge_time": pl.Datetime("ms", "UTC"),
        })
        try:
            ch.insert_df("ev.outcomes", out_df)
        except Exception as e:  # noqa: BLE001
            log.warning("could not insert resolved recommendation outcomes: %s", e)
            return 0
        return len(outcomes_to_insert)

    return 0


def step_paper_book(
    ch: GuardedClient, res: RunResult, bars: pl.DataFrame, *, auto_buy: bool = True
) -> None:
    """Advance the paper trading book: mark MTM, resolve exits on daily bars,
    auto-enter BUY recommendations, and record equity daily snapshot."""
    # 0. Read existing ev.ledger_events for hash-chain continuity
    try:
        events_df = ch.query_df(
            "SELECT seq, ts, run_id, kind, payload, prev_hash, hash "
            "FROM ev.ledger_events ORDER BY seq ASC"
        )
        if not events_df.is_empty():
            curr_seq = int(events_df["seq"].max()) + 1
            curr_prev_hash = str(events_df["hash"][-1])
        else:
            curr_seq = 1
            curr_prev_hash = GENESIS_HASH
    except Exception:  # noqa: BLE001
        curr_seq = 1
        curr_prev_hash = GENESIS_HASH

    # 1. Load current open positions (deduped by symbol, opened)
    try:
        pos_df = ch.query_df(
            "SELECT as_of, symbol, industry, qty, entry, last, stop, target, opened, horizon, sleeve, status, pnl "
            "FROM ev.positions"
        )
        if not pos_df.is_empty():
            open_pos = (
                pos_df.sort("as_of")
                .unique(subset=["symbol", "opened"], keep="last")
                .filter(pl.col("status") == "OPEN")
                .to_dicts()
            )
        else:
            open_pos = []
    except Exception as e:  # noqa: BLE001
        log.warning("could not read positions: %s", e)
        open_pos = []

    rec_map = (
        {r["symbol"]: r for r in res.recommendations.iter_rows(named=True)}
        if not res.recommendations.is_empty()
        else {}
    )

    # Step 1: Mark-to-market & Stop/Target exits on res.d
    exited_symbols: set[str] = set()
    for pos in open_pos:
        symbol = str(pos["symbol"])
        bars_today = bars.filter((pl.col("d") == res.d) & (pl.col("symbol") == symbol))
        if bars_today.is_empty():
            continue
        bar = bars_today.to_dicts()[0]
        bar_low = float(bar["low"])
        bar_high = float(bar["high"])
        bar_close = float(bar["close"])
        pos_stop = float(pos["stop"])
        pos_target = float(pos.get("target") or 0.0)
        pos_entry = float(pos["entry"])
        pos_qty = int(pos["qty"])
        rec = rec_map.get(symbol)

        opened_d = (
            pos["opened"]
            if isinstance(pos["opened"], date)
            else datetime.strptime(str(pos["opened"]), "%Y-%m-%d").date()
        )
        holding_days = (res.d - opened_d).days

        exited = False
        exit_price = 0.0
        barrier_hit = ""

        # Same-bar precedence: stop first (pessimistic, Invariant I-14)
        if bar_low <= pos_stop:
            exited = True
            exit_price = pos_stop
            barrier_hit = "DOWN"
        elif pos_target > 0 and bar_high >= pos_target:
            exited = True
            exit_price = pos_target
            barrier_hit = "UP"
        elif rec and rec.get("action") == "SELL":
            exited = True
            exit_price = bar_close
            afford_note = str(rec.get("afford_note") or "")
            if "TIME_STOP" in afford_note:
                barrier_hit = "VERTICAL"
            elif "TARGET_REACHED" in afford_note:
                barrier_hit = "UP"
            elif "ROTATE" in afford_note:
                barrier_hit = "ROTATE"
            elif "STOP_DISCIPLINE" in afford_note:
                barrier_hit = "DOWN"
            elif "TREND_REVERSAL" in afford_note:
                barrier_hit = "SIGNAL_SELL"
            else:
                barrier_hit = "SIGNAL_SELL"
        elif holding_days >= 14:
            exited = True
            exit_price = bar_close
            barrier_hit = "VERTICAL"

        if exited:
            costs = compute_costs(pos_entry * pos_qty, exit_price * pos_qty, ProductType.DELIVERY).total
            pnl_rupees = round((exit_price - pos_entry) * pos_qty - costs, 2)

            pos_close_df = pl.DataFrame([{
                "as_of": res.as_of,
                "symbol": symbol,
                "industry": str(pos.get("industry") or ""),
                "qty": pos_qty,
                "entry": pos_entry,
                "last": exit_price,
                "stop": pos_stop,
                "target": pos_target,
                "opened": pos["opened"],
                "horizon": str(pos.get("horizon") or "SWING"),
                "sleeve": str(pos.get("sleeve") or "MOMENTUM_BREAKOUT"),
                "status": "CLOSED",
                "pnl": pnl_rupees,
            }], schema={
                "as_of": pl.Datetime("ms", "UTC"),
                "symbol": pl.Utf8,
                "industry": pl.Utf8,
                "qty": pl.UInt32,
                "entry": pl.Float64,
                "last": pl.Float64,
                "stop": pl.Float64,
                "target": pl.Float64,
                "opened": pl.Date,
                "horizon": pl.Utf8,
                "sleeve": pl.Utf8,
                "status": pl.Utf8,
                "pnl": pl.Float64,
            })
            ch.insert_df("ev.positions", pos_close_df)

            risk_unit = (pos_entry - pos_stop) if (pos_entry - pos_stop) > 0 else 1.0
            r_multiple = round((exit_price - pos_entry) / risk_unit, 4)
            opened_d = pos["opened"] if isinstance(pos["opened"], date) else datetime.strptime(str(pos["opened"]), "%Y-%m-%d").date()
            holding_days = (res.d - opened_d).days

            outcome_df = pl.DataFrame([{
                "rec_id": f"P-{symbol}-{res.d}",
                "symbol": symbol,
                "arm": "DETERMINISTIC",
                "horizon": str(pos.get("horizon") or "SWING"),
                "entry_date": opened_d,
                "entry_price": pos_entry,
                "exit_date": res.d,
                "exit_price": exit_price,
                "barrier_hit": barrier_hit,
                "r_multiple": r_multiple,
                "pnl_rupees": pnl_rupees,
                "mae_r": 0.0,
                "mfe_r": 0.0,
                "holding_days": max(0, holding_days),
                "benchmark_ret": 0.0,
                "stated_conf": 0.0,
                "calibrated_p": 0.0,
                "uniqueness_w": 1.0,
                "regime": res.regime.value if hasattr(res.regime, "value") else str(res.regime),
                "degraded_mode": 0,
                "knowledge_time": res.as_of,
            }], schema={
                "rec_id": pl.Utf8,
                "symbol": pl.Utf8,
                "arm": pl.Utf8,
                "horizon": pl.Utf8,
                "entry_date": pl.Date,
                "entry_price": pl.Float64,
                "exit_date": pl.Date,
                "exit_price": pl.Float64,
                "barrier_hit": pl.Utf8,
                "r_multiple": pl.Float32,
                "pnl_rupees": pl.Float64,
                "mae_r": pl.Float32,
                "mfe_r": pl.Float32,
                "holding_days": pl.UInt16,
                "benchmark_ret": pl.Float32,
                "stated_conf": pl.Float32,
                "calibrated_p": pl.Float32,
                "uniqueness_w": pl.Float32,
                "regime": pl.Utf8,
                "degraded_mode": pl.UInt8,
                "knowledge_time": pl.Datetime("ms", "UTC"),
            })
            ch.insert_df("ev.outcomes", outcome_df)

            event = LedgerEvent.create(
                seq=curr_seq,
                ts=res.as_of,
                run_id=res.run_id,
                kind=EventKind.EXIT_FILL,
                payload={
                    "d": str(res.d),
                    "symbol": symbol,
                    "side": "SELL",
                    "qty": pos_qty,
                    "price": exit_price,
                    "notional": round(pos_qty * exit_price, 2),
                    "cost_rupees": round(costs, 2),
                    "pnl_rupees": pnl_rupees,
                    "barrier_hit": barrier_hit,
                    "note": f"paper position closed on {barrier_hit}",
                },
                prev_hash=curr_prev_hash,
            )
            ch.insert_df("ev.ledger_events", events_to_df([event]))
            curr_seq += 1
            curr_prev_hash = event.hash
            exited_symbols.add(symbol)
        else:
            effective_stop = pos_stop
            if rec and rec.get("action") == "HOLD":
                rec_stop = float(rec.get("stop") or 0.0)
                if rec_stop > pos_stop:
                    event = LedgerEvent.create(
                        seq=curr_seq,
                        ts=res.as_of,
                        run_id=res.run_id,
                        kind=EventKind.STOP_TRAILED,
                        payload={
                            "d": str(res.d),
                            "symbol": symbol,
                            "prev_stop": pos_stop,
                            "new_stop": rec_stop,
                            "note": f"trailing stop raised to {rec_stop}",
                        },
                        prev_hash=curr_prev_hash,
                    )
                    ch.insert_df("ev.ledger_events", events_to_df([event]))
                    curr_seq += 1
                    curr_prev_hash = event.hash
                    effective_stop = rec_stop

            if bar_close != float(pos["last"]) or effective_stop != pos_stop:
                pos_update_df = pl.DataFrame([{
                    "as_of": res.as_of,
                    "symbol": symbol,
                    "industry": str(pos.get("industry") or ""),
                    "qty": pos_qty,
                    "entry": pos_entry,
                    "last": bar_close,
                    "stop": effective_stop,
                    "target": pos_target,
                    "opened": pos["opened"],
                    "horizon": str(pos.get("horizon") or "SWING"),
                    "sleeve": str(pos.get("sleeve") or "MOMENTUM_BREAKOUT"),
                    "status": "OPEN",
                    "pnl": round((bar_close - pos_entry) * pos_qty, 2),
                }], schema={
                    "as_of": pl.Datetime("ms", "UTC"),
                    "symbol": pl.Utf8,
                    "industry": pl.Utf8,
                    "qty": pl.UInt32,
                    "entry": pl.Float64,
                    "last": pl.Float64,
                    "stop": pl.Float64,
                    "target": pl.Float64,
                    "opened": pl.Date,
                    "horizon": pl.Utf8,
                    "sleeve": pl.Utf8,
                    "status": pl.Utf8,
                    "pnl": pl.Float64,
                })
                ch.insert_df("ev.positions", pos_update_df)

    # Step 1.5: Process SELL recommendation cards on currently open positions (e.g. ROTATE_WEAKEST, STOP_DISCIPLINE)
    if not res.recommendations.is_empty():
        sell_recs = {
            r["symbol"]: r
            for r in res.recommendations.iter_rows(named=True)
            if r.get("action") == "SELL"
        }
        for pos in open_pos:
            symbol = str(pos["symbol"])
            if symbol in sell_recs and symbol not in exited_symbols:
                rec = sell_recs[symbol]
                pos_qty = int(pos["qty"])
                pos_entry = float(pos["entry"])
                exit_price = float(pos["last"])
                pos_stop = float(pos["stop"])
                afford_note = str(rec.get("afford_note") or "")
                if "TIME_STOP" in afford_note:
                    barrier_hit = "VERTICAL"
                elif "TARGET_REACHED" in afford_note:
                    barrier_hit = "UP"
                elif "ROTATE" in afford_note:
                    barrier_hit = "ROTATE"
                elif "STOP_DISCIPLINE" in afford_note:
                    barrier_hit = "DOWN"
                elif "TREND_REVERSAL" in afford_note:
                    barrier_hit = "SIGNAL_SELL"
                else:
                    barrier_hit = "SIGNAL_SELL"

                costs = compute_costs(pos_entry * pos_qty, exit_price * pos_qty, ProductType.DELIVERY).total
                pnl_rupees = round((exit_price - pos_entry) * pos_qty - costs, 2)

                pos_close_df = pl.DataFrame([{
                    "as_of": res.as_of,
                    "symbol": symbol,
                    "industry": str(pos.get("industry") or ""),
                    "qty": pos_qty,
                    "entry": pos_entry,
                    "last": exit_price,
                    "stop": pos_stop,
                    "target": float(pos.get("target") or 0.0),
                    "opened": pos["opened"],
                    "horizon": str(pos.get("horizon") or "SWING"),
                    "sleeve": str(pos.get("sleeve") or "MOMENTUM_BREAKOUT"),
                    "status": "CLOSED",
                    "pnl": pnl_rupees,
                }], schema={
                    "as_of": pl.Datetime("ms", "UTC"),
                    "symbol": pl.Utf8,
                    "industry": pl.Utf8,
                    "qty": pl.UInt32,
                    "entry": pl.Float64,
                    "last": pl.Float64,
                    "stop": pl.Float64,
                    "target": pl.Float64,
                    "opened": pl.Date,
                    "horizon": pl.Utf8,
                    "sleeve": pl.Utf8,
                    "status": pl.Utf8,
                    "pnl": pl.Float64,
                })
                ch.insert_df("ev.positions", pos_close_df)

                risk_unit = (pos_entry - pos_stop) if (pos_entry - pos_stop) > 0 else 1.0
                r_multiple = round((exit_price - pos_entry) / risk_unit, 4)
                opened_d = pos["opened"] if isinstance(pos["opened"], date) else datetime.strptime(str(pos["opened"]), "%Y-%m-%d").date()
                holding_days = (res.d - opened_d).days

                outcome_df = pl.DataFrame([{
                    "rec_id": f"P-{symbol}-{res.d}",
                    "symbol": symbol,
                    "arm": "DETERMINISTIC",
                    "horizon": str(pos.get("horizon") or "SWING"),
                    "entry_date": opened_d,
                    "entry_price": pos_entry,
                    "exit_date": res.d,
                    "exit_price": exit_price,
                    "barrier_hit": barrier_hit,
                    "r_multiple": r_multiple,
                    "pnl_rupees": pnl_rupees,
                    "mae_r": 0.0,
                    "mfe_r": 0.0,
                    "holding_days": max(0, holding_days),
                    "benchmark_ret": 0.0,
                    "stated_conf": 0.0,
                    "calibrated_p": 0.0,
                    "uniqueness_w": 1.0,
                    "regime": res.regime.value if hasattr(res.regime, "value") else str(res.regime),
                    "degraded_mode": 0,
                    "knowledge_time": res.as_of,
                }], schema={
                    "rec_id": pl.Utf8,
                    "symbol": pl.Utf8,
                    "arm": pl.Utf8,
                    "horizon": pl.Utf8,
                    "entry_date": pl.Date,
                    "entry_price": pl.Float64,
                    "exit_date": pl.Date,
                    "exit_price": pl.Float64,
                    "barrier_hit": pl.Utf8,
                    "r_multiple": pl.Float32,
                    "pnl_rupees": pl.Float64,
                    "mae_r": pl.Float32,
                    "mfe_r": pl.Float32,
                    "holding_days": pl.UInt16,
                    "benchmark_ret": pl.Float32,
                    "stated_conf": pl.Float32,
                    "calibrated_p": pl.Float32,
                    "uniqueness_w": pl.Float32,
                    "regime": pl.Utf8,
                    "degraded_mode": pl.UInt8,
                    "knowledge_time": pl.Datetime("ms", "UTC"),
                })
                ch.insert_df("ev.outcomes", outcome_df)

                event = LedgerEvent.create(
                    seq=curr_seq,
                    ts=res.as_of,
                    run_id=res.run_id,
                    kind=EventKind.EXIT_FILL,
                    payload={
                        "d": str(res.d),
                        "symbol": symbol,
                        "side": "SELL",
                        "qty": pos_qty,
                        "price": exit_price,
                        "notional": round(pos_qty * exit_price, 2),
                        "cost_rupees": round(costs, 2),
                        "pnl_rupees": pnl_rupees,
                        "barrier_hit": barrier_hit,
                        "note": f"auto-exit on recommendation: {afford_note or barrier_hit}",
                    },
                    prev_hash=curr_prev_hash,
                )
                ch.insert_df("ev.ledger_events", events_to_df([event]))
                curr_seq += 1
                curr_prev_hash = event.hash
                exited_symbols.add(symbol)

    # Step 1.8: Resolve past recommendations against historical paths for learning feedback loop
    try:
        resolved_count = resolve_past_recommendations(ch, bars, res.d, res.as_of)
        if resolved_count:
            log.info("resolved %d past recommendations on %s", resolved_count, res.d)
    except Exception as e:  # noqa: BLE001
        log.warning("could not resolve past recommendations: %s", e)

    # Step 2: Auto-enter tonight's BUY recommendations when auto_buy=True
    if auto_buy and not res.recommendations.is_empty():
        from ta_ledger.risk import regime_caps
        max_new_entries = int(regime_caps(res.regime)["new"])
        try:
            pos_df2 = ch.query_df("SELECT as_of, symbol, opened, status FROM ev.positions")
            if not pos_df2.is_empty():
                open_df2 = (
                    pos_df2.sort("as_of")
                    .unique(subset=["symbol", "opened"], keep="last")
                    .filter(pl.col("status") == "OPEN")
                )
                open_symbols = set(open_df2["symbol"].to_list())
                new_entered = int(open_df2.filter(pl.col("opened") == res.d).height)
            else:
                open_symbols = set()
                new_entered = 0
        except Exception:  # noqa: BLE001
            open_symbols = set()
            new_entered = 0

        for rec in res.recommendations.iter_rows(named=True):
            if rec["action"] == "BUY" and int(rec.get("qty") or 0) > 0 and rec["symbol"] not in open_symbols:
                if new_entered >= max_new_entries:
                    break
                entry_price = round((float(rec["entry_lo"]) + float(rec["entry_hi"])) / 2.0, 2)
                qty = int(rec["qty"])
                stop = float(rec["stop"])
                target = float(rec.get("target") or 0.0)

                pos_entry_df = pl.DataFrame([{
                    "as_of": res.as_of,
                    "symbol": rec["symbol"],
                    "industry": str(rec.get("industry") or ""),
                    "qty": qty,
                    "entry": entry_price,
                    "last": entry_price,
                    "stop": stop,
                    "target": target,
                    "opened": res.d,
                    "horizon": str(rec.get("horizon") or "SWING"),
                    "sleeve": str(rec.get("sleeve") or "MOMENTUM_BREAKOUT"),
                    "status": "OPEN",
                    "pnl": 0.0,
                }], schema={
                    "as_of": pl.Datetime("ms", "UTC"),
                    "symbol": pl.Utf8,
                    "industry": pl.Utf8,
                    "qty": pl.UInt32,
                    "entry": pl.Float64,
                    "last": pl.Float64,
                    "stop": pl.Float64,
                    "target": pl.Float64,
                    "opened": pl.Date,
                    "horizon": pl.Utf8,
                    "sleeve": pl.Utf8,
                    "status": pl.Utf8,
                    "pnl": pl.Float64,
                })
                ch.insert_df("ev.positions", pos_entry_df)
                open_symbols.add(rec["symbol"])

                event = LedgerEvent.create(
                    seq=curr_seq,
                    ts=res.as_of,
                    run_id=res.run_id,
                    kind=EventKind.ENTRY_FILL,
                    payload={
                        "d": str(res.d),
                        "symbol": rec["symbol"],
                        "side": "BUY",
                        "qty": qty,
                        "price": entry_price,
                        "notional": round(qty * entry_price, 2),
                        "stop": stop,
                        "target": target,
                        "note": "auto-executed BUY recommendation",
                    },
                    prev_hash=curr_prev_hash,
                )
                ch.insert_df("ev.ledger_events", events_to_df([event]))
                curr_seq += 1
                curr_prev_hash = event.hash
                new_entered += 1

    # Step 3: Record ev.equity_daily snapshot for res.d
    try:
        pf_now = current_portfolio(ch)
        eq_df = pl.DataFrame([{
            "d": res.d,
            "arm": "DETERMINISTIC",
            "equity": float(pf_now.equity),
            "cash": float(pf_now.cash),
            "deployed": float(pf_now.deployed),
            "heat_pct": float(pf_now.heat_pct),
            "drawdown": float(pf_now.drawdown_pct),
            "watermark": float(pf_now.watermark),
        }], schema={
            "d": pl.Date,
            "arm": pl.Utf8,
            "equity": pl.Float64,
            "cash": pl.Float64,
            "deployed": pl.Float64,
            "heat_pct": pl.Float32,
            "drawdown": pl.Float32,
            "watermark": pl.Float64,
        })
        ch.insert_df("ev.equity_daily", eq_df)
    except Exception as e:  # noqa: BLE001
        log.warning("could not record equity snapshot: %s", e)


def _stamp(df: pl.DataFrame, res: RunResult) -> pl.DataFrame:
    kt = res.as_of.replace(tzinfo=UTC)
    return df.with_columns(
        pl.lit(res.as_of).cast(pl.Datetime("ms", "UTC")).alias("as_of"),
        pl.lit(res.run_id).alias("run_id"),
        pl.lit(kt).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time"),
    )


def persist(ch: GuardedClient, res: RunResult) -> dict[str, int]:
    """Write the run's outputs. Each table is independent, so one failure does
    not lose the others -- a missing heat map is better than a missing run."""
    out: dict[str, int] = {}
    kt = res.as_of.replace(tzinfo=UTC)

    def write(table: str, df: pl.DataFrame, cols: list[str]) -> None:
        if df.is_empty():
            out[table] = 0
            return
        try:
            out[table] = ch.insert_df(table, df.select(cols))
        except Exception as e:  # noqa: BLE001
            log.error("write %s failed: %s", table, e)
            out[table] = -1

    if not res.sector.is_empty():
        sec = res.sector.with_columns(
            pl.lit(kt).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time"),
            pl.col("n_symbols").cast(pl.UInt16),
            pl.col("adv_count").cast(pl.UInt16), pl.col("dec_count").cast(pl.UInt16),
            *[pl.col(c).cast(pl.Float32) for c in
              ("cap_weighted_ret", "median_ret", "breadth_pct", "move_z", "rs_21d")],
        )
        write("md.sector_daily", sec, [
            "d", "industry", "n_symbols", "turnover", "cap_weighted_ret", "median_ret",
            "breadth_pct", "adv_count", "dec_count", "move_z", "rs_21d", "knowledge_time"])

    if not res.breadth.is_empty():
        br = res.breadth.with_columns(
            pl.lit(kt).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time"),
            pl.col("n_symbols").cast(pl.UInt16),
            pl.col("advancers").cast(pl.UInt16), pl.col("decliners").cast(pl.UInt16),
            *[pl.col(c).cast(pl.Float32) for c in
              ("pct_above_sma50", "pct_above_sma200", "ret_dispersion_xs", "median_ret")],
        )
        write("md.breadth_daily", br, [
            "d", "universe", "n_symbols", "advancers", "decliners", "pct_above_sma50",
            "pct_above_sma200", "ret_dispersion_xs", "median_ret", "knowledge_time"])

    if not res.trend.is_empty():
        tr = res.trend.with_columns(
            pl.lit(kt).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time"),
            pl.lit(0.0).cast(pl.Float32).alias("rs_vs_bench_63"),
            pl.col("flips_63d").cast(pl.UInt16),
            pl.col("trend_enum_version").cast(pl.UInt16),
            *[pl.col(c).cast(pl.Float32) for c in
              ("t_drift_20", "t_drift_63", "t_drift_252", "er_20", "er_63", "er_252",
               "adx14", "adx_delta_10")],
        )
        write("md.trend_1d", tr, [
            "d", "symbol", "trend_20", "trend_63", "trend_252",
            "t_drift_20", "t_drift_63", "t_drift_252", "er_20", "er_63", "er_252",
            "adx14", "adx_delta_10", "flips_63d", "rs_vs_bench_63",
            "trend_enum_version", "knowledge_time"])

    if not res.recommendations.is_empty():
        rec = _stamp(res.recommendations, res).with_columns(
            pl.col("qty").cast(pl.UInt32),
            pl.col("would_qty").cast(pl.UInt32),
            pl.col("barrier_wide").cast(pl.UInt8),
            *[pl.col(c).cast(pl.Float32) for c in
              ("risk_pct", "rr", "cost_bps", "expectancy_atr", "composite", "n_eff")],
            *[pl.col(c).cast(pl.Float32) for c in ("p_up", "p_dn", "p_vert", "ci_lo", "ci_hi")],
        )
        write("ev.recommendations", rec, [
            "as_of", "run_id", "rec_id", "d", "symbol", "name", "industry", "arm",
            "action", "sleeve", "horizon", "entry_lo", "entry_hi", "stop", "target",
            "qty", "notional", "would_qty", "would_notional", "would_risk_rupees",
            "afford_note", "risk_rupees", "risk_pct", "rr", "atr14", "cost_bps",
            "expectancy_atr", "composite", "reasons", "trend_20", "trend_63", "trend_252",
            "ind_trend_20", "ind_trend_63", "ind_trend_252",
            "barrier_wide", "barrier_state", "p_up", "p_dn",
            "p_vert", "ci_lo", "ci_hi", "n_eff", "barrier_reason", "knowledge_time"])

    if not res.rejections.is_empty():
        rej = res.rejections.with_columns(
            pl.lit(res.as_of).cast(pl.Datetime("ms", "UTC")).alias("as_of"),
            pl.lit(res.run_id).alias("run_id"), pl.lit(res.d).alias("d"),
        )
        write("ev.rejections", rej, ["as_of", "run_id", "d", "symbol", "rule_id",
                                     "reason", "detail"])

    log.info("persisted: %s", out)
    return out


def execute_backtest(
    sessions: int = 126, *, persist_results: bool = True, arm: str = "FROZEN_CONTROL",
    regime_filter: bool = False, run_id: str | None = None,
) -> dict[str, Any]:
    """Execute historical backtest over recent sessions and optionally persist equity curve.

    `arm="FROZEN_CONTROL"` is the day-1 rule set and is what the Honesty view
    shows. `regime_filter=True` runs the ADR-0004 candidate (index regime +
    REG-01 bear block) for research; it is report-only, never persisted, so a
    rejected rule can never overwrite the control's curve. `arm="DETERMINISTIC"`
    is report-only too: writing it would splice a simulation into the real
    paper book (I-23).

    Every control or regime-filter run is also recorded in ev.backtest_runs
    under its requested `sessions` window, so each window keeps its own last
    result. `run_id` pairs the control and variant lines of one request.
    """
    from ta_core.enums import Arm

    arm_obj = Arm(arm)
    if arm_obj not in (Arm.FROZEN_CONTROL, Arm.DETERMINISTIC):
        raise ValueError(f"backtest supports FROZEN_CONTROL or DETERMINISTIC, not {arm}")
    if arm_obj is Arm.DETERMINISTIC or regime_filter:
        persist_results = False

    from .backtest.engine import run_backtest
    from .stats import min_trl

    with GuardedClient() as ch:
        d = latest_session(ch)
        a = AsOf(datetime.now(UTC))
        bars, members = load_panel_inputs(ch, d, a=a, lookback=max(420, sessions + 260))
        if not members.is_empty():
            n500 = members.filter(pl.col("index_name") == "NIFTY500")["symbol"].to_list()
            if n500:
                bars = bars.filter(pl.col("symbol").is_in(n500))

        sorted_dates = sorted(bars["d"].unique().to_list())
        start_date = sorted_dates[-sessions] if len(sorted_dates) > sessions else None

        # The regime series is loaded for every run: it gates trades only when
        # regime_filter is set, but it is also stamped on each curve point so
        # the chart can say what the market was doing on any day.
        regime_info = load_index_regime(ch, d, a)
        index_regime = regime_info if regime_filter else None
        res = run_backtest(
            bars,
            members,
            start_date=start_date,
            end_date=d,
            arm=arm_obj if not regime_filter else Arm.DETERMINISTIC,
            warmup_sessions=40,
            index_regime=index_regime,
        )

        if persist_results and res.equity_curve:
            eq_df = (
                res.equity_curve
                if isinstance(res.equity_curve, pl.DataFrame)
                else pl.DataFrame(res.equity_curve)
            )
            if not eq_df.is_empty():
                if "drawdown_pct" in eq_df.columns and "drawdown" not in eq_df.columns:
                    eq_df = eq_df.rename({"drawdown_pct": "drawdown"})
                eq_to_write = eq_df.with_columns(
                    pl.col("d").str.to_date() if eq_df.schema["d"] == pl.Utf8 else pl.col("d").cast(pl.Date),
                    pl.lit("FROZEN_CONTROL").cast(pl.Utf8).alias("arm"),
                    pl.col("equity").cast(pl.Float64),
                    pl.col("cash").cast(pl.Float64),
                    pl.col("deployed").cast(pl.Float64),
                    pl.col("heat_pct").cast(pl.Float32),
                    pl.col("drawdown").cast(pl.Float32),
                    pl.col("watermark").cast(pl.Float64),
                ).select(["d", "arm", "equity", "cash", "deployed", "heat_pct", "drawdown", "watermark"])
                try:
                    ch.command("ALTER TABLE ev.equity_daily DELETE WHERE arm = 'FROZEN_CONTROL' SETTINGS mutations_sync = 1")
                    ch.insert_df("ev.equity_daily", eq_to_write)
                except Exception as e:  # noqa: BLE001
                    log.warning("failed to persist equity curve: %s", e)

            if res.outcomes:
                try:
                    ch.command("ALTER TABLE ev.outcomes DELETE WHERE arm = 'FROZEN_CONTROL' SETTINGS mutations_sync = 1")
                    out_rows = [
                        {
                            "rec_id": str(o["rec_id"]),
                            "symbol": str(o["symbol"]),
                            "arm": "FROZEN_CONTROL",
                            "horizon": str(o.get("horizon") or "SWING"),
                            "entry_date": datetime.strptime(str(o["entry_date"]), "%Y-%m-%d").date() if isinstance(o["entry_date"], str) else o["entry_date"],
                            "entry_price": float(o["entry_price"]),
                            "exit_date": datetime.strptime(str(o["exit_date"]), "%Y-%m-%d").date() if isinstance(o["exit_date"], str) else o["exit_date"],
                            "exit_price": float(o["exit_price"]),
                            "barrier_hit": str(o["barrier_hit"]),
                            "r_multiple": float(o["r_multiple"]),
                            "pnl_rupees": float(o["pnl_rupees"]),
                            "mae_r": float(o.get("mae_r") or 0.0),
                            "mfe_r": float(o.get("mfe_r") or 0.0),
                            "holding_days": max(0, int(o.get("holding_days") or 0)),
                            "benchmark_ret": 0.0,
                            "stated_conf": 0.0,
                            "calibrated_p": 0.0,
                            "uniqueness_w": 1.0,
                            "regime": "UNKNOWN",
                            "degraded_mode": 0,
                            "knowledge_time": a.ts,
                        }
                        for o in res.outcomes
                    ]
                    out_df = pl.DataFrame(out_rows, schema={
                        "rec_id": pl.Utf8,
                        "symbol": pl.Utf8,
                        "arm": pl.Utf8,
                        "horizon": pl.Utf8,
                        "entry_date": pl.Date,
                        "entry_price": pl.Float64,
                        "exit_date": pl.Date,
                        "exit_price": pl.Float64,
                        "barrier_hit": pl.Utf8,
                        "r_multiple": pl.Float32,
                        "pnl_rupees": pl.Float64,
                        "mae_r": pl.Float32,
                        "mfe_r": pl.Float32,
                        "holding_days": pl.UInt16,
                        "benchmark_ret": pl.Float32,
                        "stated_conf": pl.Float32,
                        "calibrated_p": pl.Float32,
                        "uniqueness_w": pl.Float32,
                        "regime": pl.Utf8,
                        "degraded_mode": pl.UInt8,
                        "knowledge_time": pl.Datetime("ms", "UTC"),
                    })
                    ch.insert_df("ev.outcomes", out_df)
                except Exception as e:  # noqa: BLE001
                    log.warning("failed to persist backtest outcomes: %s", e)

        s = res.stats
        sim = _forward_simulation(res)
        m_trl = min_trl(s.sharpe_annualised) if s.sharpe_annualised > 0 else 0.0
        min_trl_val = float(round(m_trl, 1)) if m_trl != float("inf") else 0.0
        line = "REGIME_FILTER" if regime_filter else ("FROZEN_CONTROL" if arm_obj is Arm.FROZEN_CONTROL else None)
        if line is not None and (regime_filter or persist_results):
            _persist_run(
                ch, res, a, run_id=run_id or f"BT-{a.ts:%Y%m%dT%H%M%S%f}", line=line, sessions=sessions,
                sim=sim, regime=regime_info,
                min_trl_val=(float(round(m_trl, 1)) if 0 < m_trl != float("inf") else None),
            )
        return {
            "start_date": str(res.start_date),
            "end_date": str(res.end_date),
            "n_sessions": int(s.n_sessions),
            "initial_equity": float(res.initial_equity),
            "final_equity": float(res.final_equity),
            "total_return_pct": float(s.total_return_pct),
            "n_trades": int(s.n_trades),
            "win_rate": float(s.hit_rate),
            "sharpe": float(s.sharpe_annualised),
            "psr": float(s.psr),
            "dsr": float(s.dsr),
            "min_trl": min_trl_val,
            "simulation_status": sim.status,
            "benchmark_index": BENCHMARK_INDEX,
            "benchmark_return_pct": benchmark_return(ch, a, res.start_date, res.end_date),
        }


def _forward_simulation(res: Any) -> Any:
    """Block-bootstrap the backtest's realised daily returns (I-27).

    Starts from the initial capital so an N-session backtest yields N daily
    returns -- otherwise the first session's P&L is dropped and a run of
    exactly SIM_MIN_DAYS sessions is refused as one short.
    """
    from .stats import block_bootstrap_forward

    eq = res.equity_curve if isinstance(res.equity_curve, pl.DataFrame) else pl.DataFrame(res.equity_curve)
    equity = [float(res.initial_equity)] + (eq["equity"].cast(pl.Float64).to_list() if eq.height else [])
    rets = [equity[i] / equity[i - 1] - 1.0 for i in range(1, len(equity)) if equity[i - 1] > 0]
    return block_bootstrap_forward(rets, n_trades=len(res.outcomes))


def _persist_run(
    ch: GuardedClient, res: Any, a: AsOf, *, run_id: str, line: str, sessions: int,
    sim: Any, regime: pl.DataFrame | None, min_trl_val: float | None,
) -> None:
    """Record one backtest line in ev.backtest_runs, keyed by its window.

    The Calibration page shows the latest row for the selected window, so a
    252-session result is still there after a 1250-session run. Statistics are
    the job's own -- the API used to recompute them from the curve and showed
    a different Sharpe than the CLI for the same run. Never ev.equity_daily
    for a variant (I-28).
    """
    from .regime import regime_on

    eq = res.equity_curve if isinstance(res.equity_curve, pl.DataFrame) else pl.DataFrame(res.equity_curve)
    if eq.is_empty():
        return
    ds = [x if isinstance(x, date) else datetime.strptime(str(x), "%Y-%m-%d").date() for x in eq["d"].to_list()]
    if regime is not None and not regime.is_empty():
        regimes = [regime_on(regime, x)[0].value for x in ds]
    else:
        regimes = ["UNKNOWN"] * len(ds)
    dd_col = "drawdown_pct" if "drawdown_pct" in eq.columns else ("drawdown" if "drawdown" in eq.columns else None)
    st = res.stats
    bench = benchmark_return(ch, a, ds[0], ds[-1])
    row = pl.DataFrame([{
        "run_id": run_id, "run_ts": a.ts, "line": line, "sessions": int(sessions),
        "bench_index": BENCHMARK_INDEX, "bench_return": bench,
        "excess_return": (float(st.total_return_pct) - bench) if bench is not None else None,
        "start_d": ds[0], "end_d": ds[-1], "n_sessions": len(ds), "n_trades": int(st.n_trades),
        "initial_equity": float(res.initial_equity), "final_equity": float(res.final_equity),
        "total_return": float(st.total_return_pct), "sharpe": float(st.sharpe_annualised),
        "psr": float(st.psr), "dsr": float(st.dsr), "min_trl": min_trl_val,
        "win_rate": float(st.hit_rate), "max_drawdown": float(st.max_drawdown_pct),
        "sim_status": sim.status, "sim_horizon": sim.horizon, "sim_block_len": sim.block_len,
        "sim_n_paths": sim.n_paths, "sim_p_lo": sim.p_lo, "sim_p_hi": sim.p_hi, "sim_p_loss": sim.p_loss,
        "sim_hist_lo": [b for b, _ in sim.histogram], "sim_hist_count": [c for _, c in sim.histogram],
        "curve_d": ds, "curve_equity": eq["equity"].cast(pl.Float64).to_list(),
        "curve_drawdown": eq[dd_col].cast(pl.Float32).to_list() if dd_col else [0.0] * len(ds),
        "curve_heat": eq["heat_pct"].cast(pl.Float32).to_list() if "heat_pct" in eq.columns else [0.0] * len(ds),
        "curve_regime": regimes,
    }], schema={
        "run_id": pl.Utf8, "run_ts": pl.Datetime("ms", "UTC"), "line": pl.Utf8, "sessions": pl.UInt32,
        "bench_index": pl.Utf8, "bench_return": pl.Float32, "excess_return": pl.Float32,
        "start_d": pl.Date, "end_d": pl.Date, "n_sessions": pl.UInt32, "n_trades": pl.UInt32,
        "initial_equity": pl.Float64, "final_equity": pl.Float64, "total_return": pl.Float32,
        "sharpe": pl.Float32, "psr": pl.Float32, "dsr": pl.Float32, "min_trl": pl.Float32,
        "win_rate": pl.Float32, "max_drawdown": pl.Float32, "sim_status": pl.Utf8,
        "sim_horizon": pl.UInt16, "sim_block_len": pl.UInt16, "sim_n_paths": pl.UInt32,
        "sim_p_lo": pl.Float32, "sim_p_hi": pl.Float32, "sim_p_loss": pl.Float32,
        "sim_hist_lo": pl.List(pl.Float32), "sim_hist_count": pl.List(pl.UInt32),
        "curve_d": pl.List(pl.Date), "curve_equity": pl.List(pl.Float64),
        "curve_drawdown": pl.List(pl.Float32), "curve_heat": pl.List(pl.Float32),
        "curve_regime": pl.List(pl.Utf8),
    })
    try:
        ch.insert_df("ev.backtest_runs", row)
    except Exception as e:  # noqa: BLE001
        log.warning("failed to persist %s backtest run: %s", line, e)
    row = pl.DataFrame([{
        "run_ts": a.ts, "arm": "FROZEN_CONTROL", "status": sim.status,
        "start_d": res.start_date if isinstance(res.start_date, date) else datetime.strptime(str(res.start_date), "%Y-%m-%d").date(),
        "end_d": res.end_date if isinstance(res.end_date, date) else datetime.strptime(str(res.end_date), "%Y-%m-%d").date(),
        "n_days": sim.n_days, "n_trades": sim.n_trades, "horizon": sim.horizon,
        "block_len": sim.block_len, "n_paths": sim.n_paths, "seed": sim.seed,
        "p_lo": sim.p_lo, "p_hi": sim.p_hi, "p_loss": sim.p_loss,
        "hist_lo": [b for b, _ in sim.histogram], "hist_count": [c for _, c in sim.histogram],
    }], schema={
        "run_ts": pl.Datetime("ms", "UTC"), "arm": pl.Utf8, "status": pl.Utf8,
        "start_d": pl.Date, "end_d": pl.Date, "n_days": pl.UInt32, "n_trades": pl.UInt32,
        "horizon": pl.UInt16, "block_len": pl.UInt16, "n_paths": pl.UInt32, "seed": pl.UInt64,
        "p_lo": pl.Float32, "p_hi": pl.Float32, "p_loss": pl.Float32,
        "hist_lo": pl.List(pl.Float32), "hist_count": pl.List(pl.UInt32),
    })
    try:
        ch.insert_df("ev.simulation", row)
    except Exception as e:  # noqa: BLE001
        log.warning("failed to persist forward simulation: %s", e)
    return sim
