"""BSE published-archive source.

Milestone M2.11: Secondary / failover market data source.
Mirrors NSEArchiveSource: fetches published UDiFF BhavCopy CSVs directly without
session warming, writes raw bytes into data/raw/YYYY/MM/DD/bse_bhavcopy/ before
parsing, and parses with series ('EQ', 'BE', 'A', 'B', 'T').
Stamps source = 'bse_udiff'.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import polars as pl
from ta_core.timefmt import IST

from ..http import PoliteClient
from ..parse.udiff import parse_udiff_bhavcopy
from .nse_archive import BHAVCOPY_PUBLISH_IST, SessionResult

log = logging.getLogger(__name__)

__all__ = ["BSEArchiveSource", "bse_bhavcopy_url"]


def bse_bhavcopy_url(d: date) -> str:
    """BSE UDiFF bhavcopy download URL for date `d`.

    >>> from datetime import date
    >>> bse_bhavcopy_url(date(2026, 10, 29))
    'https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_20261029_F_0000.CSV'
    """
    return (
        "https://www.bseindia.com/download/BhavCopy/Equity/"
        f"BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"
    )


class BSEArchiveSource:
    """BSE published bhavcopy fetcher."""

    def __init__(self, client: PoliteClient) -> None:
        self.client = client

    def fetch_session(self, d: date) -> SessionResult:
        """Fetch one session's BSE bars.

        Writes raw bytes to data/raw/YYYY/MM/DD/bse_bhavcopy/ before parsing.
        A 404 indicates BSE did not publish for this date (non-trading day).
        """
        blob = self.client.get_bytes(
            bse_bhavcopy_url(d),
            subdir=f"bse_bhavcopy/{d:%Y/%m/%d}",
            allow_404=True,
        )
        if blob is None:
            return SessionResult(d, None, was_session=False)

        kt = datetime.combine(d, BHAVCOPY_PUBLISH_IST, tzinfo=IST).astimezone(UTC)
        bars = parse_udiff_bhavcopy(blob, knowledge_time=kt, series=("EQ", "BE", "A", "B", "T"))
        bars = bars.with_columns(pl.lit("bse_udiff").alias("source"))
        return SessionResult(d, bars, was_session=True)

    def fetch_range(self, start: date, end: date) -> list[SessionResult]:
        """Fetch all weekday sessions in [start, end]."""
        out: list[SessionResult] = []
        d = start
        while d <= end:
            if d.weekday() < 5:  # skip weekends
                try:
                    out.append(self.fetch_session(d))
                except Exception as e:  # noqa: BLE001
                    log.warning("bse bhavcopy %s failed: %s", d, e)
                    out.append(SessionResult(d, None, was_session=False))
            d += timedelta(days=1)
        return out
