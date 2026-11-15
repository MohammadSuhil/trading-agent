"""NSE daily index close file (`ind_close_all_DDMMYYYY.csv`).

One published archive file per session carrying OHLC for every NSE index.
It exists because the regime filter (ADR-0004) is defined on the *benchmark
index*, and a turnover-weighted reconstruction from constituent bars has no
honest intraday high/low -- so no index ATR -- and silently drifts from the
real index as weights change.

Fetched from the same `nsearchives` host as the bhavcopy: a plain published
file, no cookies, no bot-protection workarounds.
"""
from __future__ import annotations

import io
from datetime import date, datetime

import polars as pl

INDEX_COLS = ["d", "index_name", "open", "high", "low", "close", "knowledge_time"]


def index_close_url(d: date) -> str:
    """
    >>> from datetime import date
    >>> index_close_url(date(2026, 11, 11))
    'https://nsearchives.nseindia.com/content/indices/ind_close_all_11112026.csv'
    """
    return f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{d:%d%m%Y}.csv"


def parse_index_close(
    blob: bytes, *, knowledge_time: datetime, session: date | None = None
) -> pl.DataFrame:
    """Parse the file into `md.index_1d` rows.

    Values NSE leaves blank or as "-" become null rather than 0: an index with
    no published high is unknown, not an index that traded at zero. Rows
    without a close are dropped -- a regime computed on a gap would be a
    regime computed on a guess.

    **Date order is not stable.** Most files write DD-MM-YYYY, but some (seen in
    April 2023) write MM-DD-YYYY, so "04-06-2023" is 6 April, not 4 June. When
    `session` is given, the reading that yields that session wins, and a file
    matching neither raises -- silently filing an index row on the wrong day
    corrupts every SMA that spans it.

    >>> from datetime import UTC
    >>> raw = (b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
    ...        b"Closing Index Value,Points Change\\n"
    ...        b"Nifty 50,11-11-2026,25337.8,25424.35,25095.35,25255.05,210.4\\n"
    ...        b"Odd Index,11-11-2026,-,-,-,-,-\\n")
    >>> df = parse_index_close(raw, knowledge_time=datetime(2026, 11, 11, 13, 0, tzinfo=UTC))
    >>> df.select("index_name", "high", "close").rows()
    [('Nifty 50', 25424.35, 25255.05)]
    >>> us = raw.replace(b"11-11-2026", b"04-06-2023")     # month-first file
    >>> parse_index_close(us, knowledge_time=datetime(2023, 4, 6, 13, tzinfo=UTC),
    ...                   session=date(2023, 4, 6))["d"].to_list()
    [datetime.date(2023, 4, 6)]
    """
    df = pl.read_csv(io.BytesIO(blob), infer_schema=False)
    df = df.rename({c: c.strip() for c in df.columns})

    def num(col: str) -> pl.Expr:
        return (
            pl.col(col).str.strip_chars().replace({"-": None, "": None})
            .cast(pl.Float64, strict=False)
        )

    fmts = ("%d-%m-%Y", "%m-%d-%Y")
    fmt = fmts[0]
    if session is not None:
        for f in fmts:
            parsed = df["Index Date"].str.strip_chars().str.to_date(f, strict=False).drop_nulls().unique()
            if parsed.len() == 1 and parsed[0] == session:
                fmt = f
                break
        else:
            raise ValueError(f"index close file for {session} is not dated {session} in any known order")
    out = df.select(
        pl.col("Index Date").str.strip_chars().str.to_date(fmt, strict=False).alias("d"),
        pl.col("Index Name").str.strip_chars().alias("index_name"),
        num("Open Index Value").alias("open"),
        num("High Index Value").alias("high"),
        num("Low Index Value").alias("low"),
        num("Closing Index Value").alias("close"),
    ).filter(pl.col("d").is_not_null() & pl.col("close").is_not_null() & (pl.col("close") > 0))
    return out.with_columns(pl.lit(knowledge_time).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time")).select(INDEX_COLS)
