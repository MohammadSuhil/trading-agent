"""Backfill and daily ingest."""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import polars as pl

from .http import PoliteClient
from .parse.constituents import INDEX_FILES
from .sources.nse_archive import NSEArchiveSource
from .store import GuardedClient

log = logging.getLogger(__name__)

BARS_COLS = [
    "d", "symbol", "isin", "series", "open", "high", "low", "close",
    "prev_close", "last_price", "volume", "turnover", "n_trades",
    "deliv_qty", "deliv_pct", "knowledge_time", "source",
]


def ingest_sessions(start: date, end: date, *, raw_root: str = "data/raw") -> dict:
    """Fetch and store every session in [start, end]. Idempotent.

    Safe to re-run: `md.bars_1d` is a ReplacingMergeTree keyed on (symbol, d),
    and the HTTP layer caches immutable bodies, so a repeat run costs almost
    nothing and cannot double-count.
    """
    stats = {"requested": 0, "sessions": 0, "holidays": 0, "rows": 0, "failed": 0,
             "index_rows": 0, "index_failed": 0}
    with PoliteClient(raw_root) as http, GuardedClient() as ch:
        src = NSEArchiveSource(http)
        d = start
        while d <= end:
            # Weekends are probed too: NSE trades some (Muhurat, Budget-day and
            # DR-drill sessions -- 2026-11-08 was one). Skipping them left
            # silent holes: the next bar's 1-day return then spans two
            # sessions. A weekend 404 costs one request and means "no session".
            stats["requested"] += 1
            try:
                res = src.fetch_session(d)
            except Exception as e:  # noqa: BLE001
                log.warning("%s failed: %s", d, e)
                stats["failed"] += 1
                d += timedelta(days=1)
                continue

            if not res.was_session:
                stats["holidays"] += 1
            else:
                n = ch.insert_df("md.bars_1d", res.bars.select(BARS_COLS))
                stats["sessions"] += 1
                stats["rows"] += n
                log.info("%s: %d rows", d, n)
                # Index OHLC feeds the regime filter. Its failure must not
                # cost the day's bars, so it is counted, never raised.
                try:
                    idx = src.fetch_index_close(d)
                    if idx is not None and idx.height:
                        stats["index_rows"] += ch.insert_df("md.index_1d", idx)
                except Exception as e:  # noqa: BLE001
                    log.warning("%s index close failed: %s", d, e)
                    stats["index_failed"] += 1
            d += timedelta(days=1)
    return stats


def refresh_constituents(
    indices: tuple[str, ...] = ("NIFTY50", "NIFTY100", "NIFTY200", "NIFTY500"),
    snapshot_date: date | None = None,
    *, raw_root: str = "data/raw",
) -> dict:
    """Take a DATED snapshot of index membership + industry labels."""
    snap = snapshot_date or datetime.now(UTC).date()
    stats: dict[str, int] = {}
    with PoliteClient(raw_root) as http, GuardedClient() as ch:
        src = NSEArchiveSource(http)
        for idx in indices:
            if idx not in INDEX_FILES:
                continue
            try:
                df = src.fetch_constituents(idx, snap)
                stats[idx] = ch.insert_df("md.index_members", df)
                log.info("%s: %d members", idx, stats[idx])
            except Exception as e:  # noqa: BLE001
                log.warning("constituents %s failed: %s", idx, e)
                stats[idx] = 0
    return stats


def refresh_symbol_master(*, raw_root: str = "data/raw") -> int:
    """NSE's EQUITY_L.csv -- the listed-security master."""
    import io
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    with PoliteClient(raw_root) as http, GuardedClient() as ch:
        blob = http.get_bytes(url, subdir="nse_master", reuse=False, archive=True)
        if not blob:
            return 0
        df = pl.read_csv(io.BytesIO(blob), infer_schema_length=1000)
        # NSE ships EQUITY_L.csv with LEADING SPACES in most headers
        # (" ISIN NUMBER", " SERIES"). Normalise once, then look every column up
        # through the map -- the previous version fell back to a bare literal
        # name on miss, which could never match the real spaced header.
        cols = {c.strip().upper(): c for c in df.columns}
        missing = [c for c in ("SYMBOL", "ISIN NUMBER", "NAME OF COMPANY", "SERIES")
                   if c not in cols]
        if missing:
            raise RuntimeError(f"EQUITY_L.csv missing {missing}; got {sorted(cols)}")
        out = df.select(
            pl.col(cols["SYMBOL"]).cast(pl.Utf8).str.strip_chars().alias("symbol"),
            pl.col(cols["ISIN NUMBER"]).cast(pl.Utf8).str.strip_chars().alias("isin"),
            pl.col(cols["NAME OF COMPANY"]).cast(pl.Utf8).str.strip_chars().alias("name"),
            pl.col(cols["SERIES"]).cast(pl.Utf8).str.strip_chars().alias("series"),
        ).with_columns(
            pl.lit(date(1990, 1, 1)).alias("listed_date"),
            pl.lit(datetime.now(UTC).replace(tzinfo=None)).cast(pl.Datetime("us")).alias("updated_at"),
        )
        return ch.insert_df("ref.symbols", out)


def ingest_index_history(start: date, end: date, *, raw_root: str = "data/raw") -> dict:
    """Backfill `md.index_1d` alone, for dates that predate the bars we can parse.

    The UDiFF bhavcopy only exists from mid-2024, but the index close file
    goes back years. Index history is what lets the regime filter be checked
    out of sample (ADR-0004) -- on periods the strategy was never run on.
    """
    stats = {"requested": 0, "sessions": 0, "holidays": 0, "rows": 0, "failed": 0}
    with PoliteClient(raw_root) as http, GuardedClient() as ch:
        src = NSEArchiveSource(http)
        d = start
        while d <= end:
            # weekends too -- see ingest_sessions
            stats["requested"] += 1
            try:
                idx = src.fetch_index_close(d)
                if idx is None or idx.is_empty():
                    stats["holidays"] += 1
                else:
                    stats["rows"] += ch.insert_df("md.index_1d", idx)
                    stats["sessions"] += 1
            except Exception as e:  # noqa: BLE001
                log.warning("%s index close failed: %s", d, e)
                stats["failed"] += 1
            d += timedelta(days=1)
    return stats
