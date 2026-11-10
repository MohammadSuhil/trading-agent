"""Apply PIT-bounded corporate action adjustments to historical bars.

Enforces Invariant P-05: Every number that crosses a split/bonus uses adjusted
prices, and the adjustment itself is PIT-bounded. If an analysis is run as of a
timestamp before the corporate action was known, the historical prices must
remain unadjusted.
"""
from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
from ta_core.asof import AsOf

__all__ = ["apply_adjustments"]


def apply_adjustments(
    bars: pl.DataFrame,
    actions: pl.DataFrame,
    *,
    as_of: AsOf | datetime | None = None,
) -> pl.DataFrame:
    """Adjust historical bars for stock splits and bonus issues.

    For each corporate action known at or before `as_of`, bars with `d < ex_date`
    have OHLC and prev_close multiplied by the cumulative adjustment factor, and
    volume divided by the same factor (leaving rupee turnover invariant).

    >>> import polars as pl
    >>> from datetime import date, datetime, UTC
    >>> bars = pl.DataFrame({
    ...     "symbol": ["INFY", "INFY"],
    ...     "d": [date(2026, 6, 1), date(2026, 6, 2)],
    ...     "open": [990.0, 505.0],
    ...     "high": [1010.0, 515.0],
    ...     "low": [980.0, 495.0],
    ...     "close": [1000.0, 510.0],
    ...     "prev_close": [995.0, 500.0],
    ...     "volume": [1000, 2000],
    ...     "turnover": [1000000.0, 1020000.0],
    ... })
    >>> actions = pl.DataFrame({
    ...     "symbol": ["INFY"],
    ...     "ex_date": [date(2026, 6, 2)],
    ...     "adj_factor": [0.5],
    ...     "knowledge_time": [datetime(2026, 6, 2, 18, 0, tzinfo=UTC)],
    ... })
    >>> adj = apply_adjustments(bars, actions, as_of=datetime(2026, 6, 2, 19, 0, tzinfo=UTC))
    >>> adj["close"].to_list()
    [500.0, 510.0]
    >>> # Before knowledge_time: unadjusted
    >>> unadj = apply_adjustments(bars, actions, as_of=datetime(2026, 6, 1, 19, 0, tzinfo=UTC))
    >>> unadj["close"].to_list()
    [1000.0, 510.0]
    """
    if bars.is_empty():
        return bars

    if actions.is_empty():
        return bars.sort(["symbol", "d"])

    # Determine cutoff timestamp
    cutoff_dt: datetime | None = None
    if isinstance(as_of, AsOf):
        cutoff_dt = as_of.ts
    elif isinstance(as_of, datetime):
        cutoff_dt = as_of

    if cutoff_dt is not None and cutoff_dt.tzinfo is None:
        cutoff_dt = cutoff_dt.replace(tzinfo=UTC)

    valid_actions = actions
    if cutoff_dt is not None and "knowledge_time" in valid_actions.columns:
        valid_actions = valid_actions.filter(
            pl.col("knowledge_time").cast(pl.Datetime("ms", "UTC")) <= cutoff_dt
        )

    max_d = bars["d"].max()
    if max_d is not None and "ex_date" in valid_actions.columns:
        valid_actions = valid_actions.filter(pl.col("ex_date") <= max_d)

    if valid_actions.is_empty():
        return bars.sort(["symbol", "d"])

    symbols_in_bars = bars["symbol"].unique().to_list()
    act = valid_actions.filter(pl.col("symbol").is_in(symbols_in_bars))
    if act.is_empty():
        return bars.sort(["symbol", "d"])

    # Join bars with actions where ex_date > d
    bar_keys = bars.select(["symbol", "d"])
    joined = bar_keys.join(
        act.select(["symbol", "ex_date", "adj_factor"]),
        on="symbol",
        how="inner",
    ).filter(pl.col("ex_date") > pl.col("d"))

    if joined.is_empty():
        return bars.sort(["symbol", "d"])

    cum_factors = (
        joined.group_by(["symbol", "d"])
        .agg(pl.col("adj_factor").product().alias("_cum_adj"))
    )

    adj_bars = bars.join(cum_factors, on=["symbol", "d"], how="left").with_columns(
        pl.col("_cum_adj").fill_null(1.0)
    )

    price_cols = [
        c for c in ("open", "high", "low", "close", "prev_close", "last_price")
        if c in bars.columns
    ]
    transforms = [
        (pl.col(c) * pl.col("_cum_adj")).cast(bars.schema[c]).alias(c)
        for c in price_cols
    ]
    if "volume" in bars.columns:
        vol_dtype = bars.schema["volume"]
        transforms.append(
            (pl.col("volume") / pl.col("_cum_adj")).round(0).cast(vol_dtype).alias("volume")
        )

    return (
        adj_bars.with_columns(transforms)
        .select(bars.columns)
        .sort(["symbol", "d"])
    )
