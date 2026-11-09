"""Parser for the NSE UDiFF bhavcopy -- the project's primary EOD source.

One file per session covers the entire cash market: ~2,700 EQ-series rows with
OHLC, volume, **turnover** and **trade count**. Those last two matter more than
they look: turnover is the liquidity screen a fund manager actually uses, and
getting it free in the same file means no second request.

URL shape (verified working with a plain GET, no cookies):
    https://nsearchives.nseindia.com/content/cm/
        BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip

Defensive by design: NSE changes column headers without notice, so the parser
maps by NAME, never by position, and fails loudly on a missing required column
rather than silently producing nulls that look like real zeros.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime

import polars as pl

__all__ = ["UDIFF_REQUIRED", "BhavcopyFormatError", "parse_udiff_bhavcopy"]

#: Columns we depend on. Missing any of these is a hard error.
UDIFF_REQUIRED = (
    "TradDt", "TckrSymb", "SctySrs", "OpnPric", "HghPric",
    "LwPric", "ClsPric", "PrvsClsgPric", "TtlTradgVol", "TtlTrfVal",
)


class BhavcopyFormatError(ValueError):
    """NSE changed the file shape. Fail loudly; do not guess."""


def _unzip(blob: bytes) -> bytes:
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise BhavcopyFormatError(f"no CSV inside zip: {z.namelist()}")
            return z.read(names[0])
    return blob


def parse_udiff_bhavcopy(
    blob: bytes,
    *,
    knowledge_time: datetime,
    series: tuple[str, ...] = ("EQ", "BE"),
) -> pl.DataFrame:
    """Parse one UDiFF bhavcopy into the `md.bars_1d` shape.

    `series` defaults to EQ plus BE (trade-to-trade). Everything else in the
    file -- SGBs, ETFs, debt, rights entitlements -- is excluded: they have
    different microstructure and would pollute cross-sectional factor ranks.

    `knowledge_time` is when this file became available to us, NOT the trade
    date. The bhavcopy for 2026-10-29 lands around 18:30 IST that evening, so
    a decision made at 15:00 that day could not have used it.
    """
    csv = _unzip(blob)
    df = pl.read_csv(io.BytesIO(csv), infer_schema_length=2000, ignore_errors=False)

    missing = [c for c in UDIFF_REQUIRED if c not in df.columns]
    if missing:
        raise BhavcopyFormatError(
            f"bhavcopy missing required column(s) {missing}; got {df.columns[:12]}..."
        )

    out = (
        df.filter(pl.col("SctySrs").is_in(list(series)))
        .select(
            pl.col("TradDt").str.strptime(pl.Date, "%Y-%m-%d", strict=False).alias("d"),
            pl.col("TckrSymb").cast(pl.Utf8).alias("symbol"),
            pl.col("ISIN").cast(pl.Utf8).alias("isin") if "ISIN" in df.columns
            else pl.lit("").alias("isin"),
            pl.col("SctySrs").cast(pl.Utf8).alias("series"),
            pl.col("OpnPric").cast(pl.Float64).alias("open"),
            pl.col("HghPric").cast(pl.Float64).alias("high"),
            pl.col("LwPric").cast(pl.Float64).alias("low"),
            pl.col("ClsPric").cast(pl.Float64).alias("close"),
            pl.col("PrvsClsgPric").cast(pl.Float64).alias("prev_close"),
            (pl.col("LastPric").cast(pl.Float64) if "LastPric" in df.columns
             else pl.col("ClsPric").cast(pl.Float64)).alias("last_price"),
            pl.col("TtlTradgVol").cast(pl.Float64).cast(pl.UInt64).alias("volume"),
            pl.col("TtlTrfVal").cast(pl.Float64).alias("turnover"),
            (pl.col("TtlNbOfTxsExctd").cast(pl.Float64).cast(pl.UInt32)
             if "TtlNbOfTxsExctd" in df.columns
             else pl.lit(0).cast(pl.UInt32)).alias("n_trades"),
        )
        .filter(pl.col("d").is_not_null() & (pl.col("close") > 0))
    )

    if out.height == 0:
        raise BhavcopyFormatError("bhavcopy parsed to zero usable rows")

    return out.with_columns(
        pl.lit(0).cast(pl.UInt64).alias("deliv_qty"),
        pl.lit(0.0).cast(pl.Float32).alias("deliv_pct"),
        pl.lit(knowledge_time).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time"),
        pl.lit("nse_udiff").alias("source"),
    )


def bhavcopy_url(d: date) -> str:
    """
    >>> from datetime import date
    >>> bhavcopy_url(date(2026, 10, 29))
    'https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20261029_F_0000.csv.zip'
    """
    return (
        "https://nsearchives.nseindia.com/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    )
