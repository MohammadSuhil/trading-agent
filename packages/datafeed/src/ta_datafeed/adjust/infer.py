"""Infer corporate actions (splits, bonuses) from UDiFF Bhavcopy bars.

NSE adjusts `prev_close` (`PrvsClsgPric`) on the ex-date of a corporate action,
while the prior session's `close` remains unadjusted. The overnight ratio
`prior_close / prev_close` reveals the corporate action ratio without relying
on external third-party feeds.
"""
from __future__ import annotations

import polars as pl

__all__ = ["CANDIDATE_RATIOS", "infer_corporate_actions"]

#: Standard corporate action candidate pairs (ratio_from, ratio_to).
#: target_ratio = ratio_to / ratio_from.
CANDIDATE_RATIOS: tuple[tuple[int, int], ...] = (
    (1, 2),   # 2.0: 1:2 split or 1:1 bonus
    (1, 3),   # 3.0: 1:3 split
    (1, 4),   # 4.0: 1:4 split
    (1, 5),   # 5.0: 1:5 split
    (1, 10),  # 10.0: 1:10 split
    (2, 3),   # 1.5: 1:2 bonus (3:2 total)
    (2, 5),   # 2.5: 3:2 bonus (5:2 total)
    (3, 4),   # 1.333333: 1:3 bonus (4:3 total)
    (3, 5),   # 1.666667: 2:3 bonus (5:3 total)
    (4, 5),   # 1.25: 1:4 bonus (5:4 total)
    (5, 6),   # 1.2: 1:5 bonus (6:5 total)
    (1, 6),   # 6.0: 1:6 split
    (2, 1),   # 0.5: 2:1 reverse / consolidation
    (5, 1),   # 0.2: 5:1 reverse
    (10, 1),  # 0.1: 10:1 reverse
)

_EMPTY_SCHEMA = {
    "symbol": pl.String,
    "ex_date": pl.Date,
    "action_type": pl.String,
    "ratio_from": pl.Float64,
    "ratio_to": pl.Float64,
    "dividend": pl.Float64,
    "adj_factor": pl.Float64,
    "announce_date": pl.Date,
    "knowledge_time": pl.Datetime("ms", "UTC"),
}


def infer_corporate_actions(bars: pl.DataFrame) -> pl.DataFrame:
    """Infer splits and bonuses from overnight jumps between prior close and prev_close.

    Supports two cases:
    - Case 1: NSE adjusts prev_close on ex-date (existing behavior, ~1.5% tolerance).
    - Case 2: NSE leaves prev_close unadjusted, reveals via open/close gap.

    >>> import polars as pl
    >>> from datetime import date, datetime, UTC
    >>> b = pl.DataFrame({
    ...     "symbol": ["INFY", "INFY"],
    ...     "d": [date(2026, 6, 1), date(2026, 6, 2)],
    ...     "close": [1000.0, 510.0],
    ...     "prev_close": [995.0, 500.0],
    ...     "knowledge_time": [datetime(2026, 6, 1, 18, 0, tzinfo=UTC),
    ...                       datetime(2026, 6, 2, 18, 0, tzinfo=UTC)],
    ... })
    >>> ca = infer_corporate_actions(b)
    >>> ca.select(["symbol", "ex_date", "action_type", "ratio_from", "ratio_to", "adj_factor"]).to_dicts()
    [{'symbol': 'INFY', 'ex_date': datetime.date(2026, 6, 2), 'action_type': 'SPLIT', 'ratio_from': 1.0, 'ratio_to': 2.0, 'adj_factor': 0.5}]
    """
    if bars.is_empty():
        return pl.DataFrame(schema=_EMPTY_SCHEMA)

    s = pl.col("symbol")
    df = (
        bars.sort(["symbol", "d"])
        .with_columns(
            pl.col("close").shift(1).over(s).alias("_prior_close"),
        )
    )

    # Compute _adv20 BEFORE filtering, so rolling window is exact per symbol
    has_turnover = "turnover" in df.columns
    if has_turnover:
        df = df.with_columns(
            pl.col("turnover").rolling_mean(20, min_samples=1).over(s).shift(1).over(s).alias("_adv20")
        )
    else:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("_adv20"))

    df = df.filter(
        pl.col("_prior_close").is_not_null()
        & (pl.col("_prior_close") > 0)
        & pl.col("prev_close").is_not_null()
        & (pl.col("prev_close") > 0)
    )

    if df.is_empty():
        return pl.DataFrame(schema=_EMPTY_SCHEMA)

    # Compute derived columns for case 2
    if "open" in df.columns and "close" in df.columns:
        df = df.with_columns(
            pl.when(pl.col("open") > 0).then(pl.col("_prior_close") / pl.col("open")).otherwise(None).alias("_open_ratio"),
            pl.when(pl.col("open") > 0).then((pl.col("close") / pl.col("open") - 1.0).abs()).otherwise(None).alias("_intra"),
        )
    else:
        df = df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("_open_ratio"),
            pl.lit(None, dtype=pl.Float64).alias("_intra"),
        )

    # Compute _t_mult for turnover multiplier
    if has_turnover:
        df = df.with_columns(
            pl.when(pl.col("_adv20").is_not_null() & (pl.col("_adv20") > 0))
            .then(pl.col("turnover") / pl.col("_adv20"))
            .otherwise(pl.lit(1.0))
            .alias("_t_mult")
        )
    else:
        df = df.with_columns(pl.lit(1.0, dtype=pl.Float64).alias("_t_mult"))

    df = df.with_columns(
        (pl.col("_prior_close") / pl.col("prev_close")).alias("_raw_ratio")
    )

    # Match candidate ratios: Case 1 (adjusted prev_close) or Case 2 (unadjusted)
    expr_rf = pl.lit(None, dtype=pl.Float64)
    expr_rt = pl.lit(None, dtype=pl.Float64)
    expr_case = pl.lit(None, dtype=pl.Int32)

    for rf, rt in CANDIDATE_RATIOS:
        target = float(rt) / float(rf)

        # Case 1: adjusted prev_close (existing behavior, ~1.5% tolerance)
        c1 = ((pl.col("_raw_ratio") - target).abs() / target) <= 0.015

        # Case 2: unadjusted prev_close (only for certain ratio ranges)
        if target >= 1.9 or target <= 0.55:
            # Large splits (1:2, 1:3, ..., 1:10) or reverses (2:1, 5:1, 10:1)
            c2 = (
                ((pl.col("_raw_ratio") - 1.0).abs() <= 0.02)
                & (((pl.col("_open_ratio") - target).abs() / target) <= 0.045)
                & (pl.col("_intra") <= 0.12)
            )
        elif abs(target - 1.5) < 1e-6:
            # (2, 3) 1:2 bonus with tighter thresholds and turnover guard
            c2 = (
                ((pl.col("_raw_ratio") - 1.0).abs() <= 0.02)
                & (((pl.col("_open_ratio") - target).abs() / target) <= 0.025)
                & (pl.col("_intra") <= 0.08)
                & (pl.col("_t_mult") <= 4.0)
            )
        else:
            # Smaller ratios (1.2, 1.25, 1.333, 1.667): only c1 to avoid false positives
            c2 = pl.lit(False)

        # c2 only triggers if c1 didn't match (c1 has priority)
        matched_c1 = c1
        matched_c2 = (~c1) & c2
        matched = matched_c1 | matched_c2

        expr_rf = pl.when(matched).then(float(rf)).otherwise(expr_rf)
        expr_rt = pl.when(matched).then(float(rt)).otherwise(expr_rt)
        # Track which case matched: 1 for case1, 2 for case2
        expr_case = pl.when(matched_c1).then(pl.lit(1)).otherwise(
            pl.when(matched_c2).then(pl.lit(2)).otherwise(expr_case)
        )

    df = df.with_columns(
        expr_rf.alias("_rf"),
        expr_rt.alias("_rt"),
        expr_case.alias("_case"),
    ).filter(pl.col("_rf").is_not_null())

    if df.is_empty():
        return pl.DataFrame(schema=_EMPTY_SCHEMA)

    # adj_factor: use _rf/_rt for c2 (unadjusted), prev_close/_prior_close for c1 (adjusted)
    adj_factor_expr = pl.when(
        pl.col("_case") == 2
    ).then(
        pl.col("_rf") / pl.col("_rt")
    ).otherwise(
        pl.col("prev_close") / pl.col("_prior_close")
    )

    target_ratio = pl.col("_rt") / pl.col("_rf")
    is_int_split = (
        ((target_ratio - target_ratio.round(0)).abs() < 1e-4)
        & (target_ratio.round(0) >= 2)
    )
    action_type_expr = (
        pl.when((adj_factor_expr < 1.0) & is_int_split)
        .then(pl.lit("SPLIT"))
        .otherwise(pl.lit("BONUS"))
    )

    if "knowledge_time" in df.columns:
        kt_expr = pl.col("knowledge_time").cast(pl.Datetime("ms", "UTC"))
    else:
        kt_expr = pl.col("d").cast(pl.Datetime("ms", "UTC"))

    return df.select([
        pl.col("symbol").cast(pl.String),
        pl.col("d").cast(pl.Date).alias("ex_date"),
        action_type_expr.cast(pl.String).alias("action_type"),
        pl.col("_rf").cast(pl.Float64).alias("ratio_from"),
        pl.col("_rt").cast(pl.Float64).alias("ratio_to"),
        pl.lit(0.0, dtype=pl.Float64).alias("dividend"),
        adj_factor_expr.cast(pl.Float64).alias("adj_factor"),
        pl.col("d").cast(pl.Date).alias("announce_date"),
        kt_expr.alias("knowledge_time"),
    ]).sort(["symbol", "ex_date"])
