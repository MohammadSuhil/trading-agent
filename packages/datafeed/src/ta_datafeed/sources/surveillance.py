"""NSE/BSE Surveillance list ingest (ASM/GSM).

Stocks placed under Additional Surveillance Measure (ASM) or Graded Surveillance
Measure (GSM) have increased margin requirements (up to 100%), intraday circuit
caps (5% or 2%), and settlement restrictions. RISK-12 prohibits entering any
candidate under surveillance.

If surveillance data is unavailable or empty in the store, load_surveillance_symbols
returns DataQuality.PARTIAL instead of pretending surveillance was verified.
"""
from __future__ import annotations

import io
import logging
from datetime import UTC, date, datetime

import polars as pl
from ta_core.asof import AsOf
from ta_core.enums import DataQuality

from ..store import GuardedClient

log = logging.getLogger(__name__)

__all__ = ["load_surveillance_symbols", "parse_surveillance_csv"]


def parse_surveillance_csv(blob: bytes, d: date, knowledge_time: datetime) -> pl.DataFrame:
    """Parse an exchange surveillance CSV into the `md.surveillance` shape.

    Recognises common column headers ('symbol', 'Symbol', 'TckrSymb', 'Security Symbol')
    and extracts list_type (defaults to 'ASM') and stage (defaults to '').
    """
    try:
        raw_df = pl.read_csv(io.BytesIO(blob), infer_schema_length=500, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        try:
            text = blob.decode("utf-8", errors="replace")
            raw_df = pl.read_csv(io.StringIO(text), infer_schema_length=500, ignore_errors=True)
        except Exception as inner_e:  # noqa: BLE001
            raise ValueError(f"Failed to parse surveillance CSV: {inner_e}") from e

    cols_lower = {c.strip().lower(): c for c in raw_df.columns}

    # Find symbol column
    sym_col = None
    for candidate in ("symbol", "tckrsymb", "security symbol", "scrip symbol", "security_symbol", "security"):
        if candidate in cols_lower:
            sym_col = cols_lower[candidate]
            break

    if sym_col is None:
        raise ValueError(f"No symbol column found in surveillance CSV; columns: {raw_df.columns}")

    # Find list_type column (optional, default 'ASM')
    lt_col = None
    for candidate in ("list_type", "surveillance_type", "type", "measure", "category"):
        if candidate in cols_lower:
            lt_col = cols_lower[candidate]
            break

    # Find stage column (optional, default '')
    st_col = None
    for candidate in ("stage", "stage_no", "stage_number", "level"):
        if candidate in cols_lower:
            st_col = cols_lower[candidate]
            break

    kt_utc = (
        knowledge_time.astimezone(UTC)
        if knowledge_time.tzinfo
        else knowledge_time.replace(tzinfo=UTC)
    )

    builder = raw_df.select(
        pl.lit(d).cast(pl.Date).alias("d"),
        pl.col(sym_col).cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("symbol"),
        (
            pl.col(lt_col).cast(pl.Utf8).str.strip_chars()
            if lt_col is not None
            else pl.lit("ASM").cast(pl.Utf8)
        ).alias("list_type"),
        (
            pl.col(st_col).cast(pl.Utf8).str.strip_chars()
            if st_col is not None
            else pl.lit("").cast(pl.Utf8)
        ).alias("stage"),
        pl.lit(kt_utc).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time"),
    ).filter(
        pl.col("symbol").is_not_null() & (pl.col("symbol") != "")
    )

    return builder


def load_surveillance_symbols(
    ch: GuardedClient, d: date, as_of: AsOf
) -> tuple[set[str], DataQuality]:
    """Load symbols currently under surveillance as of `as_of` on or before `d`.

    If md.surveillance has zero rows at `as_of`, returns (set(), DataQuality.PARTIAL)
    so the decision engine knows surveillance was unverified rather than assuming all
    stocks are clean.
    """
    try:
        df = ch.query_pit(
            "md.surveillance",
            as_of,
            columns="symbol, list_type",
            where=f"d <= '{d}'",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("md.surveillance read failed: %s; degrading to PARTIAL", e)
        return set(), DataQuality.PARTIAL

    if df.height == 0:
        return set(), DataQuality.PARTIAL

    symbols = set(df["symbol"].to_list())
    return symbols, DataQuality.FULL
