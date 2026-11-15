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


# ------------------------------------------------------------------ legacy
#: NSE switched the cash bhavcopy to UDiFF in July 2024. Before that the same
#: session data was published as `cm{DD}{MON}{YYYY}bhav.csv.zip`. Only
#: sessions before this date are ever asked of the legacy file.
LEGACY_BEFORE = date(2024, 7, 8)

LEGACY_REQUIRED = ("SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE",
                   "TOTTRDQTY", "TOTTRDVAL", "TIMESTAMP")


def legacy_bhavcopy_url(d: date) -> str:
    """
    >>> from datetime import date
    >>> legacy_bhavcopy_url(date(2023, 6, 1))
    'https://nsearchives.nseindia.com/content/historical/EQUITIES/2023/JUN/cm01JUN2023bhav.csv.zip'
    """
    mon = d.strftime("%b").upper()
    return ("https://nsearchives.nseindia.com/content/historical/EQUITIES/"
            f"{d:%Y}/{mon}/cm{d:%d}{mon}{d:%Y}bhav.csv.zip")


def parse_legacy_bhavcopy(
    blob: bytes,
    *,
    knowledge_time: datetime,
    series: tuple[str, ...] = ("EQ", "BE"),
) -> pl.DataFrame:
    """Parse a pre-UDiFF bhavcopy into exactly the `md.bars_1d` shape the UDiFF parser emits.

    Same series filter, same columns, same knowledge_time semantics, so every
    downstream factor treats a 2021 bar and a 2026 bar identically. Delivery
    quantity is not in this file (it was a separate MTO file), so it is 0 here,
    exactly as the UDiFF parser leaves it. `source` says which file a bar came
    from, so a format-boundary artefact is traceable.

    >>> import io, zipfile
    >>> from datetime import UTC
    >>> csv = (b"SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,"
    ...        b"TIMESTAMP,TOTALTRADES,ISIN,\\n"
    ...        b"ABC,EQ,10,11,9,10.5,10.4,10,1000,10500,01-JUN-2023,12,INE000A01011,\\n"
    ...        b"ABCTB,TB,1,1,1,1,1,1,1,1,01-JUN-2023,1,IN0,\\n")
    >>> buf = io.BytesIO(); z = zipfile.ZipFile(buf, "w"); z.writestr("x.csv", csv); z.close()
    >>> df = parse_legacy_bhavcopy(buf.getvalue(), knowledge_time=datetime(2023, 6, 1, 13, tzinfo=UTC))
    >>> df.select("d", "symbol", "close", "prev_close", "n_trades", "source").rows()
    [(datetime.date(2023, 6, 1), 'ABC', 10.5, 10.0, 12, 'nse_legacy')]
    >>> csv2 = csv.replace(b"01-JUN-2023", b"13-Jul-20")      # 2-digit year, from 2020-07-13
    >>> buf = io.BytesIO(); z = zipfile.ZipFile(buf, "w"); z.writestr("x.csv", csv2); z.close()
    >>> parse_legacy_bhavcopy(buf.getvalue(), knowledge_time=datetime(2020, 7, 13, tzinfo=UTC))["d"].to_list()
    [datetime.date(2020, 7, 13)]
    """
    csv = _unzip(blob)
    df = pl.read_csv(io.BytesIO(csv), infer_schema_length=2000, ignore_errors=False)
    df = df.rename({c: c.strip() for c in df.columns})
    missing = [c for c in LEGACY_REQUIRED if c not in df.columns]
    if missing:
        raise BhavcopyFormatError(
            f"legacy bhavcopy missing required column(s) {missing}; got {df.columns[:12]}..."
        )
    out = (
        df.filter(pl.col("SERIES").str.strip_chars().is_in(list(series)))
        .select(
            # NSE wrote 4-digit years ("01-JUN-2023") but switched to 2-digit
            # ("13-Jul-20") from 2020-07-13; "%Y" reads "20" as year 20 AD.
            pl.when(pl.col("TIMESTAMP").str.strip_chars().str.len_chars() == 11)
            .then(pl.col("TIMESTAMP").str.strip_chars().str.to_titlecase()
                  .str.strptime(pl.Date, "%d-%b-%Y", strict=False))
            .otherwise(pl.col("TIMESTAMP").str.strip_chars().str.to_titlecase()
                       .str.strptime(pl.Date, "%d-%b-%y", strict=False))
            .alias("d"),
            pl.col("SYMBOL").cast(pl.Utf8).str.strip_chars().alias("symbol"),
            (pl.col("ISIN").cast(pl.Utf8) if "ISIN" in df.columns else pl.lit("")).alias("isin"),
            pl.col("SERIES").cast(pl.Utf8).str.strip_chars().alias("series"),
            pl.col("OPEN").cast(pl.Float64).alias("open"),
            pl.col("HIGH").cast(pl.Float64).alias("high"),
            pl.col("LOW").cast(pl.Float64).alias("low"),
            pl.col("CLOSE").cast(pl.Float64).alias("close"),
            pl.col("PREVCLOSE").cast(pl.Float64).alias("prev_close"),
            (pl.col("LAST").cast(pl.Float64) if "LAST" in df.columns
             else pl.col("CLOSE").cast(pl.Float64)).alias("last_price"),
            pl.col("TOTTRDQTY").cast(pl.Float64).cast(pl.UInt64).alias("volume"),
            pl.col("TOTTRDVAL").cast(pl.Float64).alias("turnover"),
            (pl.col("TOTALTRADES").cast(pl.Float64).cast(pl.UInt32) if "TOTALTRADES" in df.columns
             else pl.lit(0).cast(pl.UInt32)).alias("n_trades"),
        )
        .filter(pl.col("d").is_not_null() & (pl.col("close") > 0))
    )
    if out.height == 0:
        raise BhavcopyFormatError("legacy bhavcopy parsed to zero usable rows")
    return out.with_columns(
        pl.lit(0).cast(pl.UInt64).alias("deliv_qty"),
        pl.lit(0.0).cast(pl.Float32).alias("deliv_pct"),
        pl.lit(knowledge_time).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time"),
        pl.lit("nse_legacy").alias("source"),
    )
