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
    allow_methods=["GET"], allow_headers=["*"],
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
    # Hand-written because build_pit_query parenthesises `where`, which a
    # GROUP BY cannot live inside. Still bounded, and `as_of` is passed so the
    # guard checks the bound for EQUALITY rather than mere presence.
    runs = ch.query_df(
        f"SELECT run_id FROM {RECS} "
        f"WHERE knowledge_time <= {a.sql()} AND d = '{d}' "
        f"GROUP BY run_id ORDER BY max(as_of) DESC LIMIT 1",
        a,
    )
    return runs["run_id"][0] if not runs.is_empty() else None


def _latest_session(ch: GuardedClient):
    df = ch.query_df(
        "SELECT max(d) AS d FROM md.bars_1d "
        "WHERE knowledge_time <= '2099-01-01 00:00:00'"
    )
    return df["d"][0] if df.height else None


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
        regime, note = _regime_from_breadth(b)
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
        pos = ch.query_df(
            "SELECT symbol, industry, qty, entry, last, stop, opened "
            "FROM ev.positions WHERE status = 'OPEN'"
        )
    except Exception:  # noqa: BLE001
        pos = pl.DataFrame()
    from ta_ledger.risk import Position
    items = tuple(
        Position(symbol=r["symbol"], industry=r["industry"], qty=int(r["qty"]),
                 entry=float(r["entry"]), last=float(r["last"]),
                 stop=float(r["stop"]), opened=r["opened"])
        for r in pos.iter_rows(named=True)
    ) if pos.height else ()
    deployed = sum(p.notional for p in items)
    return Portfolio(equity=EQUITY, cash=EQUITY - deployed, positions=items,
                     watermark=EQUITY)


@app.get("/api/portfolio")
def portfolio() -> dict:
    with _ch() as ch:
        pf = _portfolio(ch)
        return {
            "equity": pf.equity, "cash": pf.cash, "deployed": pf.deployed,
            "n_positions": len(pf.positions),
            "heat_pct": pf.heat_pct, "drawdown_pct": pf.drawdown_pct,
            "watermark": max(pf.watermark, pf.equity),
            "positions": [
                {"symbol": p.symbol, "industry": p.industry, "qty": p.qty,
                 "entry": p.entry, "last": p.last, "stop": p.stop, "target": None,
                 "pnl": (p.last - p.entry) * p.qty,
                 "pnl_pct": (p.last / p.entry - 1) if p.entry else 0.0,
                 "risk_rupees": p.risk_rupees,
                 "opened": p.opened.isoformat(), "horizon": "SWING"}
                for p in pf.positions
            ],
        }


# ----------------------------------------------------------------------- risk
@app.get("/api/risk")
def risk() -> dict:
    with _ch() as ch:
        d = _latest_session(ch)
        pf = _portfolio(ch)
        try:
            b = _rows(ch.query_pit(
                "md.breadth_daily", _now(),
                where=f"d = '{d}' AND universe = 'NIFTY500'", limit=1,
            ))
            regime, _ = _regime_from_breadth(b[0] if b else {})
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
@app.get("/api/calibration")
def calibration() -> dict:
    """Honest by construction: nothing has resolved yet, so there is nothing
    to calibrate, and the endpoint says so rather than drawing an empty chart
    that implies calibration is known and perfect."""
    with _ch() as ch:
        try:
            n = ch.query_df("SELECT count() AS n FROM ev.outcomes")["n"][0]
        except Exception:  # noqa: BLE001
            n = 0
    return {
        "state": "OK" if n >= 30 else "INSUFFICIENT",
        "n_eff": float(n), "needed": 30, "bins": [],
        "resolution": None, "bss": None,
        "note": ("calibration needs at least 30 resolved recommendations; "
                 f"{n} have resolved so far. Until then the system cannot say "
                 "how well its stated probabilities match reality."),
    }


@app.get("/api/simulation")
def simulation() -> dict:
    with _ch() as ch:
        try:
            days = ch.query_df("SELECT count() AS n FROM ev.equity_daily")["n"][0]
            trades = ch.query_df("SELECT count() AS n FROM ev.outcomes")["n"][0]
        except Exception:  # noqa: BLE001
            days, trades = 0, 0
    enough = days >= 750 and trades >= 200
    return {
        "status": "OK" if enough else "INSUFFICIENT",
        "n_days": int(days), "n_trades": int(trades),
        "needed_days": 750, "needed_trades": 200,
        "p_lo": None, "p_hi": None, "block_len": None,
        "histogram": [],
        "note": (f"forward Monte Carlo needs 750 engine-days and 200 closed trades; "
                 f"have {days} and {trades}. A distribution fitted to this little "
                 f"history would be a picture of noise."),
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
