"""ClickHouse access.

Every read of a point-in-time table goes through `GuardedClient.query_pit`,
which refuses an unbounded read. The guard is installed here, at the only door
into the database, rather than left to each caller to remember.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Self

import clickhouse_connect
import polars as pl
from ta_core.asof import AsOf, assert_pit_bounded, build_pit_query

log = logging.getLogger(__name__)

CH_HOST = os.environ.get("TA_CH_HOST", "127.0.0.1")
CH_PORT = int(os.environ.get("TA_CH_PORT", "8123"))


class GuardedClient:
    """ClickHouse client that cannot read point-in-time data unbounded."""

    def __init__(self, host: str = CH_HOST, port: int = CH_PORT, database: str = "default"):
        self._c = clickhouse_connect.get_client(
            host=host, port=port, database=database,
            settings={"max_execution_time": 300},
        )

    # ---------------------------------------------------------- writes
    def insert_df(self, table: str, df: pl.DataFrame) -> int:
        """Bulk insert a polars frame. Returns rows written."""
        if df.height == 0:
            return 0
        self._c.insert_arrow(table, df.to_arrow())
        return df.height

    def command(self, sql: str) -> Any:
        return self._c.command(sql)

    # ---------------------------------------------------------- reads
    def query_df(self, sql: str, as_of: AsOf | None = None) -> pl.DataFrame:
        """Hand-written read. Still guarded: a PIT table referenced here must
        carry a bound, and if `as_of` is supplied the bound must equal it."""
        assert_pit_bounded(sql, as_of)
        arrow = self._c.query_arrow(sql)
        return pl.from_arrow(arrow)

    def query_pit(
        self, table: str, as_of: AsOf, *, columns: str = "*",
        where: str | None = None, order_by: str | None = None, limit: int | None = None,
    ) -> pl.DataFrame:
        """The only correct way to read history.

        `as_of` is passed through to the guard, so the bound is checked for
        EQUALITY here, not mere presence -- this path builds the query itself,
        so there is no excuse for the bound to be anything else.
        """
        return self.query_df(
            build_pit_query(table, as_of, columns=columns, where=where,
                            order_by=order_by, limit=limit),
            as_of,
        )

    def query_admin(self, sql: str) -> pl.DataFrame:
        """UNGUARDED read, for operational questions only.

        "How many rows are in md.bars_1d?" is not a point-in-time read: it is
        a question about the store, not about what was knowable on a date. The
        guard rightly refuses it, and without a named escape hatch the next
        person simply reaches past the guard with a raw client -- which is how
        a *decision* read ends up unbounded.

        So this exists, is named to be obvious in review, and is greppable:
        a CI test asserts it appears nowhere under packages/quant, packages/llm
        or apps/api. Operator tooling and metadata only.
        """
        return pl.from_arrow(self._c.query_arrow(sql))

    def scalar(self, sql: str) -> Any:
        assert_pit_bounded(sql)
        return self._c.command(sql)

    def close(self) -> None:
        self._c.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
