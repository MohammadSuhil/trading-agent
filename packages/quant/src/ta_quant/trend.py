"""Multi-timeframe trend classification.

Trend is the load-bearing half of the dashboard's "analysis" surface, and the
easiest place to fool yourself. Three guards against that, each answering a
specific way the naive version lies:

1. **The drift statistic is HAC-corrected.** The obvious approach -- regress
   log(close) on t and read the slope's t-stat -- is badly wrong on price data:
   the residuals are strongly autocorrelated, the OLS standard error is far too
   small, and almost every random walk scores "significant". We instead take the
   t-stat of the MEAN DAILY LOG RETURN with a Newey-West (Bartlett) variance,
   lag L = floor(N**(1/3)). Returns are far closer to serially uncorrelated, and
   the Bartlett weights absorb what remains.

2. **Direction is not strength.** `UP` requires a *significant* drift; a stock
   that drifted up 2% over 63 noisy sessions is `RANGE`. The Kaufman efficiency
   ratio separates "went up" from "went up in a straight line", and ADX's
   10-session delta separates "trending" from "trending less than it was",
   which is what the `*_DECAYING` states capture.

3. **`UNDEFINED` is not `RANGE`.** Too little history, or a failed liquidity
   gate, means the trend was never measured. Recording that as "range-bound"
   would feed the learning loop an observation that never happened.

A state change requires the raw predicate to hold on two consecutive sessions.
Without that hysteresis a symbol oscillating around a threshold generates a
stream of state flips, each of which would look like a real event to the
attribution layer.
"""
from __future__ import annotations

import polars as pl
from ta_core.enums import TREND_ENUM_VERSION, TrendState

__all__ = ["LOOKBACKS", "TREND_COLUMNS", "add_trend_states", "nw_lag"]

#: The three timeframes. Short enough to act on, long enough to mean something.
LOOKBACKS: tuple[int, ...] = (20, 63, 252)

TREND_COLUMNS = [
    *(f"trend_{n}" for n in LOOKBACKS),
    *(f"t_drift_{n}" for n in LOOKBACKS),
    *(f"er_{n}" for n in LOOKBACKS),
    "adx_delta_10",
    "flips_63d",
    "bars_seen",
    "trend_enum_version",
]


def nw_lag(n: int) -> int:
    """Newey-West truncation lag, the standard floor(N**(1/3)) rule.

    >>> [nw_lag(n) for n in (20, 63, 252)]
    [2, 3, 6]
    """
    return max(1, int(n ** (1.0 / 3.0)))


def _hac_t_stat(ret: pl.Expr, n: int, s: pl.Expr) -> pl.Expr:
    """t-stat of mean daily log return over `n` sessions, Newey-West variance.

    gamma_k is the lag-k autocovariance estimated inside the same window, and
    the Bartlett weight (1 - k/(L+1)) keeps the variance estimate positive
    semi-definite. Without the gamma terms this reduces to the naive t-stat,
    which is exactly the overstatement we are correcting.
    """
    lag = nw_lag(n)
    mean = ret.rolling_mean(n).over(s)
    gamma0 = (ret * ret).rolling_mean(n).over(s) - mean**2

    var = gamma0
    for k in range(1, lag + 1):
        gamma_k = (ret * ret.shift(k).over(s)).rolling_mean(n).over(s) - mean**2
        var = var + 2.0 * (1.0 - k / (lag + 1.0)) * gamma_k

    # A HAC variance can go negative in small samples; clamp to a tiny positive
    # so the t-stat is finite and the row is simply uninformative rather than
    # null-propagating through every downstream comparison.
    var = pl.when(var > 1e-12).then(var).otherwise(1e-12)
    return mean / (var / n).sqrt()


def _efficiency_ratio(close: pl.Expr, n: int, s: pl.Expr) -> pl.Expr:
    """Kaufman efficiency ratio: net move / total path length, in [0, 1].

    1.0 is a straight line; ~0 is a round trip. This is what distinguishes a
    trend you can hold from a chop that happens to end higher.
    """
    net = (close - close.shift(n).over(s)).abs()
    path = (close - close.shift(1).over(s)).abs().rolling_sum(n).over(s)
    return pl.when(path > 0).then(net / path).otherwise(0.0)


def _classify(n: int) -> pl.Expr:
    """First-match-wins predicate table for one lookback.

    Thresholds are deliberately asymmetric between "strong" and plain: the
    strong states gate position sizing, so they must be rare.
    """
    t = pl.col(f"t_drift_{n}")
    er = pl.col(f"er_{n}")
    adx = pl.col("adx14")
    d_adx = pl.col("adx_delta_10")
    stack = pl.col("ma_stack")
    enough = pl.col("bars_seen") >= (n + 21)

    return (
        pl.when(~enough | t.is_null() | er.is_null() | adx.is_null())
        .then(pl.lit(TrendState.UNDEFINED.value))
        # --- strong: significant drift, straight path, aligned MAs, real ADX
        .when((t >= 2.5) & (er >= 0.35) & (stack == 1) & (adx >= 25))
        .then(pl.lit(TrendState.UP_STRONG.value))
        .when((t <= -2.5) & (er >= 0.35) & (stack == -1) & (adx >= 25))
        .then(pl.lit(TrendState.DOWN_STRONG.value))
        # --- decaying: still directional, but ADX is rolling over. Blocks new
        #     trend-following entries; never forces an exit, never flips side.
        .when((t >= 1.7) & (er >= 0.25) & (d_adx < -2.0))
        .then(pl.lit(TrendState.UP_DECAYING.value))
        .when((t <= -1.7) & (er >= 0.25) & (d_adx < -2.0))
        .then(pl.lit(TrendState.DOWN_DECAYING.value))
        # --- plain directional
        .when((t >= 1.7) & (er >= 0.25))
        .then(pl.lit(TrendState.UP.value))
        .when((t <= -1.7) & (er >= 0.25))
        .then(pl.lit(TrendState.DOWN.value))
        .otherwise(pl.lit(TrendState.RANGE.value))
    )


def add_trend_states(df: pl.DataFrame) -> pl.DataFrame:
    """Add trend columns. `df` needs close/high/low, `adx14` and `ma_stack`.

    Expects the output of `ta_quant.factors.price.add_price_factors`, sorted by
    (symbol, d).
    """
    s = pl.col("symbol")
    close = pl.col("close")
    ret = (close / close.shift(1).over(s)).log()

    df = df.sort(["symbol", "d"]).with_columns(
        pl.int_range(pl.len()).over(s).alias("bars_seen"),
        (pl.col("adx14") - pl.col("adx14").shift(10).over(s)).alias("adx_delta_10"),
    )

    df = df.with_columns(
        *[_hac_t_stat(ret, n, s).alias(f"t_drift_{n}") for n in LOOKBACKS],
        *[_efficiency_ratio(close, n, s).alias(f"er_{n}") for n in LOOKBACKS],
    )

    df = df.with_columns(*[_classify(n).alias(f"_raw_{n}") for n in LOOKBACKS])

    # Hysteresis: adopt a new state only on its SECOND consecutive session.
    # A one-day excursion across a threshold reports the prior state instead,
    # so a symbol sitting on a boundary does not emit a stream of "events".
    df = df.with_columns(
        *[
            pl.when(pl.col(f"_raw_{n}") == pl.col(f"_raw_{n}").shift(1).over(s))
            .then(pl.col(f"_raw_{n}"))
            .otherwise(pl.col(f"_raw_{n}").shift(1).over(s))
            .fill_null(pl.lit(TrendState.UNDEFINED.value))
            .alias(f"trend_{n}")
            for n in LOOKBACKS
        ]
    )

    df = df.with_columns(
        # Whipsaw count. A name that has changed its mind six times in a
        # quarter is not trending, whatever today's state says.
        (pl.col("trend_63") != pl.col("trend_63").shift(1).over(s))
        .cast(pl.Int32)
        .rolling_sum(63)
        .over(s)
        .fill_null(0)
        .alias("flips_63d"),
        pl.lit(TREND_ENUM_VERSION).cast(pl.Int16).alias("trend_enum_version"),
    )

    return df.drop([c for c in df.columns if c.startswith("_")])
