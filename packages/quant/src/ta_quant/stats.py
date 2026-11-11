"""Statistical validation, Deflated Sharpe Ratio, and bootstrap expectancy.

Zero external stats libraries: pure Python math (erf, sqrt, log) and Acklam's
rational approximation for the normal inverse CDF (< 1e-8 accuracy).

References:
  - Bailey, D. H., & López de Prado, M. (2012). The Sharpe ratio efficient frontier.
    Journal of Risk, 15(2), 3-44.
  - Bailey, D. H., & López de Prado, M. (2014). The deflated Sharpe ratio:
    Correcting for selection bias, backtest overfitting, and non-normality.
    Journal of Portfolio Management, 40(5), 94-107.
"""
from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BacktestStats",
    "bootstrap_expectancy_ci",
    "compute_backtest_stats",
    "deflated_sharpe_ratio",
    "dsr",
    "expected_max_sharpe",
    "min_track_record_length",
    "min_trl",
    "norm_cdf",
    "norm_ppf",
    "probabilistic_sharpe_ratio",
    "psr",
]

# Euler-Mascheroni constant
EULER_MASCHERONI: float = 0.5772156649015329

# Acklam coefficients for central region
_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)

# Acklam coefficients for tails
_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)

_P_LOW = 0.02425
_P_HIGH = 1.0 - _P_LOW


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function Phi(x).

    >>> round(norm_cdf(0.0), 4) == 0.5
    True
    >>> round(norm_cdf(1.96), 4) == 0.9750
    True
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Standard normal percent point function (inverse CDF) Phi^{-1}(p).

    Uses Acklam's rational approximation accurate to < 1.15e-9.

    >>> round(norm_ppf(0.95), 4) == 1.6449
    True
    >>> norm_ppf(0.5) == 0.0
    True
    """
    if p <= 0.0 or p >= 1.0:
        raise ValueError(f"p must be strictly in (0, 1), got {p}")

    if p < _P_LOW:
        # Rational approximation for lower tail
        q = math.sqrt(-2.0 * math.log(p))
        return (
            ((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]
        ) / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)

    if p <= _P_HIGH:
        # Rational approximation for central region
        q = p - 0.5
        r = q * q
        return (
            ((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]
        ) * q / (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)

    # Rational approximation for upper tail
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(
        ((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]
    ) / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)


def probabilistic_sharpe_ratio(
    sr: float,
    sr_benchmark: float,
    n_obs: int,
    *,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """Probabilistic Sharpe Ratio (Bailey & López de Prado 2012).

    Computes P(SR > SR*) adjusted for non-normality (skewness and Pearson kurtosis).
    Returns 0.0 if n_obs <= 2 or if denominator variance is non-positive.

    >>> round(probabilistic_sharpe_ratio(0.0, 0.0, 100), 4) == 0.5
    True
    >>> probabilistic_sharpe_ratio(1.0, 0.0, 1) == 0.0
    True
    """
    if n_obs <= 2:
        return 0.0

    denom_sq = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * (sr**2)
    if denom_sq <= 0.0:
        return 0.0

    z = (sr - sr_benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom_sq)
    return norm_cdf(z)


def expected_max_sharpe(n_trials: int, std_sr: float = 1.0) -> float:
    """Expected Maximum Sharpe Ratio under N independent trials (Bailey & López de Prado 2014).

    When n_trials <= 1 or std_sr <= 0, returns 0.0.

    >>> expected_max_sharpe(1) == 0.0
    True
    >>> round(expected_max_sharpe(10, 1.0), 4)
    1.5746
    """
    if n_trials <= 1 or std_sr <= 0.0:
        return 0.0

    p1 = 1.0 - 1.0 / n_trials
    p2 = 1.0 - 1.0 / (n_trials * math.e)

    p1 = max(1e-12, min(1.0 - 1e-12, p1))
    p2 = max(1e-12, min(1.0 - 1e-12, p2))

    z1 = norm_ppf(p1)
    z2 = norm_ppf(p2)

    return std_sr * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)


def deflated_sharpe_ratio(
    sr: float,
    n_obs: int,
    n_trials: int,
    *,
    skew: float = 0.0,
    kurt: float = 3.0,
    std_sr: float = 1.0,
) -> float:
    """Deflated Sharpe Ratio (Bailey & López de Prado 2014).

    Computes PSR against the expected maximum Sharpe ratio under n_trials trials.

    >>> round(deflated_sharpe_ratio(0.0, 100, 1), 4) == 0.5
    True
    """
    sr_0 = expected_max_sharpe(n_trials, std_sr=std_sr)
    return probabilistic_sharpe_ratio(sr, sr_0, n_obs, skew=skew, kurt=kurt)


def min_trl(
    sr: float,
    sr_benchmark: float = 0.0,
    *,
    skew: float = 0.0,
    kurt: float = 3.0,
    alpha: float = 0.05,
) -> float:
    """Minimum Track Record Length in number of observations (Bailey & López de Prado 2012)."""
    if sr <= sr_benchmark:
        return float("inf")
    denom_sq = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * (sr**2)
    if denom_sq <= 0.0:
        return float("inf")
    z_alpha = norm_ppf(1.0 - alpha)
    return 1.0 + denom_sq * ((z_alpha / (sr - sr_benchmark)) ** 2)


min_track_record_length = min_trl
psr = probabilistic_sharpe_ratio
dsr = deflated_sharpe_ratio


def bootstrap_expectancy_ci(
    r_multiples: Sequence[float],
    *,
    n_boot: int = 1000,
    ci: float = 0.90,
    seed: int = 42,
) -> tuple[float, float, float]:
    """Deterministic seeded bootstrap confidence interval for mean expectancy (R-multiples).

    Returns (mean, ci_lo, ci_hi).
    If r_multiples is empty, returns (0.0, 0.0, 0.0).

    >>> bootstrap_expectancy_ci([])
    (0.0, 0.0, 0.0)
    >>> mean, lo, hi = bootstrap_expectancy_ci([1.0, 1.0, 1.0], seed=42)
    >>> mean == lo == hi == 1.0
    True
    """
    if not r_multiples:
        return 0.0, 0.0, 0.0

    n = len(r_multiples)
    mean = sum(r_multiples) / n

    if n == 1 or len(set(r_multiples)) == 1:
        return round(mean, 4), round(mean, 4), round(mean, 4)

    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(n_boot):
        sample_sum = sum(r_multiples[rng.randrange(n)] for _ in range(n))
        boot_means.append(sample_sum / n)

    boot_means.sort()
    alpha = (1.0 - ci) / 2.0
    idx_lo = max(0, min(n_boot - 1, int(alpha * n_boot)))
    idx_hi = max(0, min(n_boot - 1, int((1.0 - alpha) * n_boot)))

    return round(mean, 4), round(boot_means[idx_lo], 4), round(boot_means[idx_hi], 4)


@dataclass(frozen=True, slots=True)
class BacktestStats:
    """Summary statistics for backtest and paper trading performance."""

    n_trades: int
    n_sessions: int
    n_trials: int
    total_return_pct: float
    cagr_pct: float
    sharpe_annualised: float
    psr: float
    dsr: float
    max_drawdown_pct: float
    hit_rate: float
    profit_factor: float
    expectancy_r: float
    expectancy_ci_lo: float
    expectancy_ci_hi: float
    total_costs_rupees: float
    avg_cost_bps: float
    avg_stop_slippage_bps: float
    positive_deflated_edge: bool


def compute_backtest_stats(
    equity_curve: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    *,
    n_trials: int = 1,
    initial_equity: float = 500_000.0,
) -> BacktestStats:
    """Compute comprehensive backtest performance and deflated risk statistics.

    Daily returns are derived from the equity curve; Sharpe is annualized via sqrt(252).
    PSR measures statistical significance over SR*=0; DSR adjusts for multiple testing.
    """
    n_sessions = len(equity_curve)
    n_trades = len(outcomes)

    if n_sessions == 0:
        return BacktestStats(
            n_trades=0,
            n_sessions=0,
            n_trials=n_trials,
            total_return_pct=0.0,
            cagr_pct=0.0,
            sharpe_annualised=0.0,
            psr=0.0,
            dsr=0.0,
            max_drawdown_pct=0.0,
            hit_rate=0.0,
            profit_factor=1.0,
            expectancy_r=0.0,
            expectancy_ci_lo=0.0,
            expectancy_ci_hi=0.0,
            total_costs_rupees=0.0,
            avg_cost_bps=0.0,
            avg_stop_slippage_bps=0.0,
            positive_deflated_edge=False,
        )

    final_equity = float(equity_curve[-1]["equity"])
    total_return_pct = (
        round((final_equity - initial_equity) / initial_equity, 6)
        if initial_equity > 0
        else 0.0
    )

    # CAGR calculation assuming 252 trading sessions per year
    if n_sessions > 1 and final_equity > 0 and initial_equity > 0:
        years = n_sessions / 252.0
        cagr_pct = round((final_equity / initial_equity) ** (1.0 / years) - 1.0, 6)
    else:
        cagr_pct = 0.0

    # Daily returns series
    daily_returns: list[float] = []
    for i in range(1, n_sessions):
        prev_eq = float(equity_curve[i - 1]["equity"])
        curr_eq = float(equity_curve[i]["equity"])
        r = (curr_eq - prev_eq) / prev_eq if prev_eq > 0 else 0.0
        daily_returns.append(r)

    # Sharpe, Skewness, Kurtosis
    m = len(daily_returns)
    if m >= 2:
        mean_r = sum(daily_returns) / m
        var_r = sum((x - mean_r) ** 2 for x in daily_returns) / (m - 1)
        std_r = math.sqrt(var_r)

        if std_r > 1e-12:
            daily_sr = mean_r / std_r
            sharpe_annualised = round(math.sqrt(252.0) * daily_sr, 4)

            # Sample skewness & Pearson kurtosis (normal = 3.0)
            m3 = sum((x - mean_r) ** 3 for x in daily_returns) / m
            m4 = sum((x - mean_r) ** 4 for x in daily_returns) / m
            skew = m3 / (std_r**3)
            kurt = m4 / (std_r**4)
        else:
            sharpe_annualised = 0.0
            skew = 0.0
            kurt = 3.0
    else:
        sharpe_annualised = 0.0
        skew = 0.0
        kurt = 3.0

    # PSR against 0.0 benchmark
    psr = round(
        probabilistic_sharpe_ratio(
            sharpe_annualised, 0.0, max(2, m), skew=skew, kurt=kurt
        ),
        4,
    )

    # DSR under n_trials
    std_sr_trials = max(0.5, 1.0 / math.sqrt(max(1, m)))
    dsr = round(
        deflated_sharpe_ratio(
            sharpe_annualised,
            max(2, m),
            n_trials,
            skew=skew,
            kurt=kurt,
            std_sr=std_sr_trials,
        ),
        4,
    )

    # Max Drawdown
    max_dd = max((float(r.get("drawdown", 0.0)) for r in equity_curve), default=0.0)
    max_drawdown_pct = round(max_dd, 4)

    # Trade outcomes stats
    if n_trades > 0:
        pnls = [float(o.get("pnl_rupees", 0.0)) for o in outcomes]
        wins = [p for p in pnls if p > 0.0]
        losses = [p for p in pnls if p < 0.0]

        hit_rate = round(len(wins) / n_trades, 4)
        sum_wins = sum(wins)
        sum_losses = abs(sum(losses))
        profit_factor = (
            round(sum_wins / sum_losses, 4)
            if sum_losses > 0
            else (999.0 if sum_wins > 0 else 1.0)
        )

        r_multiples = [float(o.get("r_multiple", 0.0)) for o in outcomes]
        exp_r, ci_lo, ci_hi = bootstrap_expectancy_ci(r_multiples)

        total_costs_rupees = round(sum(float(o.get("cost_rupees", 0.0)) for o in outcomes), 2)
        total_notional = sum(
            float(o.get("gross_notional") or (float(o.get("entry_price", 0.0)) * float(o.get("qty", 1))))
            for o in outcomes
        )
        avg_cost_bps = (
            round((total_costs_rupees / total_notional) * 10_000.0, 2)
            if total_notional > 0
            else 0.0
        )
        avg_stop_slippage_bps = round(
            sum(float(o.get("stop_slippage_bps", 0.0)) for o in outcomes) / n_trades,
            2,
        )
    else:
        hit_rate = 0.0
        profit_factor = 1.0
        exp_r, ci_lo, ci_hi = 0.0, 0.0, 0.0
        total_costs_rupees = 0.0
        avg_cost_bps = 0.0
        avg_stop_slippage_bps = 0.0

    positive_deflated_edge = bool(exp_r > 0.0 and dsr >= 0.50)

    return BacktestStats(
        n_trades=n_trades,
        n_sessions=n_sessions,
        n_trials=n_trials,
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        sharpe_annualised=sharpe_annualised,
        psr=psr,
        dsr=dsr,
        max_drawdown_pct=max_drawdown_pct,
        hit_rate=hit_rate,
        profit_factor=profit_factor,
        expectancy_r=exp_r,
        expectancy_ci_lo=ci_lo,
        expectancy_ci_hi=ci_hi,
        total_costs_rupees=total_costs_rupees,
        avg_cost_bps=avg_cost_bps,
        avg_stop_slippage_bps=avg_stop_slippage_bps,
        positive_deflated_edge=positive_deflated_edge,
    )


# ------------------------------------------------------- forward Monte Carlo
#: PLAN.md "Honesty rules": the forward Monte Carlo refuses below these. A
#: distribution fitted to less history is a picture of noise.
SIM_MIN_DAYS = 750
SIM_MIN_TRADES = 200


@dataclass(frozen=True, slots=True)
class ForwardSimulation:
    status: str                       # "OK" | "INSUFFICIENT"
    n_days: int
    n_trades: int
    horizon: int
    block_len: int | None = None
    n_paths: int = 0
    seed: int = 0
    p_lo: float | None = None         # 5th pct of horizon return (fraction)
    p_hi: float | None = None         # 95th pct
    p_loss: float | None = None       # share of paths ending below start
    histogram: tuple[tuple[float, int], ...] = ()


def block_bootstrap_forward(
    daily_returns: Sequence[float],
    *,
    n_trades: int,
    horizon: int = 252,
    n_paths: int = 2000,
    hold_sessions: int = 10,
    seed: int = 20261111,
    n_buckets: int = 20,
) -> ForwardSimulation:
    """Stationary block bootstrap (Politis & Romano 1994) of one-year outcomes.

    Resamples the engine's REALISED daily returns in blocks of geometric
    length, so a losing streak stays a streak -- an iid bootstrap would shuffle
    a 10-session drawdown into 10 unrelated bad days and understate the tail.
    The mean block length is at least the 10-session swing hold, because a
    position's daily returns are serially dependent for that long.

    Refuses (returns INSUFFICIENT, no numbers) below SIM_MIN_DAYS of history or
    SIM_MIN_TRADES closed trades. Seeded, so a replay reproduces it exactly.

    >>> sim = block_bootstrap_forward([0.001] * 100, n_trades=10)
    >>> sim.status, sim.p_lo, sim.p_hi
    ('INSUFFICIENT', None, None)
    >>> sim = block_bootstrap_forward([0.001, -0.0005] * 400, n_trades=250, n_paths=200)
    >>> sim.status, sim.block_len, sim.n_days
    ('OK', 10, 800)
    >>> sim.p_lo <= sim.p_hi and 0.0 <= sim.p_loss <= 1.0
    True
    >>> sim == block_bootstrap_forward([0.001, -0.0005] * 400, n_trades=250, n_paths=200)
    True
    """
    n = len(daily_returns)
    if n < SIM_MIN_DAYS or n_trades < SIM_MIN_TRADES:
        return ForwardSimulation("INSUFFICIENT", n, n_trades, horizon)

    block_len = max(hold_sessions, math.ceil(n ** (1 / 3)))
    p_new_block = 1.0 / block_len
    rng = random.Random(seed)
    ends: list[float] = []
    for _ in range(n_paths):
        growth, i = 1.0, rng.randrange(n)
        for _ in range(horizon):
            growth *= 1.0 + daily_returns[i]
            i = rng.randrange(n) if rng.random() < p_new_block else (i + 1) % n
        ends.append(growth - 1.0)
    ends.sort()

    def pct(q: float) -> float:
        return ends[min(len(ends) - 1, int(q * len(ends)))]

    lo, hi = ends[0], ends[-1]
    width = (hi - lo) / n_buckets or 1.0
    counts = [0] * n_buckets
    for e in ends:
        counts[min(n_buckets - 1, int((e - lo) / width))] += 1
    hist = tuple((round(lo + k * width, 6), c) for k, c in enumerate(counts))
    return ForwardSimulation(
        "OK", n, n_trades, horizon, block_len, n_paths, seed,
        p_lo=round(pct(0.05), 6), p_hi=round(pct(0.95), 6),
        p_loss=round(sum(e < 0 for e in ends) / len(ends), 6), histogram=hist,
    )
