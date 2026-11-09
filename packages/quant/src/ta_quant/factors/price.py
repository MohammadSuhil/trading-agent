"""Vectorised price/volume factors.

All computed with polars window functions over the whole panel at once --
~2,700 symbols x 500 sessions runs in well under a second, which matters
because this runs nightly and a per-symbol Python loop would take minutes.

Every factor here uses ONLY data at or before the bar it is attached to. The
`over("symbol")` partitioning plus strictly-trailing windows is what makes that
true; any factor needing a centred window would leak the future and is absent
on purpose.
"""
from __future__ import annotations

import polars as pl

__all__ = ["FACTOR_COLUMNS", "add_price_factors"]

FACTOR_COLUMNS = [
    "ret_1d", "ret_5d", "ret_21d", "ret_63d", "ret_126d", "ret_252d", "mom_12_1",
    "sma20", "sma50", "sma200", "ma_stack", "atr14", "realised_vol_21d",
    "rsi14", "adx14", "bb_z", "dist_52w_high", "dist_52w_low",
    "rvol_20", "adtv_20",
]


def _wilder(col: pl.Expr, n: int) -> pl.Expr:
    """Wilder's smoothing -- an EWM with alpha = 1/n.

    RSI and ADX are defined with Wilder smoothing, not a simple mean. Using a
    rolling mean here produces numbers that look plausible but disagree with
    every chart the user will compare against.
    """
    return col.ewm_mean(alpha=1.0 / n, adjust=False, min_samples=n)


def add_price_factors(df: pl.DataFrame) -> pl.DataFrame:
    """Add the standard factor set. `df` must be sorted by (symbol, d)."""
    s = pl.col("symbol")
    close, high, low = pl.col("close"), pl.col("high"), pl.col("low")

    df = df.sort(["symbol", "d"]).with_columns(
        # --- trailing returns
        (close / close.shift(1).over(s) - 1).alias("ret_1d"),
        (close / close.shift(5).over(s) - 1).alias("ret_5d"),
        (close / close.shift(21).over(s) - 1).alias("ret_21d"),
        (close / close.shift(63).over(s) - 1).alias("ret_63d"),
        (close / close.shift(126).over(s) - 1).alias("ret_126d"),
        (close / close.shift(252).over(s) - 1).alias("ret_252d"),
        # --- moving averages
        close.rolling_mean(20).over(s).alias("sma20"),
        close.rolling_mean(50).over(s).alias("sma50"),
        close.rolling_mean(200).over(s).alias("sma200"),
        # --- liquidity
        pl.col("turnover").rolling_mean(20).over(s).alias("adtv_20"),
        (pl.col("volume") / pl.col("volume").rolling_mean(20).over(s)).alias("rvol_20"),
        # --- 52-week extremes
        close.rolling_max(252).over(s).alias("_hi52"),
        close.rolling_min(252).over(s).alias("_lo52"),
        # --- true range components
        pl.max_horizontal(
            high - low,
            (high - close.shift(1).over(s)).abs(),
            (low - close.shift(1).over(s)).abs(),
        ).alias("_tr"),
        (high - high.shift(1).over(s)).alias("_up_move"),
        (low.shift(1).over(s) - low).alias("_down_move"),
        close.rolling_std(21).over(s).alias("_sd21"),
        close.rolling_std(20).over(s).alias("_sd20"),
    )

    # momentum 12-1: the standard academic definition skips the most recent
    # month, because short-horizon reversal contaminates raw 12-month momentum.
    df = df.with_columns(
        ((pl.col("close").shift(21).over(s) / pl.col("close").shift(252).over(s)) - 1)
        .alias("mom_12_1"),
        (pl.col("close") / pl.col("_hi52") - 1).alias("dist_52w_high"),
        (pl.col("close") / pl.col("_lo52") - 1).alias("dist_52w_low"),
        ((pl.col("close") - pl.col("sma20")) / pl.col("_sd20")).alias("bb_z"),
        # annualised realised vol from daily log returns
        ((pl.col("close") / pl.col("close").shift(1).over(s)).log()
         .rolling_std(21).over(s) * (252 ** 0.5)).alias("realised_vol_21d"),
        _wilder(pl.col("_tr"), 14).over(s).alias("atr14"),
        # directional movement, Wilder-smoothed
        pl.when((pl.col("_up_move") > pl.col("_down_move")) & (pl.col("_up_move") > 0))
          .then(pl.col("_up_move")).otherwise(0.0).alias("_plus_dm"),
        pl.when((pl.col("_down_move") > pl.col("_up_move")) & (pl.col("_down_move") > 0))
          .then(pl.col("_down_move")).otherwise(0.0).alias("_minus_dm"),
        # RSI: Wilder-smoothed average gain / loss
        pl.when(pl.col("ret_1d") > 0).then(pl.col("close") - pl.col("close").shift(1).over(s))
          .otherwise(0.0).alias("_gain"),
        pl.when(pl.col("ret_1d") < 0).then(pl.col("close").shift(1).over(s) - pl.col("close"))
          .otherwise(0.0).alias("_loss"),
    )

    df = df.with_columns(
        _wilder(pl.col("_gain"), 14).over(s).alias("_avg_gain"),
        _wilder(pl.col("_loss"), 14).over(s).alias("_avg_loss"),
        _wilder(pl.col("_plus_dm"), 14).over(s).alias("_sm_plus_dm"),
        _wilder(pl.col("_minus_dm"), 14).over(s).alias("_sm_minus_dm"),
        _wilder(pl.col("_tr"), 14).over(s).alias("_sm_tr"),
    ).with_columns(
        (100 - 100 / (1 + pl.col("_avg_gain") / pl.col("_avg_loss"))).alias("rsi14"),
        (100 * pl.col("_sm_plus_dm") / pl.col("_sm_tr")).alias("_pdi"),
        (100 * pl.col("_sm_minus_dm") / pl.col("_sm_tr")).alias("_mdi"),
    ).with_columns(
        _wilder(
            (100 * (pl.col("_pdi") - pl.col("_mdi")).abs()
             / (pl.col("_pdi") + pl.col("_mdi"))), 14
        ).over(s).alias("adx14"),
        # MA stack: +1 fully bullish alignment, -1 fully bearish, 0 mixed
        pl.when((pl.col("close") > pl.col("sma20")) & (pl.col("sma20") > pl.col("sma50"))
                & (pl.col("sma50") > pl.col("sma200"))).then(1)
          .when((pl.col("close") < pl.col("sma20")) & (pl.col("sma20") < pl.col("sma50"))
                & (pl.col("sma50") < pl.col("sma200"))).then(-1)
          .otherwise(0).cast(pl.Int8).alias("ma_stack"),
    )

    return df.drop([c for c in df.columns if c.startswith("_")])
