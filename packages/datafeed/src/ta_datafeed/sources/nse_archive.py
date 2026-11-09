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
from ..parse.udiff import bhavcopy_url, parse_udiff_bhavcopy

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
        blob = self.client.get_bytes(
            bhavcopy_url(d), subdir=f"nse_bhavcopy/{d:%Y/%m}", allow_404=True
        )
        if blob is None:
            return SessionResult(d, None, was_session=False)

        kt = datetime.combine(d, BHAVCOPY_PUBLISH_IST, tzinfo=IST).astimezone(UTC)
        bars = parse_udiff_bhavcopy(blob, knowledge_time=kt)
        return SessionResult(d, bars, was_session=True)

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
