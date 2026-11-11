"""Benchmark (NIFTY 50 TRI) ingestion and synthetic panel reconstruction.

Milestone M2.10: Benchmark against NIFTY 50 Total Returns Index (TRI), NOT the
price index. Flattering the system with ~1.2%/yr of uncounted dividend yield
is a classic backtesting trap.

When published NIFTY 50 TRI series is available, it is ingested with
tri_source='PUBLISHED'. When unavailable, a turnover-weighted return series is
reconstructed across index constituents from md.bars_1d and compounded from 1000.0,
stamped with tri_source='RECONSTRUCTED'.
"""
from __future__ import annotations

import io
import logging
from datetime import UTC, datetime

import polars as pl

log = logging.getLogger(__name__)

__all__ = [
    "parse_benchmark_csv",
    "reconstruct_benchmark_from_panel",
]

BENCHMARK_SCHEMA = {
    "d": pl.Date,
    "index_id": pl.Utf8,
    "series_kind": pl.Utf8,
    "close": pl.Float64,
    "ret_1d": pl.Float64,
    "tri_source": pl.Utf8,
    "knowledge_time": pl.Datetime("ms", "UTC"),
}


def parse_benchmark_csv(
    blob: bytes,
    *,
    index_id: str = "NIFTY50",
    series_kind: str = "TRI",
    knowledge_time: datetime,
) -> pl.DataFrame:
    """Parse a benchmark CSV into the `md.benchmark_1d` shape.

    Handles published niftyindices CSV formats with flexible date and index value
    headers. Calculates daily return ret_1d from the close series.
    """
    try:
        df = pl.read_csv(io.BytesIO(blob), infer_schema_length=500, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        try:
            text = blob.decode("utf-8", errors="replace")
            df = pl.read_csv(io.StringIO(text), infer_schema_length=500, ignore_errors=True)
        except Exception as inner_e:  # noqa: BLE001
            raise ValueError(f"Failed to parse benchmark CSV: {inner_e}") from e

    cols_lower = {c.strip().lower(): c for c in df.columns}

    # Locate date column
    d_col = None
    for cand in ("date", "d", "traddt", "index date"):
        if cand in cols_lower:
            d_col = cols_lower[cand]
            break
    if d_col is None:
        raise ValueError(f"No date column found in benchmark CSV: {df.columns}")

    # Locate close/TRI column
    val_col = None
    for cand in (
        "total returns index",
        "totalreturnsindex",
        "tri",
        "close",
        "close price",
        "index value",
        "clspric",
    ):
        if cand in cols_lower:
            val_col = cols_lower[cand]
            break
    if val_col is None:
        raise ValueError(f"No close/TRI column found in benchmark CSV: {df.columns}")

    kt_utc = (
        knowledge_time.astimezone(UTC)
        if knowledge_time.tzinfo
        else knowledge_time.replace(tzinfo=UTC)
    )

    # Clean close column if string containing commas
    close_expr = pl.col(val_col)
    if df.schema[val_col] in (pl.Utf8, pl.String):
        close_expr = close_expr.str.replace_all(",", "")
    close_expr = close_expr.cast(pl.Float64)

    # Date parsing expression supporting multiple formats
    d_expr = pl.coalesce([
        pl.col(d_col).str.strptime(pl.Date, "%Y-%m-%d", strict=False),
        pl.col(d_col).str.strptime(pl.Date, "%d-%b-%Y", strict=False),
        pl.col(d_col).str.strptime(pl.Date, "%d-%m-%Y", strict=False),
        pl.col(d_col).str.strptime(pl.Date, "%Y/%m/%d", strict=False),
    ])

    parsed = (
        df.select(
            d_expr.alias("d"),
            close_expr.alias("close"),
        )
        .filter(pl.col("d").is_not_null() & pl.col("close").is_not_null() & (pl.col("close") > 0))
        .sort("d")
    )

    if parsed.height == 0:
        raise ValueError("Benchmark CSV parsed to zero usable rows")

    out = parsed.with_columns(
        (pl.col("close") / pl.col("close").shift(1) - 1.0).fill_null(0.0).alias("ret_1d"),
        pl.lit(index_id).cast(pl.Utf8).alias("index_id"),
        pl.lit(series_kind).cast(pl.Utf8).alias("series_kind"),
        pl.lit("PUBLISHED").cast(pl.Utf8).alias("tri_source"),
        pl.lit(kt_utc).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time"),
    ).select(list(BENCHMARK_SCHEMA.keys()))

    return out


def reconstruct_benchmark_from_panel(
    bars: pl.DataFrame,
    members: pl.DataFrame,
    *,
    index_id: str = "NIFTY50",
    knowledge_time: datetime | None = None,
) -> pl.DataFrame:
    """Reconstruct synthetic benchmark return series from constituent bars.

    Calculates turnover-weighted daily return across constituents and compounds
    from 1000.0 base. Stamped with tri_source='RECONSTRUCTED'.
    """
    if bars.is_empty():
        return pl.DataFrame(schema=BENCHMARK_SCHEMA)

    # Filter to index constituents if members are supplied
    if not members.is_empty() and "index_name" in members.columns and "symbol" in members.columns:
        matching_members = members.filter(pl.col("index_name") == index_id)
        if matching_members.height > 0:
            syms = set(matching_members["symbol"].unique().to_list())
            sub_bars = bars.filter(pl.col("symbol").is_in(list(syms)))
        else:
            sub_bars = bars
    else:
        sub_bars = bars

    if sub_bars.is_empty():
        sub_bars = bars

    # Ensure ret_1d is available
    if "ret_1d" not in sub_bars.columns:
        sub_bars = sub_bars.sort(["symbol", "d"]).with_columns(
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret_1d")
        )

    # Remove rows where return is undefined
    clean_bars = sub_bars.filter(pl.col("ret_1d").is_not_null())
    if clean_bars.is_empty():
        return pl.DataFrame(schema=BENCHMARK_SCHEMA)

    has_to = "turnover" in clean_bars.columns
    if has_to:
        daily_ret = (
            clean_bars.group_by("d")
            .agg(
                pl.when(pl.col("turnover").sum() > 0)
                .then((pl.col("ret_1d") * pl.col("turnover")).sum() / pl.col("turnover").sum())
                .otherwise(pl.col("ret_1d").mean())
                .alias("ret_1d")
            )
            .sort("d")
        )
    else:
        daily_ret = (
            clean_bars.group_by("d")
            .agg(pl.col("ret_1d").mean().alias("ret_1d"))
            .sort("d")
        )

    # Compound from base 1000.0
    daily_comp = daily_ret.with_columns(
        (1000.0 * (1.0 + pl.col("ret_1d")).cum_prod()).alias("close")
    )

    if knowledge_time is not None:
        kt_utc = (
            knowledge_time.astimezone(UTC)
            if knowledge_time.tzinfo
            else knowledge_time.replace(tzinfo=UTC)
        )
    elif "knowledge_time" in bars.columns and bars["knowledge_time"].max() is not None:
        raw_kt = bars["knowledge_time"].max()
        kt_utc = raw_kt.astimezone(UTC) if hasattr(raw_kt, "tzinfo") and raw_kt.tzinfo else datetime.now(UTC)
    else:
        kt_utc = datetime.now(UTC)

    out = daily_comp.select(
        pl.col("d").cast(pl.Date),
        pl.lit(index_id).cast(pl.Utf8).alias("index_id"),
        pl.lit("TRI").cast(pl.Utf8).alias("series_kind"),
        pl.col("close").cast(pl.Float64),
        pl.col("ret_1d").cast(pl.Float64),
        pl.lit("RECONSTRUCTED").cast(pl.Utf8).alias("tri_source"),
        pl.lit(kt_utc).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time"),
    )

    return out
