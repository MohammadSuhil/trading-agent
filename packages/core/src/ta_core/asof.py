"""Point-in-time discipline. The single most important module here.

The failure this prevents does not raise an error, does not look wrong, and
produces a BEAUTIFUL backtest. You find out it was fiction only when live
results diverge, months later, after you have trusted it with money.

Four distinct leakage vectors, none of which announce themselves:

  1. News timestamped by when the CRAWLER saw it rather than when the exchange
     disseminated it.
  2. TODAY'S index membership applied to past dates. NSE churns constituents
     constantly, so the survivors look like the whole universe and every
     backtest inherits a large survivorship bias.
  3. Unadjusted splits -- a 1:2 split reads as an instant -50% return, which a
     momentum factor happily trades on.
  4. A feature whose source row landed AFTER the decision it fed.

The defence is structural, not procedural: every point-in-time table carries a
`knowledge_time`, every read goes through `build_pit_query`, and a CI test
monkeypatches the database client to FAIL THE BUILD on any unbounded read.
Discipline cannot be left to whoever writes the next query at 19:40 on a
Tuesday.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "PIT_TABLES",
    "AsOf",
    "PITViolation",
    "assert_pit_bounded",
    "build_pit_query",
    "is_pit_table",
]

#: Tables whose rows become knowable only at a point in time. Reading any of
#: these without a `knowledge_time` bound is a build-failing error.
PIT_TABLES: frozenset[str] = frozenset({
    "md.bars_1d", "md.bars_1m", "md.features_1d", "md.index_members",
    "md.corporate_actions", "md.surveillance", "md.sector_daily",
    "md.trend_1d", "md.breadth_daily", "md.benchmark_1d",
    # A past recommendation must be served exactly as it was recorded. Putting
    # it behind the PIT guard is what stops an endpoint from "helpfully"
    # recomputing a probability with data that arrived after the decision.
    "ev.recommendations",
    "nx.news_features", "nx.filings", "nx.events",
    "fn.statements", "fn.ratios", "fn.shareholding", "fn.estimates",
    "ev.outcomes", "ev.attribution", "ev.posteriors",
})

#: Every `knowledge_time` bound in a query, as (qualifier, right-hand side).
#: The RHS is captured -- not just detected -- because a bound that *exists* is
#: not a bound that *holds*: `knowledge_time <= now()` satisfies every
#: existence check and still reads the future on replay.
_BOUND = re.compile(
    r"(?<![a-z0-9_.])((?:[a-z_][a-z0-9_.]*)\.)?knowledge_time\s*<=?\s*"
    r"('[^']*'"                        # literal timestamp -- what AsOf.sql() emits
    r"|%\([a-z_][a-z0-9_]*\)s"         # DB-API named parameter
    r"|\{[a-z_][a-z0-9_]*:[^}]+\}"     # ClickHouse native parameter binding
    r"|[a-z_][a-z0-9_]*\s*\([^)]*\)"   # a function call -- captured so we can REFUSE it
    r"|[^\s)]+)",                      # anything else -- also refused
    re.IGNORECASE,
)

#: The only right-hand sides that pin the read to a fixed instant. A literal is
#: what `build_pit_query` emits; a parameter placeholder is bound by the driver
#: and cannot be checked statically, so it is trusted. Everything else --
#: `now()`, `today()`, a column, a subquery -- is evaluated at execution time
#: and therefore moves with the wall clock.
_PINNED_RHS = re.compile(
    r"^('[^']*'|%\([a-z_][a-z0-9_]*\)s|\{[a-z_][a-z0-9_]*:[^}]+\})$",
    re.IGNORECASE,
)

_IDENT = re.compile(r"\b([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\b", re.IGNORECASE)
_ALIAS = re.compile(
    r"\b([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\s+(?:AS\s+)?([a-z_][a-z0-9_]*)\b",
    re.IGNORECASE,
)
_SQL_KEYWORDS = frozenset({
    "join", "inner", "left", "right", "full", "outer", "cross", "on", "where",
    "group", "order", "limit", "having", "union", "all", "any", "asof", "using",
    "settings", "format", "prewhere", "array", "global", "semi", "anti",
})


class PITViolation(AssertionError):
    """Raised when a query reads point-in-time data without an as-of bound."""


@dataclass(frozen=True, slots=True)
class AsOf:
    """The instant a decision is being made. Everything later is invisible.

    >>> from datetime import datetime, UTC
    >>> a = AsOf(datetime(2026, 10, 30, 14, 0, tzinfo=UTC))
    >>> a.sql()
    "'2026-10-30 14:00:00'"

    A naive datetime is refused: 'what did we know at 14:00' is meaningless
    without saying 14:00 *where*, and this system spans IST feeds and UTC storage.

    >>> AsOf(datetime(2026, 10, 30, 14, 0))
    Traceback (most recent call last):
        ...
    ValueError: as_of must be timezone-aware
    """

    ts: datetime

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")

    def sql(self) -> str:
        from datetime import UTC
        return "'" + self.ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") + "'"


def is_pit_table(name: str) -> bool:
    """
    >>> is_pit_table('md.bars_1d'), is_pit_table('system.parts')
    (True, False)
    """
    return name.lower() in PIT_TABLES


def build_pit_query(
    table: str,
    as_of: AsOf,
    *,
    columns: str = "*",
    where: str | None = None,
    order_by: str | None = None,
    limit: int | None = None,
) -> str:
    """Build a read that cannot see the future.

    The `knowledge_time` bound is injected by this function, never written by
    the caller -- a bound you have to remember is a bound you will forget.

    >>> from datetime import datetime, UTC
    >>> a = AsOf(datetime(2026, 10, 30, 14, 0, tzinfo=UTC))
    >>> print(build_pit_query('md.bars_1d', a, columns='sym, close',
    ...                       where="sym = 'RELIANCE'", order_by='d', limit=5))
    SELECT sym, close FROM md.bars_1d WHERE knowledge_time <= '2026-10-30 14:00:00' AND (sym = 'RELIANCE') ORDER BY d LIMIT 5

    Non-PIT tables are passed through unbounded, which is correct -- reference
    data has no knowledge_time:

    >>> print(build_pit_query('ref.symbols', a, columns='sym'))
    SELECT sym FROM ref.symbols

    >>> build_pit_query('md.bars_1d', datetime(2026, 1, 1))
    Traceback (most recent call last):
        ...
    TypeError: as_of must be an AsOf instance
    """
    if not isinstance(as_of, AsOf):
        raise TypeError("as_of must be an AsOf instance")

    sql = f"SELECT {columns} FROM {table}"
    clauses: list[str] = []
    if is_pit_table(table):
        clauses.append(f"knowledge_time <= {as_of.sql()}")
    if where:
        clauses.append(f"({where})")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += f" LIMIT {limit}"
    return sql


def _assert_bounds_hold(
    bounds: list[tuple[str | None, str]], as_of: AsOf | None
) -> None:
    """Refuse bounds that exist but do not pin the read to a fixed instant.

    Split out from `assert_pit_bounded` because it answers a different
    question: that function asks *is there a bound*, this one asks *does the
    bound hold*. The second question is the one a reviewer skips.

    >>> _assert_bounds_hold([(None, "'2026-10-30 14:00:00'")], None)
    >>> _assert_bounds_hold([('b.', 'now()')], None)
    Traceback (most recent call last):
        ...
    ta_core.asof.PITViolation: knowledge_time bound must be a literal as-of, not now()

    A driver-bound parameter cannot be inspected statically, so it is trusted:

    >>> _assert_bounds_hold([(None, '%(as_of)s')], None)
    """
    volatile = sorted({rhs for _, rhs in bounds if not _PINNED_RHS.match(rhs)})
    if volatile:
        raise PITViolation(
            "knowledge_time bound must be a literal as-of, not " + ", ".join(volatile)
        )

    # A literal bound can still be the wrong instant -- a constant copied from
    # another query reads the future and looks perfect. Only the caller knows
    # which instant was meant, so this runs only when `as_of` is supplied.
    if as_of is None:
        return
    want = as_of.sql()
    for _, rhs in bounds:
        if rhs.startswith("'") and rhs != want:
            raise PITViolation(f"knowledge_time bound {rhs} != as_of {want}")


def assert_pit_bounded(sql: str, as_of: AsOf | None = None) -> None:
    """Fail if `sql` reads a PIT table without a knowledge_time bound that holds.

    This is the guard the CI suite installs over the real database client, so
    that a hand-written query added later cannot quietly reintroduce look-ahead.

    Three distinct failures, in increasing order of subtlety. The first is the
    one everybody expects; the other two pass an existence check and still read
    the future, which is why the bound's right-hand side is inspected and not
    merely detected.

    >>> assert_pit_bounded("SELECT * FROM md.bars_1d "
    ...                    "WHERE knowledge_time <= '2026-10-30 14:00:00'")
    >>> assert_pit_bounded("SELECT * FROM ref.symbols")

    The whole point -- an unbounded read of a PIT table is refused:

    >>> assert_pit_bounded("SELECT * FROM md.bars_1d WHERE sym = 'RELIANCE'")
    Traceback (most recent call last):
        ...
    ta_core.asof.PITViolation: unbounded read of point-in-time table(s): md.bars_1d

    A join that bounds only one side is still a violation -- the classic way
    leakage survives review:

    >>> assert_pit_bounded(
    ...     "SELECT * FROM md.bars_1d b "
    ...     "JOIN nx.news_features n ON n.sym = b.sym "
    ...     "WHERE b.knowledge_time <= '2026-10-30 14:00:00'")
    Traceback (most recent call last):
        ...
    ta_core.asof.PITViolation: unbounded read of point-in-time table(s): nx.news_features

    Second: a bound that is evaluated at query time is not a bound at all.
    `now()` moves with the wall clock, so replaying 2026-10-15 today reads
    everything learned since. This is syntactically indistinguishable from a
    correct query and was the gap in the first version of this guard.

    >>> assert_pit_bounded("SELECT * FROM md.bars_1d WHERE knowledge_time <= now()")
    Traceback (most recent call last):
        ...
    ta_core.asof.PITViolation: knowledge_time bound must be a literal as-of, not now()

    Third -- and only checkable if you say what instant you meant: the bound is
    a literal, but the *wrong* literal. A constant copied from another query
    passes every check above.

    >>> from datetime import datetime, UTC
    >>> a = AsOf(datetime(2026, 10, 30, 14, 0, tzinfo=UTC))
    >>> assert_pit_bounded(build_pit_query('md.bars_1d', a), a)
    >>> assert_pit_bounded(
    ...     "SELECT * FROM md.bars_1d WHERE knowledge_time <= '2026-10-31 09:00:00'", a)
    Traceback (most recent call last):
        ...
    ta_core.asof.PITViolation: knowledge_time bound '2026-10-31 09:00:00' != as_of '2026-10-30 14:00:00'
    """
    referenced = {m.group(1).lower() for m in _IDENT.finditer(sql)}
    pit_hits = sorted(t for t in referenced if t in PIT_TABLES)
    if not pit_hits:
        return

    bounds = [(m.group(1), m.group(2)) for m in _BOUND.finditer(sql)]
    _assert_bounds_hold(bounds, as_of)

    # Map each PIT table to every name it can be referred to by: its full name,
    # its bare table name, and any alias bound to it in a FROM/JOIN clause.
    # Without this, "FROM md.bars_1d b ... WHERE b.knowledge_time <= X" looks
    # unbounded, and the guard cries wolf on correct queries.
    names: dict[str, set[str]] = {t: {t, t.split(".")[-1]} for t in pit_hits}
    for m in _ALIAS.finditer(sql):
        tbl, alias = m.group(1).lower(), m.group(2).lower()
        if tbl in names and alias not in _SQL_KEYWORDS:
            names[tbl].add(alias)

    # Which qualifiers actually carry a bound? Every bound still standing here
    # is pinned to a fixed instant -- volatile ones already raised above.
    bounded_prefixes = {p.rstrip(".").lower() for p, _ in bounds if p}
    has_bare_bound = any(p is None for p, _ in bounds)

    unbounded = []
    for t in pit_hits:
        if bounded_prefixes & names[t]:
            continue
        # A bare `knowledge_time <= X` is only unambiguous with ONE PIT table.
        if has_bare_bound and len(pit_hits) == 1:
            continue
        unbounded.append(t)

    if unbounded:
        raise PITViolation(
            "unbounded read of point-in-time table(s): " + ", ".join(unbounded)
        )
