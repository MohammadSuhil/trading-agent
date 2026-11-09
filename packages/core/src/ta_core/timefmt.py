"""The ONE date normaliser.

Every feed this system reads dates differently, and several omit the timezone
entirely. `feedparser` silently leaves `published_parsed=None` on some of these
formats, and the obvious fallback -- defaulting to now() -- destroys both the
novelty signal and the staleness check, because a five-year-old recycled story
then looks brand new.

Observed formats in the wild (all normalised to tz-aware UTC):

  1. 'Sat, 31 Oct 2026 16:57:35 +0530'   ET / Business Standard / CNBC  (RFC822 +tz)
  2. the same, wrapped in CDATA           Mint / BusinessLine
  3. '31-Oct-2026 17:49:33'               NSE RSS            -- NO TIMEZONE
  4. '02-May-2026 14:20'                  NSE Insider        -- NO SECONDS
  5. '30 Oct, 2026'                       SEBI               -- NO TIME AT ALL
  6. 'Fri, 30 Oct 2026 18:40:00'          RBI  -- RFC822 SHAPE BUT NO TZ.
     This one is the trap: a naive RFC822 parser assumes UTC and silently
     shifts every RBI event by 5h30m.
  7. RFC822 in GMT                        Google News

Anything tz-naive is interpreted as IST, because every one of these publishers
is Indian. That assumption is explicit here rather than implicit in a parser.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

__all__ = ["IST", "UnparseableDate", "parse_feed_datetime", "to_ist"]

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

_CDATA = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.DOTALL)
_WS = re.compile(r"\s+")

# Tried in order. Ones WITHOUT %z are assumed IST.
_FORMATS: tuple[tuple[str, bool], ...] = (
    ("%a, %d %b %Y %H:%M:%S %z", True),   # 1,7  RFC822 with offset
    ("%a, %d %b %Y %H:%M:%S", False),     # 6    RBI: RFC822 shape, no tz
    ("%a, %d %b %Y %H:%M %z", True),
    ("%d-%b-%Y %H:%M:%S", False),         # 3    NSE RSS
    ("%d-%b-%Y %H:%M", False),            # 4    NSE insider, no seconds
    ("%d-%b-%Y", False),
    ("%d %b, %Y", False),                 # 5    SEBI, date only
    ("%d %b %Y", False),
    ("%Y-%m-%dT%H:%M:%S%z", True),        # ISO with offset
    ("%Y-%m-%dT%H:%M:%S", False),
    ("%Y-%m-%d %H:%M:%S", False),
    ("%Y-%m-%d", False),
)


class UnparseableDate(ValueError):
    """Raised instead of silently defaulting to now().

    Defaulting to now() is the bug this whole module exists to prevent: it
    makes stale news look fresh and poisons every novelty/decay feature.
    """


def _strip(raw: str) -> str:
    m = _CDATA.match(raw)
    if m:
        raw = m.group(1)
    raw = raw.replace("GMT", "+0000").replace("UTC", "+0000")
    return _WS.sub(" ", raw).strip()


def parse_feed_datetime(raw: str | None, *, assume: timezone = IST) -> datetime:
    """Normalise any observed feed date to a tz-aware UTC datetime.

    RFC822 with an explicit offset is honoured:

    >>> parse_feed_datetime('Sat, 31 Oct 2026 16:57:35 +0530').isoformat()
    '2026-10-31T11:27:35+00:00'

    CDATA wrappers are stripped:

    >>> parse_feed_datetime('<![CDATA[Sat, 31 Oct 2026 16:57:35 +0530]]>').isoformat()
    '2026-10-31T11:27:35+00:00'

    NSE's format carries no timezone, so it is read as IST, not UTC:

    >>> parse_feed_datetime('31-Oct-2026 17:49:33').isoformat()
    '2026-10-31T12:19:33+00:00'

    The RBI trap -- RFC822 *shape* but no offset. A naive parser would call
    this 18:40 UTC and shift the event 5h30m into the future:

    >>> parse_feed_datetime('Fri, 30 Oct 2026 18:40:00').isoformat()
    '2026-10-30T13:10:00+00:00'

    Date-only sources land at IST midnight:

    >>> parse_feed_datetime('30 Oct, 2026').isoformat()
    '2026-10-29T18:30:00+00:00'

    Missing or junk input raises rather than defaulting to now():

    >>> parse_feed_datetime(None)
    Traceback (most recent call last):
        ...
    ta_core.timefmt.UnparseableDate: empty date
    >>> parse_feed_datetime('last Tuesday')
    Traceback (most recent call last):
        ...
    ta_core.timefmt.UnparseableDate: unrecognised date format: 'last Tuesday'
    """
    if raw is None or not str(raw).strip():
        raise UnparseableDate("empty date")

    s = _strip(str(raw))
    for fmt, has_tz in _FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if not has_tz and dt.tzinfo is None:
            dt = dt.replace(tzinfo=assume)
        return dt.astimezone(UTC)

    raise UnparseableDate(f"unrecognised date format: {raw!r}")


def to_ist(dt: datetime) -> datetime:
    """Render a tz-aware datetime in IST for display.

    >>> from datetime import datetime, UTC
    >>> to_ist(datetime(2026, 10, 31, 11, 27, 35, tzinfo=UTC)).strftime('%d-%b-%Y %H:%M')
    '31-Oct-2026 16:57'
    """
    if dt.tzinfo is None:
        raise ValueError("refusing to convert a naive datetime; normalise it first")
    return dt.astimezone(IST)
