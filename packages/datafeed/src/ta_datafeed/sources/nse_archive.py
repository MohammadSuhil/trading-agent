"""NSE published-archive source.

Deliberately limited to files NSE *publishes* for download. It does not warm a
browser session or defeat bot protection to reach the site's internal JSON API:
`www.nseindia.com` returns 403 to non-browser clients, and working around that
is a materially different act from fetching a published CSV. NSE's Terms of Use
also prohibit systematic automated collection, so the posture here is the
narrow, defensible one.

Everything this source provides is therefore unauthenticated and stable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import polars as pl
from ta_core.timefmt import IST

from ..http import PoliteClient
from ..parse.constituents import constituents_url, parse_constituents
from ..parse.index_close import index_close_url, parse_index_close
from ..parse.udiff import (
    LEGACY_BEFORE,
    BhavcopyFormatError,
    bhavcopy_url,
    legacy_bhavcopy_url,
    parse_legacy_bhavcopy,
    parse_udiff_bhavcopy,
)

log = logging.getLogger(__name__)

#: NSE publishes the cash bhavcopy at roughly 18:30 IST on the trading day.
#: Using this as knowledge_time (rather than midnight) keeps same-day decisions
#: honest: a 15:00 decision cannot see a file that lands at 18:30.
BHAVCOPY_PUBLISH_IST = time(18, 30)


@dataclass(frozen=True)
class SessionResult:
    d: date
    bars: pl.DataFrame | None
    was_session: bool


def _same_session(bars: pl.DataFrame, d: date) -> pl.DataFrame:
    """Every row of the file for session `d` must be dated `d`.

    A date-format change (NSE moved the legacy file from "2020" to "20" mid-
    2020, which parsed as year 20 AD) or a mis-served file would otherwise land
    bars on the wrong session -- or crash the insert halfway through a backfill.
    """
    bad = bars.filter(pl.col("d") != d)
    if bad.height:
        raise BhavcopyFormatError(
            f"file for {d} contains {bad.height} row(s) dated {bad['d'].unique().to_list()[:3]}"
        )
    return bars


class NSEArchiveSource:
    def __init__(self, client: PoliteClient) -> None:
        self.client = client

    # ------------------------------------------------------------- bars
    def fetch_session(self, d: date) -> SessionResult:
        """Fetch one session's bars.

        A 404 means NSE published nothing for that date, i.e. it was not a
        trading session. That is an *answer*, not a failure -- it is in fact
        the most reliable trading-calendar source available, because it is the
        exchange's own output.
        """
        kt = datetime.combine(d, BHAVCOPY_PUBLISH_IST, tzinfo=IST).astimezone(UTC)
        if d < LEGACY_BEFORE:
            # Before July 2024 the session lives in the legacy file. Asking it
            # first saves a guaranteed-404 UDiFF probe per historical day; the
            # two formats were verified field-identical where both exist.
            legacy = self.client.get_bytes(
                legacy_bhavcopy_url(d), subdir=f"nse_bhavcopy_legacy/{d:%Y/%m}", allow_404=True
            )
            if legacy is not None:
                return SessionResult(d, _same_session(parse_legacy_bhavcopy(legacy, knowledge_time=kt), d),
                                     was_session=True)
        blob = self.client.get_bytes(
            bhavcopy_url(d), subdir=f"nse_bhavcopy/{d:%Y/%m}", allow_404=True
        )
        if blob is None:
            return SessionResult(d, None, was_session=False)
        bars = _same_session(parse_udiff_bhavcopy(blob, knowledge_time=kt), d)
        return SessionResult(d, bars, was_session=True)

    # ------------------------------------------------------------ indices
    def fetch_index_close(self, d: date) -> pl.DataFrame | None:
        """OHLC for every NSE index on session `d`; None if NSE published none.

        Stamped with the same 18:30 IST knowledge_time as the bhavcopy: the
        file is an end-of-day publication, so a same-day 15:00 decision must
        not see it.
        """
        blob = self.client.get_bytes(
            index_close_url(d), subdir=f"nse_index_close/{d:%Y/%m}", allow_404=True
        )
        if blob is None:
            return None
        kt = datetime.combine(d, BHAVCOPY_PUBLISH_IST, tzinfo=IST).astimezone(UTC)
        return parse_index_close(blob, knowledge_time=kt, session=d)

    def fetch_range(self, start: date, end: date) -> list[SessionResult]:
        out: list[SessionResult] = []
        d = start
        while d <= end:
            if d.weekday() < 5:          # skip obvious weekends, saves ~290 requests/yr
                try:
                    out.append(self.fetch_session(d))
                except Exception as e:   # noqa: BLE001 - one bad day must not kill a backfill
                    log.warning("bhavcopy %s failed: %s", d, e)
                    out.append(SessionResult(d, None, was_session=False))
            d += timedelta(days=1)
        return out

    # ------------------------------------------------------ constituents
    def fetch_constituents(self, index_name: str, snapshot_date: date) -> pl.DataFrame:
        blob = self.client.get_bytes(
            constituents_url(index_name),
            # Day-granular, because two snapshots in one month must not collide:
            # md.index_members is a series of DATED snapshots, and overwriting
            # one silently reintroduces the survivorship bias the table exists
            # to prevent.
            subdir=f"nse_constituents/{snapshot_date:%Y/%m/%d}",
            reuse=False,     # membership changes; always take the live list
            archive=True,    # ...but keep this snapshot forever
        )
        if blob is None:
            raise RuntimeError(f"no constituents returned for {index_name}")
        kt = datetime.now(UTC)
        return parse_constituents(blob, index_name, snapshot_date, kt)
