"""Parser for niftyindices.com constituent CSVs.

These carry the official `Industry` column, which is the heat map's grouping.
Using the exchange's own taxonomy rather than a third party's means the sector
labels match what every Indian market participant sees.

Schema: `Company Name, Industry, Symbol, Series, ISIN Code`
"""
from __future__ import annotations

import io
from datetime import date, datetime

import polars as pl

__all__ = ["INDEX_FILES", "constituents_url", "parse_constituents"]

#: index_name -> the file slug niftyindices.com serves
INDEX_FILES: dict[str, str] = {
    "NIFTY50": "ind_nifty50list",
    "NIFTY100": "ind_nifty100list",
    "NIFTY200": "ind_nifty200list",
    "NIFTY500": "ind_nifty500list",
    "NIFTYBANK": "ind_niftybanklist",
    "NIFTYIT": "ind_niftyitlist",
    "NIFTYPHARMA": "ind_niftypharmalist",
    "NIFTYAUTO": "ind_niftyautolist",
    "NIFTYFMCG": "ind_niftyfmcglist",
    "NIFTYMETAL": "ind_niftymetallist",
}


def constituents_url(index_name: str) -> str:
    """
    >>> constituents_url('NIFTY200')
    'https://niftyindices.com/IndexConstituent/ind_nifty200list.csv'
    """
    slug = INDEX_FILES.get(index_name.upper())
    if slug is None:
        raise KeyError(f"unknown index {index_name!r}; known: {sorted(INDEX_FILES)}")
    return f"https://niftyindices.com/IndexConstituent/{slug}.csv"


def parse_constituents(
    blob: bytes, index_name: str, snapshot_date: date, knowledge_time: datetime
) -> pl.DataFrame:
    """Parse into the `md.index_members` shape.

    Stored as a DATED SNAPSHOT. Membership churns constantly; applying today's
    list to a past date is survivorship bias and materially inflates backtest
    returns.
    """
    df = pl.read_csv(io.BytesIO(blob), infer_schema_length=1000)
    cols = {c.strip().lower(): c for c in df.columns}

    def pick(*names: str) -> str:
        for n in names:
            if n in cols:
                return cols[n]
        raise ValueError(f"constituent CSV missing any of {names}; got {df.columns}")

    return df.select(
        pl.lit(snapshot_date).alias("snapshot_date"),
        pl.lit(index_name.upper()).alias("index_name"),
        pl.col(pick("symbol")).cast(pl.Utf8).str.strip_chars().alias("symbol"),
        pl.col(pick("isin code", "isin")).cast(pl.Utf8).str.strip_chars().alias("isin"),
        pl.col(pick("company name", "company")).cast(pl.Utf8).alias("company_name"),
        pl.col(pick("industry")).cast(pl.Utf8).str.strip_chars().alias("industry"),
        pl.lit(knowledge_time).cast(pl.Datetime("ms", "UTC")).alias("knowledge_time"),
    ).filter(pl.col("symbol").is_not_null() & (pl.col("symbol") != ""))
