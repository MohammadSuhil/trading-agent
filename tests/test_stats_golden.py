"""Golden tests for statistical validation, PSR, DSR, and bootstrap expectancy.

Validates analytical formulas, numerical stability, Acklam's inverse CDF accuracy,
and non-normal distribution penalty invariants.
"""
from __future__ import annotations

import pytest
from ta_quant.stats import (
    BacktestStats,
    bootstrap_expectancy_ci,
    compute_backtest_stats,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    norm_cdf,
    norm_ppf,
    probabilistic_sharpe_ratio,
)


def test_norm_cdf_and_ppf_precision():
    """Verify normal CDF and Acklam inverse CDF precision against analytical constants."""
    assert norm_cdf(0.0) == 0.5
    assert round(norm_cdf(1.959964), 4) == 0.9750
    assert round(norm_cdf(-1.959964), 4) == 0.0250

    assert round(norm_ppf(0.95), 4) == 1.6449
    assert norm_ppf(0.5) == 0.0

    # Invertibility test over multiple percentiles
    test_probs = [0.001, 0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99, 0.999]
    for p in test_probs:
        x = norm_ppf(p)
        reconstructed = norm_cdf(x)
        assert reconstructed == pytest.approx(p, abs=1e-8)

    with pytest.raises(ValueError, match="strictly in \\(0, 1\\)"):
        norm_ppf(0.0)

    with pytest.raises(ValueError, match="strictly in \\(0, 1\\)"):
        norm_ppf(1.0)


def test_expected_max_sharpe():
    """Verify Expected Maximum Sharpe Ratio scaling under multiple independent trials."""
    # N <= 1 must return 0.0
    assert expected_max_sharpe(1) == 0.0
    assert expected_max_sharpe(0) == 0.0
    assert expected_max_sharpe(-5) == 0.0

    # Monotonicity with number of trials
    em2 = expected_max_sharpe(2)
    em10 = expected_max_sharpe(10)
    em50 = expected_max_sharpe(50)
    em100 = expected_max_sharpe(100)

    assert 0.0 < em2 < em10 < em50 < em100

    # Linear scaling with standard deviation of Sharpe
    assert expected_max_sharpe(20, std_sr=2.0) == pytest.approx(
        2.0 * expected_max_sharpe(20, std_sr=1.0), rel=1e-6
    )


def test_probabilistic_sharpe_ratio_invariants():
    """Verify PSR non-normality penalties and observation count behavior."""
    # Unbiased benchmark at 0
    assert probabilistic_sharpe_ratio(0.0, 0.0, 100) == pytest.approx(0.5, abs=1e-4)

    # Higher observations increase confidence of edge
    psr_short = probabilistic_sharpe_ratio(1.5, 0.0, 20)
    psr_long = probabilistic_sharpe_ratio(1.5, 0.0, 100)
    assert psr_long > psr_short

    # Negative skewness heavily penalizes PSR
    psr_normal = probabilistic_sharpe_ratio(1.5, 0.0, 50, skew=0.0)
    psr_neg_skew = probabilistic_sharpe_ratio(1.5, 0.0, 50, skew=-1.5)
    assert psr_neg_skew < psr_normal

    # High kurtosis (fat tail risk) reduces PSR
    psr_fat_tails = probabilistic_sharpe_ratio(1.5, 0.0, 50, kurt=8.0)
    assert psr_fat_tails < psr_normal

    # Degenerate cases
    assert probabilistic_sharpe_ratio(1.5, 0.0, 2) == 0.0
    assert probabilistic_sharpe_ratio(1.5, 0.0, 1) == 0.0


def test_deflated_sharpe_ratio_penalizes_multiple_trials():
    """Verify DSR deflates Sharpe ratio when multiple trials are evaluated."""
    sr = 1.2
    n_obs = 60

    psr_single = probabilistic_sharpe_ratio(sr, 0.0, n_obs)
    dsr_10_trials = deflated_sharpe_ratio(sr, n_obs, n_trials=10)
    dsr_100_trials = deflated_sharpe_ratio(sr, n_obs, n_trials=100)
    dsr_1000_trials = deflated_sharpe_ratio(sr, n_obs, n_trials=1000)

    # Selection bias penalty strictly monotonically reduces DSR
    assert dsr_10_trials < psr_single
    assert dsr_100_trials < dsr_10_trials
    assert dsr_1000_trials < dsr_100_trials


def test_bootstrap_expectancy_ci():
    """Verify seeded bootstrap confidence interval properties."""
    # Empty sequence
    assert bootstrap_expectancy_ci([]) == (0.0, 0.0, 0.0)

    # Constant sequence
    assert bootstrap_expectancy_ci([1.5, 1.5, 1.5]) == (1.5, 1.5, 1.5)

    # Seeded reproducibility
    trades = [1.2, -0.8, 2.0, -1.0, 0.5, 1.8, -0.4, 0.9, -1.1, 2.4]
    m1, lo1, hi1 = bootstrap_expectancy_ci(trades, n_boot=500, ci=0.90, seed=42)
    m2, lo2, hi2 = bootstrap_expectancy_ci(trades, n_boot=500, ci=0.90, seed=42)
    assert (m1, lo1, hi1) == (m2, lo2, hi2)

    # Expectancy ordering
    assert lo1 <= m1 <= hi1
    assert m1 == pytest.approx(sum(trades) / len(trades), abs=1e-4)


def test_compute_backtest_stats():
    """Verify BacktestStats calculation across equity curve and trade outcomes."""
    # Synthetic equity curve over 5 sessions
    equity_curve = [
        {"d": "2026-10-01", "equity": 500_000.0, "drawdown": 0.0},
        {"d": "2026-10-02", "equity": 505_000.0, "drawdown": 0.0},
        {"d": "2026-10-03", "equity": 502_000.0, "drawdown": 0.0059},
        {"d": "2026-10-04", "equity": 510_000.0, "drawdown": 0.0},
        {"d": "2026-10-05", "equity": 515_000.0, "drawdown": 0.0},
    ]

    outcomes = [
        {
            "rec_id": "T1",
            "symbol": "INFY",
            "pnl_rupees": 5000.0,
            "r_multiple": 1.25,
            "cost_rupees": 120.0,
            "gross_notional": 50_000.0,
            "stop_slippage_bps": 0.0,
        },
        {
            "rec_id": "T2",
            "symbol": "TCS",
            "pnl_rupees": -3000.0,
            "r_multiple": -1.0,
            "cost_rupees": 115.0,
            "gross_notional": 48_000.0,
            "stop_slippage_bps": 5.0,
        },
        {
            "rec_id": "T3",
            "symbol": "HDFCBANK",
            "pnl_rupees": 8000.0,
            "r_multiple": 2.0,
            "cost_rupees": 130.0,
            "gross_notional": 52_000.0,
            "stop_slippage_bps": 0.0,
        },
    ]

    stats = compute_backtest_stats(equity_curve, outcomes, n_trials=5, initial_equity=500_000.0)

    assert isinstance(stats, BacktestStats)
    assert stats.n_sessions == 5
    assert stats.n_trades == 3
    assert stats.n_trials == 5
    assert stats.total_return_pct == pytest.approx(0.03, abs=1e-4)
    assert stats.hit_rate == pytest.approx(2 / 3, abs=1e-4)
    assert stats.profit_factor == pytest.approx(13000.0 / 3000.0, abs=1e-4)
    assert stats.total_costs_rupees == pytest.approx(365.0, abs=1e-2)
    assert stats.max_drawdown_pct == pytest.approx(0.0059, abs=1e-4)
    assert stats.expectancy_r > 0.0
    assert isinstance(stats.positive_deflated_edge, bool)

    # Empty stats handling
    empty_stats = compute_backtest_stats([], [])
    assert empty_stats.n_sessions == 0
    assert empty_stats.n_trades == 0
    assert not empty_stats.positive_deflated_edge
