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

import math

import polars as pl

__all__ = ["FACTOR_COLUMNS", "add_price_factors"]

FACTOR_COLUMNS = [
    # --- Original 20 Core Factors
    "ret_1d", "ret_5d", "ret_21d", "ret_63d", "ret_126d", "ret_252d", "mom_12_1",
    "sma20", "sma50", "sma200", "ma_stack", "atr14", "realised_vol_21d",
    "rsi14", "adx14", "bb_z", "dist_52w_high", "dist_52w_low",
    "rvol_20", "adtv_20",
    # --- 1. Returns & Multi-Horizon Momentum (10 new)
    "ret_2d", "ret_3d", "ret_10d", "ret_42d", "mom_3_1", "mom_6_1",
    "overnight_ret_1d", "intraday_ret_1d", "overnight_ret_21d", "intraday_ret_21d",
    # --- 2. Moving Averages & Trend Structure (10 new)
    "sma10", "sma100", "ema12", "ema26", "macd_line", "macd_signal", "macd_hist",
    "price_to_sma20", "price_to_sma50", "price_to_sma200",
    # --- 3. Volatility, Range & Tail Risk (12 new)
    "atr7", "atr21", "atr_pct", "realised_vol_5d", "realised_vol_63d", "realised_vol_252d",
    "vol_ratio_5_21", "parkinson_vol_21d", "daily_range_pct", "close_in_range",
    "max_dd_63d", "max_dd_252d",
    # --- 4. Oscillators & Mean Reversion (8 new)
    "rsi7", "rsi21", "stoch_k_14", "stoch_d_14", "williams_r_14", "bb_width_20",
    "ret_z_20", "up_days_20",
    # --- 5. Volume, Turnover & Microstructure (10 new)
    "rvol_5", "rvol_63", "adtv_5", "adtv_63", "turnover_ratio_5_20", "turnover_z_20",
    "amihud_21d", "avg_trade_size", "trade_count_rvol_20", "up_down_vol_ratio_20",
]


def _wilder(col: pl.Expr, n: int) -> pl.Expr:
    """Wilder's smoothing -- an EWM with alpha = 1/n.

    RSI and ADX are defined with Wilder smoothing, not a simple mean. Using a
    rolling mean here produces numbers that look plausible but disagree with
    every chart the user will compare against.
    """
    return col.ewm_mean(alpha=1.0 / n, adjust=False, min_samples=n)


def add_price_factors(df: pl.DataFrame) -> pl.DataFrame:
    """Add the standard factor set (~70 strictly-trailing factors).

    `df` must be sorted by (symbol, d).
    """
    s = pl.col("symbol")
    close, high, low, open_p = pl.col("close"), pl.col("high"), pl.col("low"), pl.col("open")

    has_trades = "n_trades" in df.columns

    df = df.sort(["symbol", "d"]).with_columns(
        # --- Core trailing returns
        (close / close.shift(1).over(s) - 1).alias("ret_1d"),
        (close / close.shift(2).over(s) - 1).alias("ret_2d"),
        (close / close.shift(3).over(s) - 1).alias("ret_3d"),
        (close / close.shift(5).over(s) - 1).alias("ret_5d"),
        (close / close.shift(10).over(s) - 1).alias("ret_10d"),
        (close / close.shift(21).over(s) - 1).alias("ret_21d"),
        (close / close.shift(42).over(s) - 1).alias("ret_42d"),
        (close / close.shift(63).over(s) - 1).alias("ret_63d"),
        (close / close.shift(126).over(s) - 1).alias("ret_126d"),
        (close / close.shift(252).over(s) - 1).alias("ret_252d"),
        # Overnight and intraday single-session returns
        (open_p / close.shift(1).over(s) - 1).alias("overnight_ret_1d"),
        (close / open_p - 1).alias("intraday_ret_1d"),
        # --- Moving averages
        close.rolling_mean(10).over(s).alias("sma10"),
        close.rolling_mean(20).over(s).alias("sma20"),
        close.rolling_mean(50).over(s).alias("sma50"),
        close.rolling_mean(100).over(s).alias("sma100"),
        close.rolling_mean(200).over(s).alias("sma200"),
        # Exponential moving averages for MACD
        close.ewm_mean(alpha=2.0 / 13, adjust=False).over(s).alias("ema12"),
        close.ewm_mean(alpha=2.0 / 27, adjust=False).over(s).alias("ema26"),
        # --- Liquidity & ADTV
        pl.col("turnover").rolling_mean(5).over(s).alias("adtv_5"),
        pl.col("turnover").rolling_mean(20).over(s).alias("adtv_20"),
        pl.col("turnover").rolling_mean(63).over(s).alias("adtv_63"),
        # Relative volumes (rvol)
        pl.when(pl.col("volume").rolling_mean(5).over(s).is_null())
        .then(None)
        .when(pl.col("volume").rolling_mean(5).over(s) > 0)
        .then(pl.col("volume") / pl.col("volume").rolling_mean(5).over(s))
        .otherwise(0.0)
        .alias("rvol_5"),
        pl.when(pl.col("volume").rolling_mean(20).over(s).is_null())
        .then(None)
        .when(pl.col("volume").rolling_mean(20).over(s) > 0)
        .then(pl.col("volume") / pl.col("volume").rolling_mean(20).over(s))
        .otherwise(0.0)
        .alias("rvol_20"),
        pl.when(pl.col("volume").rolling_mean(63).over(s).is_null())
        .then(None)
        .when(pl.col("volume").rolling_mean(63).over(s) > 0)
        .then(pl.col("volume") / pl.col("volume").rolling_mean(63).over(s))
        .otherwise(0.0)
        .alias("rvol_63"),
        # --- 52-week & 63-day extremes
        close.rolling_max(63).over(s).alias("_hi63"),
        close.rolling_max(252).over(s).alias("_hi52"),
        close.rolling_min(252).over(s).alias("_lo52"),
        # --- True range components
        pl.max_horizontal(
            high - low,
            (high - close.shift(1).over(s)).abs(),
            (low - close.shift(1).over(s)).abs(),
        ).alias("_tr"),
        (high - high.shift(1).over(s)).alias("_up_move"),
        (low.shift(1).over(s) - low).alias("_down_move"),
        close.rolling_std(21).over(s).alias("_sd21"),
        close.rolling_std(20).over(s).alias("_sd20"),
        # Range & intraday position
        pl.when(close > 0).then((high - low) / close).otherwise(0.0).alias("daily_range_pct"),
        pl.when(high == low).then(0.5)
        .when((high - low) > 0).then((close - low) / (high - low))
        .otherwise(0.5)
        .alias("close_in_range"),
        # Parkinson range term: ((ln(H/L))^2) / (4 * ln 2)
        pl.when((high > 0) & (low > 0) & (high >= low))
        .then(((high / low).log()) ** 2)
        .otherwise(0.0)
        .alias("_parkinson_daily"),
    )

    # Secondary stage: ratios, momentum skips, MACD signal, and indicators
    df = df.with_columns(
        # mom_12_1: standard 12-month momentum skipping the most recent 1 month
        ((pl.col("close").shift(21).over(s) / pl.col("close").shift(252).over(s)) - 1)
        .alias("mom_12_1"),
        # mom_3_1: 3-month momentum skipping month 1 (sessions 21 to 63)
        ((pl.col("close").shift(21).over(s) / pl.col("close").shift(63).over(s)) - 1)
        .alias("mom_3_1"),
        # mom_6_1: 6-month momentum skipping month 1 (sessions 21 to 126)
        ((pl.col("close").shift(21).over(s) / pl.col("close").shift(126).over(s)) - 1)
        .alias("mom_6_1"),
        # Distances to 52w extremes
        (pl.col("close") / pl.col("_hi52") - 1).alias("dist_52w_high"),
        (pl.col("close") / pl.col("_lo52") - 1).alias("dist_52w_low"),
        # Drawdowns from rolling peaks
        (pl.col("close") / pl.col("_hi63") - 1).alias("max_dd_63d"),
        (pl.col("close") / pl.col("_hi52") - 1).alias("max_dd_252d"),
        # MACD Line
        (pl.col("ema12") - pl.col("ema26")).alias("macd_line"),
        # Price to SMAs
        pl.when(pl.col("sma20") > 0).then(pl.col("close") / pl.col("sma20") - 1).otherwise(0.0)
        .alias("price_to_sma20"),
        pl.when(pl.col("sma50") > 0).then(pl.col("close") / pl.col("sma50") - 1).otherwise(0.0)
        .alias("price_to_sma50"),
        pl.when(pl.col("sma200") > 0).then(pl.col("close") / pl.col("sma200") - 1).otherwise(0.0)
        .alias("price_to_sma200"),
        # Bollinger Bands z-score and width
        pl.when(pl.col("_sd20").is_null())
        .then(None)
        .when(pl.col("_sd20") > 0)
        .then((pl.col("close") - pl.col("sma20")) / pl.col("_sd20"))
        .otherwise(0.0)
        .alias("bb_z"),
        pl.when(pl.col("sma20") > 0)
        .then(4.0 * pl.col("_sd20") / pl.col("sma20"))
        .otherwise(0.0)
        .alias("bb_width_20"),
        # Annualised realised vol from daily log returns across multiple horizons
        ((pl.col("close") / pl.col("close").shift(1).over(s)).log()
         .rolling_std(5).over(s) * (252 ** 0.5)).alias("realised_vol_5d"),
        ((pl.col("close") / pl.col("close").shift(1).over(s)).log()
         .rolling_std(21).over(s) * (252 ** 0.5)).alias("realised_vol_21d"),
        ((pl.col("close") / pl.col("close").shift(1).over(s)).log()
         .rolling_std(63).over(s) * (252 ** 0.5)).alias("realised_vol_63d"),
        ((pl.col("close") / pl.col("close").shift(1).over(s)).log()
         .rolling_std(252).over(s) * (252 ** 0.5)).alias("realised_vol_252d"),
        # Parkinson 21d high-low range volatility
        (
            (pl.col("_parkinson_daily").rolling_mean(21).over(s) / (4.0 * math.log(2))).sqrt()
            * (252 ** 0.5)
        ).alias("parkinson_vol_21d"),
        # Multi-horizon ATRs
        _wilder(pl.col("_tr"), 7).over(s).alias("atr7"),
        _wilder(pl.col("_tr"), 14).over(s).alias("atr14"),
        _wilder(pl.col("_tr"), 21).over(s).alias("atr21"),
        # Directional movement components
        pl.when((pl.col("_up_move") > pl.col("_down_move")) & (pl.col("_up_move") > 0))
          .then(pl.col("_up_move")).otherwise(0.0).alias("_plus_dm"),
        pl.when((pl.col("_down_move") > pl.col("_up_move")) & (pl.col("_down_move") > 0))
          .then(pl.col("_down_move")).otherwise(0.0).alias("_minus_dm"),
        # Gain/loss components for RSI
        pl.when(pl.col("ret_1d") > 0).then(pl.col("close") - pl.col("close").shift(1).over(s))
          .otherwise(0.0).alias("_gain"),
        pl.when(pl.col("ret_1d") < 0).then(pl.col("close").shift(1).over(s) - pl.col("close"))
          .otherwise(0.0).alias("_loss"),
        # Stochastic Oscillator components: 14-period range
        high.rolling_max(14).over(s).alias("_hi14"),
        low.rolling_min(14).over(s).alias("_lo14"),
        # Overnight & Intraday 21-day log return sums
        pl.when((open_p > 0) & (pl.col("close").shift(1).over(s) > 0))
        .then((open_p / pl.col("close").shift(1).over(s)).log())
        .otherwise(0.0)
        .rolling_sum(21).over(s)
        .alias("overnight_ret_21d"),
        pl.when((close > 0) & (open_p > 0))
        .then((close / open_p).log())
        .otherwise(0.0)
        .rolling_sum(21).over(s)
        .alias("intraday_ret_21d"),
        # Return z-score over 20 sessions
        pl.when(pl.col("ret_1d").rolling_std(20).over(s) > 0)
        .then(pl.col("ret_1d") / pl.col("ret_1d").rolling_std(20).over(s))
        .otherwise(0.0)
        .alias("ret_z_20"),
        # Fraction of up days over last 20 sessions
        pl.when(pl.col("ret_1d") > 0).then(1.0).otherwise(0.0).rolling_mean(20).over(s).alias("up_days_20"),
        # Turnover ratio & z-score
        pl.when(pl.col("adtv_20") > 0).then(pl.col("adtv_5") / pl.col("adtv_20")).otherwise(0.0)
        .alias("turnover_ratio_5_20"),
        pl.when(pl.col("turnover").rolling_std(20).over(s) > 0)
        .then((pl.col("turnover") - pl.col("adtv_20")) / pl.col("turnover").rolling_std(20).over(s))
        .otherwise(0.0)
        .alias("turnover_z_20"),
        # Amihud illiquidity measure: 21d rolling mean of (|ret| / turnover * 1e7)
        pl.when(pl.col("turnover") > 0)
        .then((pl.col("ret_1d").abs() / pl.col("turnover")) * 1e7)
        .otherwise(0.0)
        .rolling_mean(21).over(s)
        .alias("amihud_21d"),
        # Volume on up-days vs down-days over 20 sessions
        pl.when(pl.col("ret_1d") > 0).then(pl.col("volume")).otherwise(0.0)
        .rolling_sum(20).over(s)
        .alias("_up_vol_20"),
        pl.when(pl.col("ret_1d") < 0).then(pl.col("volume")).otherwise(0.0)
        .rolling_sum(20).over(s)
        .alias("_dn_vol_20"),
        # Trade statistics (guarded if n_trades is present)
        (
            pl.when(pl.col("n_trades") > 0).then(pl.col("turnover") / pl.col("n_trades")).otherwise(0.0)
            if has_trades else pl.lit(0.0)
        ).alias("avg_trade_size"),
        (
            pl.when(pl.col("n_trades").rolling_mean(20).over(s) > 0)
            .then(pl.col("n_trades") / pl.col("n_trades").rolling_mean(20).over(s))
            .otherwise(1.0)
            if has_trades else pl.lit(1.0)
        ).alias("trade_count_rvol_20"),
    )

    # Third stage: MACD signal, ATR%, vol ratio, RSI (7, 14, 21), ADX, Stochastics
    df = df.with_columns(
        # MACD Signal (9-day EMA of macd_line)
        pl.col("macd_line").ewm_mean(alpha=2.0 / 10, adjust=False).over(s).alias("macd_signal"),
        # ATR % of price
        pl.when(close > 0).then(pl.col("atr14") / close).otherwise(0.0).alias("atr_pct"),
        # Volatility ratio (short-term 5d vs medium-term 21d)
        pl.when(pl.col("realised_vol_5d").is_null() | pl.col("realised_vol_21d").is_null())
        .then(None)
        .when(pl.col("realised_vol_21d") > 0)
        .then(pl.col("realised_vol_5d") / pl.col("realised_vol_21d"))
        .otherwise(0.0)
        .alias("vol_ratio_5_21"),
        # Up/down volume ratio
        pl.when(pl.col("_up_vol_20").is_null() | pl.col("_dn_vol_20").is_null())
        .then(None)
        .when(pl.col("_dn_vol_20") > 0)
        .then(pl.col("_up_vol_20") / pl.col("_dn_vol_20"))
        .when(pl.col("_up_vol_20") == 0)
        .then(1.0)
        .otherwise(10.0)
        .alias("up_down_vol_ratio_20"),
        # Stochastic %K (14)
        pl.when(pl.col("_hi14") == pl.col("_lo14"))
        .then(50.0)
        .when((pl.col("_hi14") - pl.col("_lo14")) > 0)
        .then(100.0 * (close - pl.col("_lo14")) / (pl.col("_hi14") - pl.col("_lo14")))
        .otherwise(50.0)
        .alias("stoch_k_14"),
        # Wilder smoothings for RSI 7, 14, 21
        _wilder(pl.col("_gain"), 7).over(s).alias("_avg_gain_7"),
        _wilder(pl.col("_loss"), 7).over(s).alias("_avg_loss_7"),
        _wilder(pl.col("_gain"), 14).over(s).alias("_avg_gain"),
        _wilder(pl.col("_loss"), 14).over(s).alias("_avg_loss"),
        _wilder(pl.col("_gain"), 21).over(s).alias("_avg_gain_21"),
        _wilder(pl.col("_loss"), 21).over(s).alias("_avg_loss_21"),
        _wilder(pl.col("_plus_dm"), 14).over(s).alias("_sm_plus_dm"),
        _wilder(pl.col("_minus_dm"), 14).over(s).alias("_sm_minus_dm"),
        _wilder(pl.col("_tr"), 14).over(s).alias("_sm_tr"),
    )

    # Fourth stage: MACD Hist, Stoch %D, Williams %R, RSI evaluations, ADX components
    df = df.with_columns(
        (pl.col("macd_line") - pl.col("macd_signal")).alias("macd_hist"),
        pl.col("stoch_k_14").rolling_mean(3).over(s).alias("stoch_d_14"),
        (pl.col("stoch_k_14") - 100.0).alias("williams_r_14"),
        # RSI 7
        pl.when(pl.col("_avg_gain_7").is_null() | pl.col("_avg_loss_7").is_null())
        .then(None)
        .when((pl.col("_avg_gain_7") == 0) & (pl.col("_avg_loss_7") == 0))
        .then(50.0)
        .when(pl.col("_avg_loss_7") == 0)
        .then(100.0)
        .otherwise(100.0 - 100.0 / (1.0 + pl.col("_avg_gain_7") / pl.col("_avg_loss_7")))
        .alias("rsi7"),
        # RSI 14
        pl.when(pl.col("_avg_gain").is_null() | pl.col("_avg_loss").is_null())
        .then(None)
        .when((pl.col("_avg_gain") == 0) & (pl.col("_avg_loss") == 0))
        .then(50.0)
        .when(pl.col("_avg_loss") == 0)
        .then(100.0)
        .otherwise(100.0 - 100.0 / (1.0 + pl.col("_avg_gain") / pl.col("_avg_loss")))
        .alias("rsi14"),
        # RSI 21
        pl.when(pl.col("_avg_gain_21").is_null() | pl.col("_avg_loss_21").is_null())
        .then(None)
        .when((pl.col("_avg_gain_21") == 0) & (pl.col("_avg_loss_21") == 0))
        .then(50.0)
        .when(pl.col("_avg_loss_21") == 0)
        .then(100.0)
        .otherwise(100.0 - 100.0 / (1.0 + pl.col("_avg_gain_21") / pl.col("_avg_loss_21")))
        .alias("rsi21"),
        # Directional indicators +DI / -DI
        pl.when(pl.col("_sm_tr").is_null())
        .then(None)
        .when(pl.col("_sm_tr") > 0)
        .then(100.0 * pl.col("_sm_plus_dm") / pl.col("_sm_tr"))
        .otherwise(0.0)
        .alias("_pdi"),
        pl.when(pl.col("_sm_tr").is_null())
        .then(None)
        .when(pl.col("_sm_tr") > 0)
        .then(100.0 * pl.col("_sm_minus_dm") / pl.col("_sm_tr"))
        .otherwise(0.0)
        .alias("_mdi"),
    ).with_columns(
        # ADX 14
        _wilder(
            pl.when(pl.col("_pdi").is_null() | pl.col("_mdi").is_null())
            .then(None)
            .when((pl.col("_pdi") + pl.col("_mdi")) > 0)
            .then(
                100.0
                * (pl.col("_pdi") - pl.col("_mdi")).abs()
                / (pl.col("_pdi") + pl.col("_mdi"))
            )
            .otherwise(0.0),
            14,
        ).over(s).alias("adx14"),
        # MA stack: +1 fully bullish alignment, -1 fully bearish, 0 mixed
        pl.when((pl.col("close") > pl.col("sma20")) & (pl.col("sma20") > pl.col("sma50"))
                & (pl.col("sma50") > pl.col("sma200"))).then(1)
          .when((pl.col("close") < pl.col("sma20")) & (pl.col("sma20") < pl.col("sma50"))
                & (pl.col("sma50") < pl.col("sma200"))).then(-1)
          .otherwise(0).cast(pl.Int8).alias("ma_stack"),
    )

    return df.drop([c for c in df.columns if c.startswith("_")])
