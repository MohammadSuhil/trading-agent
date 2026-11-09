"""NSE trading calendar.

Two design decisions worth stating, because both are load-bearing:

1. A session is a WEEKDAY, NOT A HOLIDAY, *or* an explicitly declared special
   session. That second clause exists because Muhurat trading falls on
   Diwali, which can land on a Saturday or Sunday. A calendar generated from
   a Mon-Fri weekmask -- which is how `pandas_market_calendars` works --
   structurally cannot represent it, so we do not use one.

2. For HISTORICAL dates the authoritative source is not a holiday list at all:
   it is whether NSE published a bhavcopy that day. The exchange's own output
   is ground truth and cannot drift from reality. `TradingCalendar.from_observed`
   builds a calendar from ingested session dates; the YAML holiday list is only
   needed to schedule FORWARD.

`ta_core` has zero I/O, so this module never reads the YAML itself -- the app
layer loads it and constructs the calendar.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .timefmt import IST

__all__ = ["MARKET_CLOSE", "MARKET_OPEN", "PREOPEN_START", "TradingCalendar"]

PREOPEN_START = time(9, 0)
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

_SAT, _SUN = 5, 6


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    """Immutable NSE session calendar.

    >>> cal = TradingCalendar(
    ...     holidays={date(2026, 11, 5)},
    ...     special_sessions={date(2026, 11, 8)},   # Muhurat, a Sunday
    ... )
    >>> cal.is_trading_day(date(2026, 11, 4))   # ordinary Wednesday
    True
    >>> cal.is_trading_day(date(2026, 11, 5))   # declared holiday
    False
    >>> cal.is_trading_day(date(2026, 11, 7))   # ordinary Saturday
    False
    >>> cal.is_trading_day(date(2026, 11, 8))   # Muhurat on a SUNDAY
    True

    Navigation skips non-sessions in both directions:

    >>> cal.next_session(date(2026, 11, 4))
    datetime.date(2026, 11, 6)
    >>> cal.prev_session(date(2026, 11, 6))
    datetime.date(2026, 11, 4)

    Session arithmetic is used everywhere the time stop is computed, so it
    must agree with the labeller exactly:

    >>> cal.shift_sessions(date(2026, 11, 4), 2)   # Muhurat counts as a session
    datetime.date(2026, 11, 8)
    >>> cal.sessions_between(date(2026, 11, 4), date(2026, 11, 10))
    [datetime.date(2026, 11, 6), datetime.date(2026, 11, 8), datetime.date(2026, 11, 9), datetime.date(2026, 11, 10)]
    """

    holidays: frozenset[date] = field(default_factory=frozenset)
    special_sessions: frozenset[date] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "holidays", frozenset(self.holidays))
        object.__setattr__(self, "special_sessions", frozenset(self.special_sessions))
        overlap = self.holidays & self.special_sessions
        if overlap:
            raise ValueError(f"date is both a holiday and a special session: {sorted(overlap)}")

    @classmethod
    def from_observed(cls, session_dates: Iterable[date]) -> TradingCalendar:
        """Build from dates NSE actually published a bhavcopy for.

        Ground truth for any historical range -- it cannot disagree with the
        exchange, because it *is* the exchange's output. Weekend sessions are
        recorded as special sessions automatically.

        >>> cal = TradingCalendar.from_observed(
        ...     [date(2026, 11, 4), date(2026, 11, 6), date(2026, 11, 8)])
        >>> sorted(cal.special_sessions)
        [datetime.date(2026, 11, 8)]
        >>> cal.is_trading_day(date(2026, 11, 5))
        False
        """
        observed = set(session_dates)
        if not observed:
            return cls()
        specials = {d for d in observed if d.weekday() in (_SAT, _SUN)}
        lo, hi = min(observed), max(observed)
        holidays = set()
        d = lo
        while d <= hi:
            if d.weekday() < _SAT and d not in observed:
                holidays.add(d)
            d += timedelta(days=1)
        return cls(holidays=frozenset(holidays), special_sessions=frozenset(specials))

    def is_trading_day(self, d: date) -> bool:
        if d in self.special_sessions:
            return True
        return d.weekday() < _SAT and d not in self.holidays

    def next_session(self, d: date) -> date:
        probe = d + timedelta(days=1)
        for _ in range(400):
            if self.is_trading_day(probe):
                return probe
            probe += timedelta(days=1)
        raise ValueError(f"no trading day within 400 days after {d}")

    def prev_session(self, d: date) -> date:
        probe = d - timedelta(days=1)
        for _ in range(400):
            if self.is_trading_day(probe):
                return probe
            probe -= timedelta(days=1)
        raise ValueError(f"no trading day within 400 days before {d}")

    def shift_sessions(self, d: date, n: int) -> date:
        """Move `n` sessions forward (n>0) or back (n<0) from `d`."""
        if n == 0:
            return d
        step = self.next_session if n > 0 else self.prev_session
        cur = d
        for _ in range(abs(n)):
            cur = step(cur)
        return cur

    def sessions_between(self, start: date, end: date) -> list[date]:
        """Sessions in (start, end] -- start EXCLUSIVE, end INCLUSIVE.

        Exclusive-start matches how a holding period is counted: the entry
        day is day 0, not day 1.
        """
        out: list[date] = []
        d = start + timedelta(days=1)
        while d <= end:
            if self.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def iter_sessions(self, start: date, end: date) -> Iterator[date]:
        d = start
        while d <= end:
            if self.is_trading_day(d):
                yield d
            d += timedelta(days=1)

    def session_open(self, d: date) -> datetime:
        """Tz-aware IST market open for a session.

        >>> TradingCalendar().session_open(date(2026, 11, 4)).isoformat()
        '2026-11-04T09:15:00+05:30'
        """
        if not self.is_trading_day(d):
            raise ValueError(f"{d} is not a trading session")
        return datetime.combine(d, MARKET_OPEN, tzinfo=IST)

    def session_close(self, d: date) -> datetime:
        if not self.is_trading_day(d):
            raise ValueError(f"{d} is not a trading session")
        return datetime.combine(d, MARKET_CLOSE, tzinfo=IST)
