"""`ta` — the operator CLI. Every pipeline stage is runnable by hand."""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def cmd_cost(a: argparse.Namespace) -> int:
    from ta_core.costs import compute_costs
    from ta_core.enums import ProductType
    prod = ProductType.INTRADAY if a.intraday else ProductType.DELIVERY
    c = compute_costs(a.notional, a.notional, prod)
    print(f"{prod.value} round trip on Rs {a.notional:,.0f}")
    print(f"  brokerage    {c.brokerage:>10,.2f}")
    print(f"  STT          {c.stt:>10,.2f}")
    print(f"  exchange     {c.exchange_txn:>10,.2f}")
    print(f"  SEBI         {c.sebi_fee:>10,.2f}")
    print(f"  stamp duty   {c.stamp_duty:>10,.2f}")
    print(f"  GST          {c.gst:>10,.2f}")
    print(f"  DP charge    {c.dp_charge:>10,.2f}")
    print(f"  {'TOTAL':<12} {c.total:>10,.2f}   ({c.bps_of(a.notional)} bps)")
    return 0


def cmd_ingest(a: argparse.Namespace) -> int:
    from ta_datafeed.backfill import ingest_sessions
    end = _d(a.end) if a.end else date.today()
    start = _d(a.start) if a.start else end - timedelta(days=a.days)
    print(f"ingesting NSE sessions {start} .. {end}")
    s = ingest_sessions(start, end)
    print(f"  days probed     : {s['requested']}")
    print(f"  sessions stored : {s['sessions']}")
    print(f"  holidays (404)  : {s['holidays']}")
    print(f"  failures        : {s['failed']}")
    print(f"  rows inserted   : {s['rows']:,}")
    return 1 if s["sessions"] == 0 else 0


def cmd_daily(a: argparse.Namespace) -> int:
    """The whole nightly run, in the order the plan specifies.

    Each stage is independent and a failure is reported rather than raised:
    a missing constituents refresh must not cost you the day's bars, and a
    screen that fails must not hide the fact that ingestion succeeded. The
    exit code is non-zero if the SCREEN did not produce a run, because that
    is the stage whose silence would otherwise look like "no trades today".
    """
    from datetime import datetime

    from ta_datafeed.backfill import (
        ingest_sessions,
        refresh_constituents,
        refresh_symbol_master,
    )
    from ta_quant.nightly import execute_nightly

    t0 = datetime.now()
    end = _d(a.end) if a.end else date.today()
    start = end - timedelta(days=a.days)
    if a.days == 7:
        try:
            from ta_datafeed.store import GuardedClient
            with GuardedClient() as ch:
                n_bars = ch.query_admin("SELECT uniqExact(d) AS n FROM md.bars_1d")["n"][0]
            if n_bars < 75:
                start = end - timedelta(days=max(a.days, 115))
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).debug("could not check md.bars_1d session count: %s", e)
    rc = 0

    if getattr(a, "replay", False):
        target_d = _d(a.date) if getattr(a, "date", None) else None
        print(f"[replay] re-deriving recommendations for session {target_d or 'latest'} from record at session as-of")
        try:
            res = execute_nightly(target_d, replay=True)
            buy = (
                res.recommendations.filter(res.recommendations["action"] == "BUY").height
                if res.recommendations.height
                else 0
            )
            print(f"      run_id={res.run_id} session={res.d} regime={res.regime.value}")
            print(
                f"      cards={res.recommendations.height} (BUY={buy}) "
                f"rejected={res.rejections.height}"
            )
        except Exception as e:  # noqa: BLE001
            print(f"      FAILED: {e}")
            rc = 1
        print(f"done in {(datetime.now() - t0).total_seconds():.0f}s, rc={rc}")
        return rc

    print(f"[1/4] ingest {start} .. {end}")
    try:
        s = ingest_sessions(start, end)
        print(f"      sessions={s['sessions']} rows={s['rows']:,} "
              f"holidays={s['holidays']} failed={s['failed']}")
    except Exception as e:  # noqa: BLE001
        print(f"      FAILED: {e}")
        rc = 1

    print("[2/4] symbol master")
    try:
        print(f"      ref.symbols={refresh_symbol_master():,}")
    except Exception as e:  # noqa: BLE001
        print(f"      FAILED: {e}")

    print("[3/4] index membership snapshot")
    try:
        print(f"      {refresh_constituents()}")
    except Exception as e:  # noqa: BLE001
        print(f"      FAILED: {e}")

    print("[4/4] screen + size + record")
    try:
        res = execute_nightly()
        buy = res.recommendations.filter(
            res.recommendations["action"] == "BUY"
        ).height if res.recommendations.height else 0
        print(f"      run_id={res.run_id} session={res.d} regime={res.regime.value}")
        print(f"      cards={res.recommendations.height} (BUY={buy}) "
              f"rejected={res.rejections.height}")
    except Exception as e:  # noqa: BLE001
        print(f"      FAILED: {e}")
        rc = 1

    print(f"done in {(datetime.now() - t0).total_seconds():.0f}s, rc={rc}")
    return rc


def cmd_index_history(a: argparse.Namespace) -> int:
    """Backfill md.index_1d only -- for regime checks on pre-bhavcopy history."""
    from ta_datafeed.backfill import ingest_index_history
    end = _d(a.end) if a.end else date.today()
    start = _d(a.start)
    print(f"ingesting NSE index close files {start} .. {end}")
    s = ingest_index_history(start, end)
    print(f"  sessions={s['sessions']} rows={s['rows']:,} holidays={s['holidays']} failed={s['failed']}")
    return 1 if s["sessions"] == 0 else 0


def cmd_constituents(a: argparse.Namespace) -> int:
    from ta_datafeed.backfill import refresh_constituents
    s = refresh_constituents(tuple(a.indices))
    for k, v in s.items():
        print(f"  {k:<12} {v:>4} members")
    return 0 if any(s.values()) else 1


def cmd_symbols(a: argparse.Namespace) -> int:
    from ta_datafeed.backfill import refresh_symbol_master
    print(f"  ref.symbols: {refresh_symbol_master():,} rows")
    return 0


def cmd_screen(a: argparse.Namespace) -> int:
    """Run tonight's screen and print the cards.

    `features` and `sectors` used to be separate subcommands that imported
    functions which never existed; both are now stages inside the one nightly
    run, so exposing them as standalone commands was advertising an API the
    code did not have.
    """
    import polars as pl
    from ta_quant.nightly import execute_nightly

    res = execute_nightly(_d(a.date) if a.date else None)
    r = res.recommendations
    print(f"run {res.run_id} · session {res.d} · regime {res.regime.value}")
    if r.is_empty():
        print("  no candidates survived the gates")
        return 1
    cols = [c for c in ("symbol", "industry", "action", "entry_hi", "stop",
                        "target", "would_qty", "would_notional",
                        "expectancy_atr") if c in r.columns]
    with pl.Config(tbl_rows=a.top, tbl_width_chars=160):
        print(r.select(cols).head(a.top))
    buys = r.filter(pl.col("action") == "BUY").height
    print(f"  {buys} actionable · {r.height - buys} watch · "
          f"{res.rejections.height} rejected")
    return 0


def cmd_calendar(a: argparse.Namespace) -> int:
    """Sessions actually OBSERVED in the store for a month.

    Derived from the bars we hold rather than from a holiday list: NSE not
    publishing a bhavcopy IS the authoritative statement that a date was not
    a session, and a hand-maintained calendar drifts from it.
    """
    from ta_datafeed.store import GuardedClient

    y, m = (int(x) for x in a.month.split("-"))
    last = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    with GuardedClient() as ch:
        rows = ch.query_admin(
            "SELECT DISTINCT d FROM md.bars_1d "
            f"WHERE d >= '{date(y, m, 1)}' AND d <= '{last}' ORDER BY d"
        )
    days = rows["d"].to_list() if rows.height else []
    print(f"{a.month}: {len(days)} sessions observed")
    for d in days:
        print(f"  {d}")
    return 0


def cmd_backtest(a: argparse.Namespace) -> int:
    """Run historical backtest simulation over recent sessions."""
    from ta_quant.nightly import execute_backtest

    sessions = getattr(a, "sessions", 126) or 126
    arm = (getattr(a, "arm", None) or "FROZEN_CONTROL").upper()
    print(f"Running historical backtest over {sessions} sessions ({arm})...")
    rf = bool(getattr(a, "regime_filter", False))
    res = execute_backtest(sessions=sessions, persist_results=True, arm=arm, regime_filter=rf)

    label = "INDEX REGIME + REG-01 (ADR-0004 candidate, rejected)" if rf else arm
    print("=" * 60)
    print(f"BACKTEST RESULTS: {label}" + ("  (report-only, not persisted)" if rf or arm != "FROZEN_CONTROL" else ""))
    print(f"  Period               : {res['start_date']} .. {res['end_date']}")
    print(f"  Sessions             : {res['n_sessions']}")
    print(f"  Initial Equity       : Rs {res['initial_equity']:,.2f}")
    print(f"  Final Equity         : Rs {res['final_equity']:,.2f}")
    print(f"  Total Return         : {res['total_return_pct']:+.2%}")  # a fraction, like win_rate
    b = res.get("benchmark_return_pct")
    if b is not None:  # ADR-0005: the index every strategy must beat
        ex = res["total_return_pct"] - b
        print(f"  {res.get('benchmark_index', 'Nifty 500') + ' (price)':<21}: {b:+.2%}")
        print(f"  vs benchmark         : {ex:+.2%}  ({'BEATS' if ex > 0 else 'TRAILS'} the index)")
    print(f"  Sharpe (Annualised)  : {res['sharpe']:.2f}")
    print(f"  Trades               : {res['n_trades']}")
    print(f"  Win Rate             : {res['win_rate']:.2%}")
    print(f"  PSR (Prob Sharpe)    : {res['psr']:.2%}")
    print(f"  DSR (Deflated Sharpe): {res['dsr']:.2%}")
    print(f"  Min TRL              : {res['min_trl']}")
    print(f"  Forward simulation   : {res.get('simulation_status', '-')}")
    print("=" * 60)
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    from ta_datafeed.store import GuardedClient
    with GuardedClient() as ch:
        rows = ch.query_admin(
            "SELECT 'md.bars_1d' t, count() c, min(d)::String lo, max(d)::String hi, "
            "uniqExact(symbol)::String u FROM md.bars_1d "
            "UNION ALL SELECT 'md.index_members', count(), min(snapshot_date)::String, "
            "max(snapshot_date)::String, uniqExact(symbol)::String FROM md.index_members "
            "UNION ALL SELECT 'md.features_1d', count(), min(d)::String, max(d)::String, "
            "uniqExact(symbol)::String FROM md.features_1d "
            "UNION ALL SELECT 'md.sector_daily', count(), min(d)::String, max(d)::String, "
            "uniqExact(industry)::String FROM md.sector_daily "
            "UNION ALL SELECT 'ref.symbols', count(), '', '', uniqExact(symbol)::String FROM ref.symbols"
        )
        print(rows)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ta", description="Trading Agent operator CLI")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("cost", help="print the Indian round-trip cost model")
    c.add_argument("--notional", type=float, default=100_000)
    c.add_argument("--intraday", action="store_true")
    c.add_argument("--delivery", action="store_true")
    c.set_defaults(fn=cmd_cost)

    c = sub.add_parser("ingest", help="fetch NSE bhavcopy sessions into ClickHouse")
    c.add_argument("--start"); c.add_argument("--end")
    c.add_argument("--days", type=int, default=30)
    c.set_defaults(fn=cmd_ingest)

    c = sub.add_parser("daily", help="the full nightly run (ingest + screen + record)")
    c.add_argument("--end", help="last session to ingest, YYYY-MM-DD")
    c.add_argument("--days", type=int, default=7,
                   help="how far back to ingest; cached files cost nothing")
    c.add_argument("--date", help="target session YYYY-MM-DD (for --replay)")
    c.add_argument("--replay", action="store_true", help="re-derive recommendations from record at session as-of")
    c.set_defaults(fn=cmd_daily)

    c = sub.add_parser("backtest", help="run historical backtest simulation")
    c.add_argument("--sessions", type=int, default=126, help="number of historical sessions to simulate")
    c.add_argument("--start", help="start date YYYY-MM-DD")
    c.add_argument("--end", help="end date YYYY-MM-DD")
    c.add_argument("--arm", default="FROZEN_CONTROL", help="arm to simulate (FROZEN_CONTROL, DETERMINISTIC)")
    c.add_argument("--regime-filter", action="store_true",
                   help="research: apply the index regime + REG-01 bear block (ADR-0004; report-only)")
    c.add_argument("--trials", type=int, default=1, help="number of trials for deflated Sharpe")
    c.set_defaults(fn=cmd_backtest)

    c = sub.add_parser("index-history", help="backfill NSE index OHLC (md.index_1d) only")
    c.add_argument("--start", required=True); c.add_argument("--end")
    c.set_defaults(fn=cmd_index_history)

    c = sub.add_parser("constituents", help="snapshot index membership + industries")
    c.add_argument("--indices", nargs="+",
                   default=["NIFTY50", "NIFTY100", "NIFTY200", "NIFTY500"])
    c.set_defaults(fn=cmd_constituents)

    c = sub.add_parser("symbols", help="refresh the NSE symbol master")
    c.set_defaults(fn=cmd_symbols)

    c = sub.add_parser("screen", help="run the liquidity+momentum screen")
    c.add_argument("--date"); c.add_argument("--top", type=int, default=25)
    c.add_argument("--index", default="NIFTY200"); c.set_defaults(fn=cmd_screen)

    c = sub.add_parser("calendar", help="sessions observed in a month (YYYY-MM)")
    c.add_argument("month"); c.set_defaults(fn=cmd_calendar)

    c = sub.add_parser("status", help="what is in the database")
    c.set_defaults(fn=cmd_status)

    a = p.parse_args(argv)
    _log(a.verbose)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
