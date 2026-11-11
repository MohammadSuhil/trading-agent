"""FastAPI read model for the dashboard.

Two disciplines, both inherited from the plan and both load-bearing:

1. **Every endpoint is one SELECT from a pre-aggregated table.** The heat map's
   gate is p95 < 30 ms, which is only achievable if the nightly run has already
   done the work. A panel that needs a per-request computation over 500 stocks
   must first add a nightly pre-aggregate; there is no exception path.

2. **Reads are PIT-bounded with an explicit as-of.** `ta_core.asof` refuses an
   unbounded read and now also refuses `now()` inside SQL, so each handler
   pins a literal instant. Serving a past recommendation must return what was
   recorded, never a value recomputed with data that arrived afterwards.

The API computes nothing. Every number on the dashboard was decided and
recorded by the nightly run; this layer selects and serialises it.

## Unit conventions — read before adding a field

**Every `*_pct` field is a FRACTION of one, not a percentage.** `pct_above_sma50: 0.235` means
23.5%; `risk_pct: 0.0079` means 0.79%. The suffix is a trap and it nearly caused a 100× display
error: read as "already a percentage" it would have rendered *0.24% above the 50-DMA* on a day
when 23.5% of the universe was above it — wrong by two orders of magnitude and entirely
plausible-looking.

Rupee amounts carry an explicit `_rupees` suffix. `barrier.ci_lo/ci_hi` are the 90% credible
interval around `p_up` specifically (the Beta marginal of the Dirichlet), not around the triad.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from ta_core.asof import AsOf
from ta_core.enums import Regime
from ta_core.glossary import as_dict
from ta_datafeed.store import GuardedClient
from ta_ledger.risk import DEFAULT, Portfolio, rule_states

log = logging.getLogger(__name__)

app = FastAPI(title="Trading Agent", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["GET", "POST"], allow_headers=["*"],
)

EQUITY = 500_000.0


def _now() -> AsOf:
    return AsOf(datetime.now(UTC))


def _rows(df: pl.DataFrame) -> list[dict[str, Any]]:
    """polars -> JSON-safe dicts. NaN/Inf become None: a NaN serialised into
    JSON is invalid and crashes the client, and a silent 0.0 would be worse."""
    if df is None or df.is_empty():
        return []
    out = []
    for r in df.iter_rows(named=True):
        d = {}
        for k, v in r.items():
            # `v != v` is the NaN test: NaN is the only value unequal to
            # itself, and math.isnan would reject the non-float branch.
            if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):  # noqa: PLR0124
                d[k] = None
            elif hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            else:
                d[k] = v
        out.append(d)
    return out


#: One client PER THREAD, not one per process.
#
# clickhouse-connect multiplexes a session over a single client and refuses
# overlapping queries on it: "Attempt to execute concurrent queries within the
# same session. Please use a separate client instance per thread/process."
# FastAPI runs sync endpoints in a worker threadpool, so a single shared client
# fails the moment two requests overlap -- and the Today view legitimately
# fetches five endpoints at once, which made the dashboard look broken at
# random while every sequential test passed.
#
# Thread-local keeps the whole latency win (FastAPI reuses a bounded pool of
# worker threads, so each builds its client once and then reuses it) without
# sharing a session across threads.
_LOCAL = threading.local()
_LOCK = threading.Lock()
_ALL_CLIENTS: list[GuardedClient] = []


class _Shared:
    """Context-manager facade over ONE long-lived client.

    Measured: constructing a `GuardedClient` costs ~38 ms p50 while the query
    it then runs costs ~0.8 ms. Building one per request put `/api/heatmap` at
    58 ms p95 against a <30 ms gate, which read as "the pre-aggregation
    discipline is not working" when in fact the pre-aggregation was fine and
    the client lifecycle was the whole cost.

    Exiting does NOT close the connection -- that is the entire point. Every
    endpoint keeps its `with _ch() as ch:` shape, so nothing else had to
    change to get the win.
    """

    __slots__ = ("_c",)

    def __init__(self, c: GuardedClient) -> None:
        self._c = c

    def __enter__(self) -> GuardedClient:
        return self._c

    def __exit__(self, *exc: object) -> None:
        return None


def _ch() -> _Shared:
    """This thread's client, built once and reused."""
    c: GuardedClient | None = getattr(_LOCAL, "client", None)
    if c is None:
        try:
            c = GuardedClient()
        except Exception as e:
            raise HTTPException(503, f"ClickHouse unavailable: {e}") from e
        _LOCAL.client = c
        with _LOCK:
            _ALL_CLIENTS.append(c)
    return _Shared(c)


def _drop_client() -> None:
    """Forget THIS thread's handle so its next request rebuilds it."""
    c: GuardedClient | None = getattr(_LOCAL, "client", None)
    if c is None:
        return
    try:
        c.close()
    except Exception as e:  # noqa: BLE001
        log.debug("closing stale client: %s", e)
    _LOCAL.client = None
    with _LOCK:
        if c in _ALL_CLIENTS:
            _ALL_CLIENTS.remove(c)


RECS = "ev.recommendations"


def _latest_run(ch: GuardedClient, a: AsOf, d) -> str | None:
    """The newest nightly run for session `d`.

    Re-running the job writes a second set of rows with fresh rec_ids, so
    deduping on rec_id is not enough: without pinning the run, the dashboard
    shows every historical run's cards merged, each symbol repeated once per
    run. ReplacingMergeTree does not help -- the rows are genuinely distinct.
    """
    if d is None:
        return None
    # Hand-written because build_pit_query parenthesises `where`, which a
    # GROUP BY cannot live inside. Still bounded, and `as_of` is passed so the
    # guard checks the bound for EQUALITY rather than mere presence.
    runs = ch.query_df(
        f"SELECT run_id FROM {RECS} "
        f"WHERE knowledge_time <= {a.sql()} AND d = '{d}' "
        f"GROUP BY run_id ORDER BY max(as_of) DESC LIMIT 1",
        a,
    )
    if not runs.is_empty():
        return runs["run_id"][0]
    rejs = ch.query_df(
        f"SELECT run_id FROM ev.rejections WHERE d = '{d}' "
        f"ORDER BY as_of DESC LIMIT 1"
    )
    return rejs["run_id"][0] if not rejs.is_empty() else None


def _latest_session(ch: GuardedClient):
    df = ch.query_df(
        "SELECT max(d) AS d FROM md.bars_1d "
        "WHERE knowledge_time <= '2099-01-01 00:00:00'"
    )
    if not df.height or df["d"][0] is None or str(df["d"][0]) <= "1970-01-02":
        return None
    return df["d"][0]


# ------------------------------------------------------------------- glossary
@app.get("/api/glossary")
def glossary() -> dict:
    """Plain-English meaning of every code the dashboard renders.

    Served from the backend so a tooltip cannot drift from the rule that
    actually fires. The UI renders these verbatim and writes none of its own.
    """
    return {"terms": as_dict()}


# --------------------------------------------------------------------- health
@app.get("/api/health")
def health() -> dict:
    services = {"clickhouse": False, "mongo": False, "redis": False}
    as_of, dq = None, "PARTIAL"
    try:
        with _ch() as ch:
            services["clickhouse"] = True
            d = _latest_session(ch)
            as_of = d.isoformat() if d else None
            dq = "OK" if d else "PARTIAL"
    except Exception as e:  # noqa: BLE001
        log.warning("health: clickhouse down: %s", e)
        _drop_client()
    try:
        import pymongo
        pymongo.MongoClient("mongodb://127.0.0.1:27017/", serverSelectionTimeoutMS=400)\
            .admin.command("ping")
        services["mongo"] = True
    except Exception as e:  # noqa: BLE001
        log.debug("health: mongo unreachable: %s", e)
    try:
        import socket
        with socket.create_connection(("127.0.0.1", 6379), timeout=0.4):
            services["redis"] = True
    except Exception as e:  # noqa: BLE001
        log.debug("health: redis unreachable: %s", e)
    ok = services["clickhouse"]
    return {"status": "ok" if ok else "degraded", "as_of": as_of,
            "data_quality": dq, "services": services}


# -------------------------------------------------------------------- context
@app.get("/api/context")
def context() -> dict:
    a = _now()
    with _ch() as ch:
        d = _latest_session(ch)
        if d is None:
            raise HTTPException(503, "no market data ingested yet")
        br = ch.query_pit(
            "md.breadth_daily", a,
            where=f"d = '{d}' AND universe = 'NIFTY500'", limit=1,
        )
        row = _rows(br)
        b = row[0] if row else {}
        regime, note = _live_regime(ch, a, d, b)
        return {
            "as_of": d.isoformat(),
            "regime": regime.value, "regime_note": note,
            "breadth": {
                "advancers": b.get("advancers", 0),
                "decliners": b.get("decliners", 0),
                "pct_above_sma50": b.get("pct_above_sma50"),
                "universe": b.get("universe", "NIFTY500"),
            },
            "benchmark": {
                "name": "NIFTY 50 TRI", "close": None,
                "ret_1d": None, "ret_21d": None,
                "tri_source": "RECONSTRUCTED",
            },
            "data_quality": "OK" if row else "PARTIAL",
        }


def _live_regime(ch, a, d, breadth: dict) -> tuple[Regime, str]:
    """The regime that GATES trades (breadth) plus the index regime as information.

    ADR-0004 tested the index rules as a gate and rejected them, so gating
    stays on the breadth heuristic -- the same function the nightly uses. The
    nightly still records the index regime (md.regime_daily); it is appended
    to the note, labelled informational, so the dashboard never presents a
    regime that did not actually gate anything as if it had.
    """
    regime, note = _regime_from_breadth(breadth)
    try:
        r = _rows(ch.query_pit("md.regime_daily", a, where=f"d <= '{d}' AND model = 'INDEX_RULES_V1'",
                               order_by="d DESC", limit=1))
    except Exception:  # noqa: BLE001
        r = []
    if r:
        note = f"{note} · Nifty 500 index rules (informational, not gating): {humanize_regime(r[0]['regime'])}"
    return regime, note


def humanize_regime(v: str) -> str:
    return {"TREND_UP": "bull", "TREND_DOWN": "bear", "CHOP": "sideways",
            "HIGH_VOL": "high volatility"}.get(v, v.lower())


def _regime_from_breadth(b: dict) -> tuple[Regime, str]:
    if not b:
        return Regime.UNKNOWN, "no breadth row for this session"
    above = b.get("pct_above_sma50") or 0.0
    disp = b.get("ret_dispersion_xs") or 0.0
    med = b.get("median_ret") or 0.0
    if disp > 0.030:
        return Regime.HIGH_VOL, f"cross-sectional dispersion {disp:.2%} > 3.0%"
    if above >= 0.60 and med > 0:
        return Regime.TREND_UP, f"{above:.0%} above 50-DMA"
    if above <= 0.35:
        return Regime.TREND_DOWN, f"only {above:.0%} above 50-DMA"
    return Regime.CHOP, f"{above:.0%} above 50-DMA, dispersion {disp:.2%}"


# -------------------------------------------------------------------- heatmap
@app.get("/api/heatmap")
def heatmap() -> dict:
    a = _now()
    with _ch() as ch:
        d = _latest_session(ch)
        if d is None:
            return {"as_of": None, "rows": []}
        df = ch.query_pit(
            "md.sector_daily", a,
            columns="industry, n_symbols AS n, cap_weighted_ret AS ret_1d, "
                    "median_ret AS ret_5d, rs_21d AS ret_21d, "
                    "turnover / 1e7 AS turnover_cr, breadth_pct",
            where=f"d = '{d}'", order_by="turnover_cr DESC", limit=200,
        )
        # Dedupe BEFORE truncating. With md.sector_daily spread across
        # unmerged ReplacingMergeTree parts this endpoint returned 40 rows
        # covering only 14 industries -- six real industries never reached the
        # client, and a background merge later hid the bug without fixing it.
        if not df.is_empty():
            df = df.unique(subset=["industry"], keep="last").sort(
                "turnover_cr", descending=True
            ).head(40)
        return {"as_of": d.isoformat() if d else None, "rows": _rows(df)}


# ------------------------------------------------------------------- screener
@app.get("/api/screener")
def screener(limit: int = 50) -> dict:
    a = _now()
    limit = max(1, min(limit, 200))
    with _ch() as ch:
        d = _latest_session(ch)
        if d is None:
            return {"as_of": None, "rows": []}
        # Two separate PIT-bounded reads joined in polars, rather than one SQL
        # join. The guard cannot prove which table a bare `knowledge_time`
        # bound belongs to when two PIT tables are in scope, and it is right
        # to refuse -- qualifying the bound through subquery aliases would be
        # fighting the guard to reach the same answer less safely.
        run_id = _latest_run(ch, a, d)
        recs = ch.query_pit(
            RECS, a,
            columns="symbol, name, industry, entry_hi AS close, composite, sleeve, "
                    "reasons, rr, expectancy_atr, action",
            where=f"d = '{d}' AND run_id = '{run_id}'",
            order_by="composite DESC", limit=limit,
        ) if run_id else pl.DataFrame()
        trend = ch.query_pit(
            "md.trend_1d", a,
            columns="symbol, trend_63 AS trend_state, er_63 AS rs_63, adx14",
            where=f"d = '{d}'",
        )
        # ReplacingMergeTree deduplicates only on an unpredictable background
        # merge, so a re-run of the nightly job leaves both versions visible
        # until then. Joining against an unmerged table multiplies rows. We
        # dedupe on read rather than trusting the engine's timing.
        df = (recs.unique(subset=["symbol"], keep="last")
              .join(trend.unique(subset=["symbol"], keep="last"),
                    on="symbol", how="left")
              .sort("composite", descending=True)
              if not recs.is_empty() else recs)
        rows = _rows(df)
        for r in rows:
            r.setdefault("ret_1d", None)
            r.setdefault("ret_21d", None)
            r.setdefault("rvol_20", None)
            r.setdefault("adtv_cr", None)
        return {"as_of": d.isoformat() if d else None, "rows": rows}


# ------------------------------------------------------------ recommendations
@app.get("/api/recommendations")
def recommendations() -> dict:
    a = _now()
    with _ch() as ch:
        d = _latest_session(ch)
        if d is None:
            return {"as_of": None, "rows": []}
        run_id = _latest_run(ch, a, d)
        if run_id is None:
            return {"as_of": d.isoformat() if d else None, "rows": []}
        df = ch.query_pit(
            RECS, a,
            columns="rec_id AS id, symbol, name, industry, action, sleeve, horizon, "
                    "entry_lo, entry_hi, stop, target, qty, notional, would_qty, "
                    "would_notional, would_risk_rupees, afford_note, risk_rupees, "
                    "risk_pct, rr, cost_bps, expectancy_atr, composite, reasons, "
                    "trend_20, trend_63, trend_252, "
                    "ind_trend_20, ind_trend_63, ind_trend_252, "
                    "barrier_state, barrier_wide, p_up, p_dn, p_vert, ci_lo, ci_hi, n_eff, "
                    "barrier_reason",
            where=f"d = '{d}' AND run_id = '{run_id}'",
            order_by="action ASC, composite DESC", limit=200,
        )
        if not df.is_empty():
            df = df.unique(subset=["id"], keep="last").sort(
                ["action", "composite"], descending=[False, True]
            ).head(60)
        rows = []
        for r in _rows(df):
            rows.append({
                **{k: v for k, v in r.items()
                   if k not in ("barrier_state", "barrier_wide", "p_up", "p_dn", "p_vert",
                                "ci_lo", "ci_hi", "n_eff", "barrier_reason")},
                "barrier": {
                    "state": r.get("barrier_state") or "UNAVAILABLE",
                    "p_up": r.get("p_up"), "p_dn": r.get("p_dn"),
                    "p_vert": r.get("p_vert"),
                    "ci_lo": r.get("ci_lo"), "ci_hi": r.get("ci_hi"),
                    "n_eff": r.get("n_eff") or 0.0,
                    "wide": bool(r.get("barrier_wide")),
                    "reason": r.get("barrier_reason") or "",
                },
            })
        return {"as_of": d.isoformat() if d else None, "rows": rows}


# ----------------------------------------------------------------- rejections
@app.get("/api/rejections")
def rejections() -> dict:
    a = _now()
    with _ch() as ch:
        d = _latest_session(ch)
        if d is None:
            return {"as_of": None, "rows": []}
        # ev.rejections is append-only MergeTree by design, so without pinning
        # the run every past night's rejections pile up: 124 rows from only 18
        # distinct (symbol, rule_id) facts, some repeated ten times. No
        # background merge can ever hide this one.
        run_id = _latest_run(ch, a, d)
        if run_id is None:
            return {"as_of": d.isoformat() if d else None, "rows": []}
        df = ch.query_df(
            "SELECT symbol, rule_id, reason, detail FROM ev.rejections "
            f"WHERE d = '{d}' AND run_id = '{run_id}' "
            "ORDER BY rule_id, symbol LIMIT 500"
        )
        return {"as_of": d.isoformat() if d else None, "rows": _rows(df)}


# ------------------------------------------------------------------ portfolio
def _portfolio(ch: GuardedClient) -> Portfolio:
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
    except Exception as e:  # noqa: BLE001
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

    from ta_ledger.risk import Position
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
    cash = (EQUITY + realised_pnl) - cost_basis
    equity = cash + deployed
    return Portfolio(
        equity=equity,
        cash=cash,
        positions=items,
        watermark=max(EQUITY, equity),
    )


@app.get("/api/portfolio")
def portfolio() -> dict:
    with _ch() as ch:
        pf = _portfolio(ch)
        a = _now()
        try:
            # The live paper book ONLY. There used to be a fallback to "every
            # arm" when the live book had <2 nights, which spliced the
            # FROZEN_CONTROL backtest onto the live account as one line -- a
            # simulated -12.7% presented as your money. The UI has an honest
            # "needs 2 sessions" empty state; the backtest lives on /api/backtest.
            eq_df = ch.query_df(
                "SELECT d, arm, equity, cash, deployed, heat_pct, drawdown AS drawdown_pct "
                "FROM ev.equity_daily WHERE arm = 'DETERMINISTIC' ORDER BY d ASC LIMIT 1000"
            )
            if not eq_df.is_empty():
                eq_df = eq_df.unique(subset=["d"], keep="last").sort("d")
            equity_curve = _rows(eq_df)
        except Exception:  # noqa: BLE001
            equity_curve = []

        # ADR-0005 benchmark over the live book's own dates. md.benchmark_1d
        # was never populated, so this chart never had a benchmark line.
        benchmark_curve = []
        if equity_curve:
            try:
                lo, hi = equity_curve[0]["d"], equity_curve[-1]["d"]
                benchmark_curve = _rows(ch.query_pit(
                    "md.index_1d", a, columns="d, index_name AS index_id, close, knowledge_time",
                    where=f"index_name = 'Nifty 500' AND d >= '{lo}' AND d <= '{hi}'", order_by="d ASC",
                ).sort("knowledge_time").unique(subset=["d"], keep="last").sort("d").drop("knowledge_time"))
                for r in benchmark_curve:
                    r["series_kind"], r["tri_source"] = "PRICE", "PUBLISHED_PRICE"
            except Exception:  # noqa: BLE001
                benchmark_curve = []

        try:
            import json
            trades_df = ch.query_df(
                "SELECT seq, ts, run_id, kind, payload "
                "FROM ev.ledger_events ORDER BY seq DESC LIMIT 100"
            )
            trades = []
            for r in trades_df.iter_rows(named=True):
                try:
                    p_load = json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {})
                except (ValueError, TypeError):
                    p_load = {}
                trades.append({
                    "seq": int(r["seq"]),
                    "ts": r["ts"].isoformat() if hasattr(r["ts"], "isoformat") else str(r["ts"]),
                    "d": p_load.get("d"),
                    "symbol": p_load.get("symbol"),
                    "kind": str(r["kind"]),
                    "side": p_load.get("side"),
                    "qty": p_load.get("qty"),
                    "price": p_load.get("price"),
                    "notional": p_load.get("notional"),
                    "cost_rupees": p_load.get("cost_rupees"),
                    "pnl_rupees": p_load.get("pnl_rupees"),
                    "note": p_load.get("note"),
                })
        except Exception:  # noqa: BLE001
            trades = []

        return {
            "equity": pf.equity, "cash": pf.cash, "deployed": pf.deployed,
            "n_positions": len(pf.positions),
            "heat_pct": pf.heat_pct, "drawdown_pct": pf.drawdown_pct,
            "watermark": max(pf.watermark, pf.equity),
            "positions": [
                {"symbol": p.symbol, "industry": p.industry, "qty": p.qty,
                 "entry": p.entry, "last": p.last, "stop": p.stop,
                 "target": p.target if p.target > 0 else None,
                 "pnl": (p.last - p.entry) * p.qty,
                 "pnl_pct": (p.last / p.entry - 1) if p.entry else 0.0,
                 "risk_rupees": p.risk_rupees,
                 "opened": p.opened.isoformat() if hasattr(p.opened, "isoformat") else str(p.opened),
                 "horizon": p.horizon}
                for p in pf.positions
            ],
            "trades": trades,
            "equity_curve": equity_curve,
            "benchmark_curve": benchmark_curve,
        }


# ------------------------------------------------------------------ paper trading
@app.post("/api/paper/buy")
def paper_buy(body: dict) -> dict:
    """Execute a manual paper BUY order."""
    symbol = str(body.get("symbol") or "").upper().strip()
    if not symbol:
        raise HTTPException(400, "symbol is required")

    a = _now()
    with _ch() as ch:
        d = _latest_session(ch)
        if d is None:
            raise HTTPException(503, "no market data ingested")

        # Check latest recommendations
        rec_rows = []
        run_id = _latest_run(ch, a, d)
        if run_id:
            try:
                rec_df = ch.query_pit(
                    RECS, a,
                    where=f"symbol = '{symbol}' AND d = '{d}' AND run_id = '{run_id}'",
                    limit=1,
                )
                rec_rows = _rows(rec_df)
            except Exception:  # noqa: BLE001
                rec_rows = []
        rec = rec_rows[0] if rec_rows else {}

        # Determine price, qty, stop, target
        if body.get("price"):
            price = float(body["price"])
        elif rec.get("entry_lo") and rec.get("entry_hi"):
            price = round((float(rec["entry_lo"]) + float(rec["entry_hi"])) / 2.0, 2)
        else:
            try:
                bar = ch.query_pit("md.bars_1d", a, columns="close", where=f"symbol = '{symbol}' AND d = '{d}'", limit=1)
                price = float(bar["close"][0]) if bar.height else 100.0
            except Exception:  # noqa: BLE001
                price = 100.0

        qty = int(body.get("qty") or (rec.get("qty") if rec else None) or (rec.get("would_qty") if rec else None) or 100)
        stop = float(body.get("stop") or (rec.get("stop") if rec else round(price * 0.95, 2)))
        target = float(body.get("target") or (rec.get("target") if rec else round(price * 1.10, 2)))
        industry = str(rec.get("industry") or body.get("industry") or "")
        horizon = str(rec.get("horizon") or body.get("horizon") or "SWING")
        sleeve = str(rec.get("sleeve") or body.get("sleeve") or "MOMENTUM_BREAKOUT")

        pos_df = pl.DataFrame([{
            "as_of": a.ts,
            "symbol": symbol,
            "industry": industry,
            "qty": qty,
            "entry": price,
            "last": price,
            "stop": stop,
            "target": target,
            "opened": d,
            "horizon": horizon,
            "sleeve": sleeve,
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
        ch.insert_df("ev.positions", pos_df)

        from ta_ledger.events import GENESIS_HASH, EventKind, LedgerEvent, events_to_df
        try:
            events_df = ch.query_df("SELECT seq, ts, run_id, kind, payload, prev_hash, hash FROM ev.ledger_events ORDER BY seq ASC")
            if not events_df.is_empty():
                curr_seq = int(events_df["seq"].max()) + 1
                curr_prev_hash = str(events_df["hash"][-1])
            else:
                curr_seq = 1
                curr_prev_hash = GENESIS_HASH
        except Exception:  # noqa: BLE001
            curr_seq = 1
            curr_prev_hash = GENESIS_HASH

        event = LedgerEvent.create(
            seq=curr_seq,
            ts=a.ts,
            run_id=run_id or f"MANUAL-{d}",
            kind=EventKind.ENTRY_FILL,
            payload={
                "d": str(d),
                "symbol": symbol,
                "side": "BUY",
                "qty": qty,
                "price": price,
                "notional": round(qty * price, 2),
                "stop": stop,
                "target": target,
                "note": "manual paper BUY order",
            },
            prev_hash=curr_prev_hash,
        )
        ch.insert_df("ev.ledger_events", events_to_df([event]))

        try:
            pf_now = _portfolio(ch)
            eq_df = pl.DataFrame([{
                "d": d,
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

        return {"ok": True, "symbol": symbol, "qty": qty, "price": price}


@app.post("/api/paper/close")
def paper_close(body: dict) -> dict:
    """Execute a manual paper exit / close order."""
    symbol = str(body.get("symbol") or "").upper().strip()
    if not symbol:
        raise HTTPException(400, "symbol is required")

    a = _now()
    with _ch() as ch:
        d = _latest_session(ch)
        if d is None:
            raise HTTPException(503, "no market data ingested")

        try:
            pos_df = ch.query_df("SELECT as_of, symbol, industry, qty, entry, last, stop, target, opened, horizon, sleeve, status, pnl FROM ev.positions")
            if not pos_df.is_empty():
                open_matches = (
                    pos_df.sort("as_of")
                    .unique(subset=["symbol", "opened"], keep="last")
                    .filter((pl.col("status") == "OPEN") & (pl.col("symbol") == symbol))
                    .to_dicts()
                )
            else:
                open_matches = []
        except Exception as e:
            raise HTTPException(500, f"failed to query positions: {e}") from e

        if not open_matches:
            raise HTTPException(404, f"No open position found for {symbol}")

        pos = open_matches[0]
        pos_qty = int(pos["qty"])
        pos_entry = float(pos["entry"])
        exit_price = float(body.get("price") or pos["last"])

        from ta_core.costs import compute_costs
        from ta_core.enums import ProductType
        costs = compute_costs(pos_entry * pos_qty, exit_price * pos_qty, ProductType.DELIVERY).total
        pnl_rupees = round((exit_price - pos_entry) * pos_qty - costs, 2)

        pos_close_df = pl.DataFrame([{
            "as_of": a.ts,
            "symbol": symbol,
            "industry": str(pos.get("industry") or ""),
            "qty": pos_qty,
            "entry": pos_entry,
            "last": exit_price,
            "stop": float(pos["stop"]),
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

        from datetime import date as _date
        risk_unit = (pos_entry - float(pos["stop"])) if (pos_entry - float(pos["stop"])) > 0 else 1.0
        r_multiple = round((exit_price - pos_entry) / risk_unit, 4)
        opened_d = pos["opened"] if isinstance(pos["opened"], _date) else datetime.strptime(str(pos["opened"]), "%Y-%m-%d").date()
        holding_days = (d - opened_d).days

        outcome_df = pl.DataFrame([{
            "rec_id": f"P-{symbol}-{d}",
            "symbol": symbol,
            "arm": "DETERMINISTIC",
            "horizon": str(pos.get("horizon") or "SWING"),
            "entry_date": opened_d,
            "entry_price": pos_entry,
            "exit_date": d,
            "exit_price": exit_price,
            "barrier_hit": "MANUAL",
            "r_multiple": r_multiple,
            "pnl_rupees": pnl_rupees,
            "mae_r": 0.0,
            "mfe_r": 0.0,
            "holding_days": max(0, holding_days),
            "benchmark_ret": 0.0,
            "stated_conf": 0.0,
            "calibrated_p": 0.0,
            "uniqueness_w": 1.0,
            "regime": "UNKNOWN",
            "degraded_mode": 0,
            "knowledge_time": a.ts,
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

        from ta_ledger.events import GENESIS_HASH, EventKind, LedgerEvent, events_to_df
        try:
            events_df = ch.query_df("SELECT seq, ts, run_id, kind, payload, prev_hash, hash FROM ev.ledger_events ORDER BY seq ASC")
            if not events_df.is_empty():
                curr_seq = int(events_df["seq"].max()) + 1
                curr_prev_hash = str(events_df["hash"][-1])
            else:
                curr_seq = 1
                curr_prev_hash = GENESIS_HASH
        except Exception:  # noqa: BLE001
            curr_seq = 1
            curr_prev_hash = GENESIS_HASH

        event = LedgerEvent.create(
            seq=curr_seq,
            ts=a.ts,
            run_id=f"MANUAL-{d}",
            kind=EventKind.EXIT_FILL,
            payload={
                "d": str(d),
                "symbol": symbol,
                "side": "SELL",
                "qty": pos_qty,
                "price": exit_price,
                "notional": round(pos_qty * exit_price, 2),
                "cost_rupees": round(costs, 2),
                "pnl_rupees": pnl_rupees,
                "barrier_hit": "MANUAL",
                "note": "manual paper close order",
            },
            prev_hash=curr_prev_hash,
        )
        ch.insert_df("ev.ledger_events", events_to_df([event]))

        try:
            pf_now = _portfolio(ch)
            eq_df = pl.DataFrame([{
                "d": d,
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

        return {"ok": True, "symbol": symbol, "pnl_rupees": pnl_rupees}


# ------------------------------------------------------------------- backtest
def _iso(ts) -> str:
    """A run timestamp as ISO-8601 UTC ("2026-11-11T17:43:26.897Z"), whatever the driver returns."""
    from datetime import datetime as _dt

    if isinstance(ts, str):
        try:
            ts = _dt.fromisoformat(ts.replace(" ", "T"))
        except ValueError:
            return ts
    if isinstance(ts, _dt):
        ts = ts.astimezone(UTC) if ts.tzinfo else ts.replace(tzinfo=UTC)
        return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return str(ts)


_RUN_COLS = (
    "run_id, run_ts, line, sessions, start_d, end_d, n_sessions, n_trades, initial_equity, "
    "final_equity, total_return, sharpe, psr, dsr, min_trl, win_rate, max_drawdown, "
    "bench_index, bench_return, excess_return, "
    "sim_status, sim_horizon, sim_block_len, sim_n_paths, sim_p_lo, sim_p_hi, sim_p_loss, "
    "sim_hist_lo, sim_hist_count, curve_d, curve_equity, curve_drawdown, curve_heat, curve_regime"
)


def _latest_backtest_run(ch, line: str, sessions: int | None, run_id: str | None = None) -> dict | None:
    """Latest stored run of `line` for a window (or exactly `run_id`)."""
    where = [f"line = '{line}'"]
    if run_id is not None:
        where.append(f"run_id = '{run_id}'")
    elif sessions is not None:
        where.append(f"sessions = {int(sessions)}")
    df = ch.query_df(f"SELECT {_RUN_COLS} FROM ev.backtest_runs WHERE {' AND '.join(where)} "
                     "ORDER BY run_ts DESC LIMIT 1")
    return None if df.is_empty() else df.row(0, named=True)


def _stored_windows(ch) -> list[dict]:
    """Every window with a stored control run, and when it was last run."""
    df = ch.query_df(
        "SELECT sessions, max(run_ts) AS last_run, argMax(start_d, run_ts) AS start_d, "
        "argMax(end_d, run_ts) AS end_d, argMax(total_return, run_ts) AS total_return "
        "FROM ev.backtest_runs WHERE line = 'FROZEN_CONTROL' GROUP BY sessions ORDER BY sessions"
    )
    return [{"sessions": int(r["sessions"]), "last_run": _iso(r["last_run"]), "start_d": str(r["start_d"]),
             "end_d": str(r["end_d"]), "total_return_pct": float(r["total_return"])} for r in _rows(df)]


@app.get("/api/backtest")
def backtest(sessions: int | None = None) -> dict:
    """The stored backtest for one window -- the last run of THAT window.

    Each window (63 / 126 / 252 / 750 / 1250 sessions, or any CLI value) keeps
    its own latest run in ev.backtest_runs, so switching the selector shows the
    last result for that window until it is re-run. With no `sessions`, the most
    recent run of any window. Statistics are the job's own, stored with the run.
    """
    a = _now()
    weights_verified, weights_sha256 = False, ""
    try:
        from ta_quant.weights import EXPECTED_V1_SHA256, verify_v1_integrity
        weights_verified = bool(verify_v1_integrity())
        weights_sha256 = str(EXPECTED_V1_SHA256)
    except Exception:  # noqa: BLE001
        pass
    try:
        from ta_ledger.events import verify_chain
        verify_chain([])
        ledger_verified = True
    except Exception:  # noqa: BLE001
        ledger_verified = False

    base = {
        "arm": "FROZEN_CONTROL", "weights_version": "v1", "weights_verified": weights_verified,
        "weights_sha256": weights_sha256, "ledger_verified": ledger_verified, "needed_sessions": 63,
    }
    with _ch() as ch:
        try:
            windows = _stored_windows(ch)
            run = _latest_backtest_run(ch, "FROZEN_CONTROL", sessions)
        except Exception:  # noqa: BLE001
            windows, run = [], None
        if run is None:
            return {
                **base, "state": "NOT_RUN", "stats_state": "INSUFFICIENT", "sessions": sessions,
                "windows": windows, "n_sessions": 0, "n_trades": 0, "final_equity": None,
                "total_return_pct": None, "sharpe": None, "win_rate": None, "psr": None, "dsr": None,
                "min_trl": None, "max_drawdown": None, "equity_curve": [], "benchmark_curve": [],
                "comparison": None, "run": None,
                "note": (f"No backtest has been stored for the {sessions}-session window yet. "
                         "Click 'Run historical backtest' to create one." if sessions else
                         "No backtest has been run yet."),
            }

        curve = [
            {"d": str(d_), "arm": "FROZEN_CONTROL", "equity": e_, "drawdown_pct": dd_, "heat_pct": h_, "regime": g_}
            for d_, e_, dd_, h_, g_ in zip(run["curve_d"], run["curve_equity"], run["curve_drawdown"],
                                           run["curve_heat"], run["curve_regime"], strict=True)
        ]
        lo, hi = run["start_d"], run["end_d"]
        bm_rows: list[dict] = []
        try:
            # The published Nifty 500 PRICE index (the free daily file has no
            # TRI) over exactly this run's dates. ReplacingMergeTree keeps
            # re-ingested days twice until a merge: dedupe on read.
            bm_rows = _rows(ch.query_pit(
                "md.index_1d", a, columns="d, index_name AS index_id, close, knowledge_time",
                where=f"index_name = 'Nifty 500' AND d >= '{lo}' AND d <= '{hi}'", order_by="d ASC",
            ).sort("knowledge_time").unique(subset=["d"], keep="last").sort("d").drop("knowledge_time"))
            for r in bm_rows:
                r["series_kind"], r["tri_source"] = "PRICE", "PUBLISHED_PRICE"
        except Exception:  # noqa: BLE001
            bm_rows = []

        cmp = None
        try:
            # The variant from the SAME request if there is one, else the latest
            # for this window; drawn only if it covers exactly the same dates.
            v = _latest_backtest_run(ch, "REGIME_FILTER", None, run_id=run["run_id"]) or \
                _latest_backtest_run(ch, "REGIME_FILTER", int(run["sessions"]))
            if v is not None:
                same = str(v["start_d"]) == str(lo) and str(v["end_d"]) == str(hi)
                cmp = {
                    "variant": "REGIME_FILTER", "same_window": same, "run_ts": _iso(v["run_ts"]),
                    "start_d": str(v["start_d"]), "end_d": str(v["end_d"]),
                    "n_sessions": v["n_sessions"], "n_trades": v["n_trades"],
                    "final_equity": v["final_equity"], "total_return_pct": v["total_return"],
                    "sharpe": v["sharpe"], "max_drawdown": v["max_drawdown"], "win_rate": v["win_rate"],
                    "excess_return_pct": v["excess_return"],
                    "curve": [{"d": str(d_), "equity": e_, "regime": g_} for d_, e_, g_ in
                              zip(v["curve_d"], v["curve_equity"], v["curve_regime"], strict=True)] if same else [],
                }
        except Exception:  # noqa: BLE001
            cmp = None

    n = int(run["n_sessions"])
    enough = n >= base["needed_sessions"]
    return {
        **base, "state": "READY", "sessions": int(run["sessions"]), "windows": windows,
        "run": {"run_id": run["run_id"], "run_ts": _iso(run["run_ts"]), "start_d": str(lo), "end_d": str(hi)},
        "stats_state": "OK" if enough else "INSUFFICIENT",
        "n_sessions": n, "n_trades": int(run["n_trades"]), "final_equity": float(run["final_equity"]),
        "total_return_pct": float(run["total_return"]), "sharpe": float(run["sharpe"]),
        "win_rate": float(run["win_rate"]), "max_drawdown": float(run["max_drawdown"]),
        # ADR-0005: the index every strategy must beat, over THIS run's dates.
        "benchmark": {
            "index": run["bench_index"] or "Nifty 500", "series": "PRICE (dividends excluded)",
            "return_pct": run["bench_return"], "excess_return_pct": run["excess_return"],
            "beats_benchmark": (None if run["excess_return"] is None else bool(run["excess_return"] > 0)),
        },
        "psr": run["psr"] if enough else None, "dsr": run["dsr"] if enough else None,
        "min_trl": run["min_trl"] if enough else None,
        "note": ("Deflated statistics computed by the backtest job and stored with this run."
                 if enough else f"Deflated statistics need at least 63 sessions; this run has {n}."),
        "equity_curve": curve, "benchmark_curve": bm_rows, "comparison": cmp,
    }


@app.post("/api/backtest/run")
def run_backtest_api(body: dict | None = None) -> dict:
    """Run the control backtest for one window and, unless `compare` is false,
    the ADR-0004 regime-filter variant over the SAME window. Both are stored in
    ev.backtest_runs under that window with one shared run_id, replacing only
    that window's previous result. Returns the window's stored result."""
    import uuid

    from ta_quant.nightly import execute_backtest
    sessions = int((body or {}).get("sessions") or 126)
    run_id = f"BT-{uuid.uuid4().hex[:12]}"
    if not (body or {}).get("compare", True):
        execute_backtest(sessions=sessions, persist_results=True, run_id=run_id)
        return backtest(sessions)
    # The two runs are independent and write DIFFERENT rows (line =
    # FROZEN_CONTROL / REGIME_FILTER; only the control touches ev.equity_daily,
    # I-28), so they run side by side in two processes. Processes, not threads:
    # the session loop is Python-bound and would serialise on the GIL. Sessions
    # WITHIN a run cannot be parallelised -- each day depends on the prior book.
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=2, mp_context=mp.get_context("spawn")) as pool:
        control = pool.submit(execute_backtest, sessions, persist_results=True, run_id=run_id)
        variant = pool.submit(execute_backtest, sessions, regime_filter=True, run_id=run_id)
        control.result()
        variant.result()
    return backtest(sessions)


# ----------------------------------------------------------------------- risk
@app.get("/api/risk")
def risk() -> dict:
    with _ch() as ch:
        d = _latest_session(ch)
        if d is None:
            return {"as_of": None, "regime": Regime.UNKNOWN.value, "rules": []}
        pf = _portfolio(ch)
        try:
            b = _rows(ch.query_pit(
                "md.breadth_daily", _now(),
                where=f"d = '{d}' AND universe = 'NIFTY500'", limit=1,
            ))
            regime, _ = _live_regime(ch, _now(), d, b[0] if b else {})
        except Exception:  # noqa: BLE001
            regime = Regime.UNKNOWN
        return {
            "as_of": d.isoformat() if d else None,
            "regime": regime.value,
            "rules": [
                {"id": s.id, "label": s.label, "state": s.state.value,
                 "value": s.value, "limit": s.limit, "unit": s.unit,
                 "headroom_pct": s.headroom_pct, "detail": s.detail}
                for s in rule_states(pf, regime=regime, cfg=DEFAULT)
            ],
        }


@app.get("/api/industries")
def industries() -> dict:
    with _ch() as ch:
        d = _latest_session(ch)
        if d is None:
            return {"as_of": None, "rows": []}
        pf = _portfolio(ch)
        cfg = DEFAULT
        names = sorted({p.industry for p in pf.positions})
        rows = []
        for ind in names:
            cap = (cfg.finserv_max_pct if ind == cfg.finserv_industry
                   else cfg.max_industry_pct)
            notional = pf.industry_notional(ind)
            headroom_rupees = pf.equity * cap - notional
            rows.append({
                "industry": ind, "notional": notional,
                "pct_equity": notional / pf.equity if pf.equity else 0.0,
                "n_names": pf.industry_names(ind), "cap_pct": cap,
                # Both, explicitly named. `headroom` alone was ambiguous: the
                # dashboard could not tell rupees from a fraction and had to
                # guess from magnitude, which silently stops working the day
                # a fraction happens to exceed 1.0.
                "headroom_rupees": headroom_rupees,
                "headroom_frac": (headroom_rupees / (pf.equity * cap)
                                  if pf.equity and cap else None),
                "headroom": headroom_rupees,   # deprecated alias
            })
        return {"as_of": d.isoformat() if d else None, "rows": rows}


# ---------------------------------------------------------------------- trend
@app.get("/api/trend/{symbol}")
def trend(symbol: str) -> dict:
    a = _now()
    sym = "".join(c for c in symbol.upper() if c.isalnum() or c in "-&")[:24]
    with _ch() as ch:
        d = _latest_session(ch)
        t = _rows(ch.query_pit(
            "md.trend_1d", a,
            columns="trend_20, trend_63, trend_252, er_63, adx14, t_drift_63, flips_63d",
            where=f"symbol = '{sym}' AND d = '{d}'", limit=1,
        ))
        bars = ch.query_pit(
            "md.bars_1d", a,
            columns="d, open AS o, high AS h, low AS l, close AS c, volume AS v",
            where=f"symbol = '{sym}'", order_by="d DESC", limit=260,
        )
        if not t and bars.is_empty():
            raise HTTPException(404, f"no data for {sym}")
        row = t[0] if t else {}
        return {
            "symbol": sym,
            "states": {"t20": row.get("trend_20"), "t63": row.get("trend_63"),
                       "t252": row.get("trend_252")},
            "er_63": row.get("er_63"), "adx14": row.get("adx14"),
            "slope_t_63": row.get("t_drift_63"), "flips_63d": row.get("flips_63d"),
            "rs_vs_bench_63": None,
            "bars": list(reversed(_rows(bars))),
        }


# --------------------------------------------------- calibration & simulation
#: Only outcomes of recommendations the system actually made count toward
#: calibration. FROZEN_CONTROL rows are a backtest of a different rule set;
#: counting them is how this endpoint once said "OK" with zero bins.
_CALIBRATION_ARMS = ("DETERMINISTIC", "SHADOW_BUY", "SHADOW_WATCH")


@app.get("/api/calibration")
def calibration() -> dict:
    """Never "OK" until a calibrator exists (M8) -- a sample count is not calibration.

    It previously returned OK once ANY 30 rows sat in ev.outcomes (backtest
    included) while sending no bins, BSS or resolution: a sufficiency badge
    over nothing. Now:

    * n_eff counts per-symbol EPISODES among live + shadow outcomes: windows
      on the same name that overlap in time merge into one, because a WATCH
      card repeated five days running is one path through one price move,
      not five tests. (An earlier `uniqExact(symbol, exit_date)` still
      counted those repeats whenever they resolved on different days.)
    * Below 30 -> INSUFFICIENT. At or above 30 -> NOT_BUILT: enough evidence
      exists, but the isotonic calibrator that would use it is M8 work.
    """
    arms = ", ".join(f"'{a}'" for a in _CALIBRATION_ARMS)
    with _ch() as ch:
        try:
            row = ch.query_df(
                "SELECT count() AS n_raw, countIf(new_episode) AS n_eff, "
                "uniqExact(symbol) AS n_symbols, uniqExact(exit_date) AS n_exits FROM ("
                "  SELECT symbol, exit_date, entry_date > max(exit_date) OVER ("
                "    PARTITION BY symbol ORDER BY entry_date, exit_date "
                "    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS new_episode"
                f"  FROM ev.outcomes WHERE arm IN ({arms}) "
                "  AND knowledge_time <= '2099-01-01 00:00:00')"
            ).row(0, named=True)
            n_raw, n_eff = int(row["n_raw"]), int(row["n_eff"])
            n_symbols, n_exits = int(row["n_symbols"]), int(row["n_exits"])
        except Exception:  # noqa: BLE001
            n_raw = n_eff = n_symbols = n_exits = 0
    needed = 30
    if n_eff < needed:
        state = "INSUFFICIENT"
        note = (f"calibration needs at least {needed} independent resolved cards; have {n_eff} "
                f"({n_raw} outcomes on {n_symbols} names over {n_exits} exit sessions, overlapping "
                f"repeats merged). Until then the system cannot say how well its stated "
                f"probabilities match reality.")
    else:
        state = "NOT_BUILT"
        note = (f"{n_eff} independent resolved cards exist (threshold {needed}; {n_raw} outcomes "
                f"on {n_symbols} names over only {n_exits} exit sessions, so they share market "
                f"moves), but no calibrator is built yet: card probabilities are raw volatility-"
                f"bucket base rates. Isotonic calibration arrives in M8; until then no "
                f"calibration figure is claimed.")
    return {
        "state": state, "n_eff": float(n_eff), "n_raw": n_raw, "n_symbols": n_symbols,
        "n_exit_sessions": n_exits, "needed": needed,
        "bins": [], "resolution": None, "bss": None, "note": note,
    }


@app.get("/api/simulation")
def simulation(sessions: int | None = None) -> dict:
    """The forward Monte Carlo of the stored backtest for one window.

    Computed by the backtest job and stored with the run (ev.backtest_runs), so
    it follows the window selector exactly like the backtest panel does: a
    252-session window shows its own (refusing) verdict even after a 1250-
    session run produced a full one.
    """
    from ta_quant.stats import SIM_MIN_DAYS, SIM_MIN_TRADES

    base = {"needed_days": SIM_MIN_DAYS, "needed_trades": SIM_MIN_TRADES, "sessions": sessions}
    with _ch() as ch:
        try:
            r = _latest_backtest_run(ch, "FROZEN_CONTROL", sessions)
        except Exception:  # noqa: BLE001
            r = None
    if r is None:
        return {**base, "status": "INSUFFICIENT", "n_days": 0, "n_trades": 0,
                "p_lo": None, "p_hi": None, "block_len": None, "histogram": [],
                "note": (f"no backtest is stored for the {sessions}-session window yet; the simulation "
                         "bootstraps a stored backtest's realised daily returns." if sessions else
                         "no backtest has been run yet; the simulation bootstraps the backtest's realised "
                         "daily returns.")}
    span = f"backtest {r['start_d']} .. {r['end_d']} ({r['sessions']}-session window)"
    n_days, n_trades = int(r["n_sessions"]), int(r["n_trades"])
    if r["sim_status"] != "OK":
        return {**base, "status": "INSUFFICIENT", "n_days": n_days, "n_trades": n_trades,
                "p_lo": None, "p_hi": None, "block_len": None, "histogram": [],
                "note": (f"forward Monte Carlo needs {SIM_MIN_DAYS} sessions and {SIM_MIN_TRADES} closed "
                         f"trades of backtest; this {span} has {n_days} and {n_trades}. A distribution "
                         f"fitted to less is a picture of noise. Select or run the 750- or 1250-session window.")}
    return {
        **base, "status": "OK", "n_days": n_days, "n_trades": n_trades,
        "p_lo": r["sim_p_lo"], "p_hi": r["sim_p_hi"], "p_loss": r["sim_p_loss"],
        "block_len": r["sim_block_len"], "horizon": r["sim_horizon"], "n_paths": r["sim_n_paths"],
        "histogram": [{"bucket": b, "count": c} for b, c in zip(r["sim_hist_lo"], r["sim_hist_count"], strict=True)],
        "note": (f"{r['sim_n_paths']:,} block-bootstrap paths of {r['sim_horizon']} sessions from the {span} "
                 f"realised daily returns (FROZEN_CONTROL rules). Interval is the 5th-95th percentile of "
                 f"one-year return; {r['sim_p_loss']:.0%} of paths lose money. It assumes the future "
                 f"resembles that history -- it is not a forecast."),
    }


# ------------------------------------------------------------------ SPA mount
@app.middleware("http")
async def _cache_policy(request, call_next):
    """Tell browsers exactly how to cache, instead of letting them guess.

    Without a Cache-Control header, Chrome applies heuristic freshness and can
    keep serving an OLD index.html long after a rebuild. That old page points
    at a hashed bundle (`/assets/index-<hash>.js`) which the rebuild deleted,
    so the page loads blank -- in Chrome, which had cached it during
    development, while a browser that had never visited loads fine.

    Policy:
      * /assets/*  -- content-hashed, so the URL changes whenever the bytes
                      do: cache for a year, immutable.
      * /api/*     -- never cache; every response is "as of now".
      * everything else (the SPA shell) -- `no-cache`: keep a copy but
                      revalidate on every load, so a rebuild is picked up
                      immediately via the ETag.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    else:
        response.headers["Cache-Control"] = "no-cache"
    return response


# .../apps/api/src/ta_api/main.py -> parents[3] is .../apps
_DIST = Path(__file__).resolve().parents[3] / "ui" / "dist"
if _DIST.is_dir():
    # Mounted LAST so it never shadows /api/*. html=True makes unknown paths
    # fall back to index.html, which is what a client-side router needs.
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="ui")
else:
    log.warning("UI bundle not found at %s; run `npm run build` in apps/ui", _DIST)
