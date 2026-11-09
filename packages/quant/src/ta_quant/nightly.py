"""Database I/O for the nightly run. The pipeline itself stays pure.

Separated on purpose: `pipeline.run_nightly` takes frames and returns frames,
so it is testable without a database, and every read here goes through the PIT
guard with an explicit as-of rather than `now()`.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time

import polars as pl
from ta_core.asof import AsOf
from ta_core.timefmt import IST
from ta_datafeed.store import GuardedClient
from ta_ledger.risk import DEFAULT, Portfolio

from .pipeline import RunResult, run_nightly

log = logging.getLogger(__name__)

__all__ = ["as_of_for", "execute_nightly", "load_panel_inputs", "persist"]

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
        pos = ch.query_df(
            "SELECT symbol, industry, qty, entry, last, stop, opened "
            "FROM ev.positions WHERE status = 'OPEN'"
        )
    except Exception as e:  # noqa: BLE001 - a missing table must not kill the run
        log.warning("positions unavailable (%s); starting flat", e)
        return Portfolio(equity=equity, cash=equity, watermark=equity)

    from ta_ledger.risk import Position
    items = tuple(
        Position(symbol=r["symbol"], industry=r["industry"], qty=int(r["qty"]),
                 entry=float(r["entry"]), last=float(r["last"]),
                 stop=float(r["stop"]), opened=r["opened"])
        for r in pos.iter_rows(named=True)
    ) if pos.height else ()
    deployed = sum(p.notional for p in items)
    return Portfolio(equity=equity, cash=equity - deployed, positions=items,
                     watermark=equity)


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
        res = run_nightly(bars, members, d=day, portfolio=pf, cfg=DEFAULT, as_of=a.ts)
        persist(ch, res)
        return res


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
