"""Tests for corporate action inference and PIT-bounded price adjustments.

Enforces Invariant P-05: Every number that crosses a split/bonus uses adjusted
prices, and the adjustment itself is PIT-bounded.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
from ta_core.asof import AsOf
from ta_datafeed.adjust import apply_adjustments, infer_corporate_actions


def test_infer_1_to_2_split():
    """Prior close 1000, ex-date prev_close 500 -> 1:2 split, adj_factor 0.5."""
    bars = pl.DataFrame({
        "symbol": ["INFY", "INFY"],
        "d": [date(2026, 6, 1), date(2026, 6, 2)],
        "close": [1000.0, 510.0],
        "prev_close": [995.0, 500.0],
        "knowledge_time": [
            datetime(2026, 6, 1, 18, 0, tzinfo=UTC),
            datetime(2026, 6, 2, 18, 0, tzinfo=UTC),
        ],
    })
    ca = infer_corporate_actions(bars)
    assert ca.height == 1
    row = ca.row(0, named=True)
    assert row["symbol"] == "INFY"
    assert row["ex_date"] == date(2026, 6, 2)
    assert row["action_type"] == "SPLIT"
    assert row["ratio_from"] == 1.0
    assert row["ratio_to"] == 2.0
    assert abs(row["adj_factor"] - 0.5) < 1e-6
    assert row["dividend"] == 0.0


def test_infer_bonus_issues():
    """3:2 bonus (target 2.5) and 1:1 bonus (target 2.0)."""
    # 3:2 bonus: 2 existing shares receive 3 bonus shares -> 5 shares total.
    # Prior close = 250.0, ex-date prev_close = 100.0 -> raw_ratio = 2.5
    bars_3_2 = pl.DataFrame({
        "symbol": ["TCS", "TCS"],
        "d": [date(2026, 7, 10), date(2026, 7, 11)],
        "close": [250.0, 102.0],
        "prev_close": [248.0, 100.0],
        "knowledge_time": [
            datetime(2026, 7, 10, 18, 0, tzinfo=UTC),
            datetime(2026, 7, 11, 18, 0, tzinfo=UTC),
        ],
    })
    ca_3_2 = infer_corporate_actions(bars_3_2)
    assert ca_3_2.height == 1
    r_3_2 = ca_3_2.row(0, named=True)
    assert r_3_2["symbol"] == "TCS"
    assert r_3_2["action_type"] == "BONUS"
    assert r_3_2["ratio_from"] == 2.0
    assert r_3_2["ratio_to"] == 5.0
    assert abs(r_3_2["adj_factor"] - 0.4) < 1e-6

    # 1:1 bonus / 1:2 split halving price
    bars_1_1 = pl.DataFrame({
        "symbol": ["WIPRO", "WIPRO"],
        "d": [date(2026, 8, 1), date(2026, 8, 2)],
        "close": [400.0, 204.0],
        "prev_close": [398.0, 200.0],
        "knowledge_time": [
            datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
            datetime(2026, 8, 2, 18, 0, tzinfo=UTC),
        ],
    })
    ca_1_1 = infer_corporate_actions(bars_1_1)
    assert ca_1_1.height == 1
    r_1_1 = ca_1_1.row(0, named=True)
    assert r_1_1["symbol"] == "WIPRO"
    assert abs(r_1_1["adj_factor"] - 0.5) < 1e-6


def test_apply_adjustments_eliminates_artificial_drop():
    """Unadjusted 1-day return across ex-date is -49.0%; adjusted is +2.0%."""
    bars = pl.DataFrame({
        "symbol": ["INFY", "INFY"],
        "d": [date(2026, 6, 1), date(2026, 6, 2)],
        "open": [990.0, 505.0],
        "high": [1010.0, 515.0],
        "low": [980.0, 495.0],
        "close": [1000.0, 510.0],
        "prev_close": [995.0, 500.0],
        "volume": [1000, 2000],
        "turnover": [1_000_000.0, 1_020_000.0],
        "knowledge_time": [
            datetime(2026, 6, 1, 18, 0, tzinfo=UTC),
            datetime(2026, 6, 2, 18, 0, tzinfo=UTC),
        ],
    })

    # Unadjusted return: 510 / 1000 - 1 = -49.0%
    unadjusted_ret = bars["close"][1] / bars["close"][0] - 1.0
    assert abs(unadjusted_ret - (-0.49)) < 1e-6

    actions = infer_corporate_actions(bars)
    adj_bars = apply_adjustments(bars, actions, as_of=datetime(2026, 6, 2, 19, 0, tzinfo=UTC))

    # Pre-split close adjusted from 1000 -> 500
    assert abs(adj_bars["close"][0] - 500.0) < 1e-6
    assert abs(adj_bars["close"][1] - 510.0) < 1e-6

    # Adjusted return: 510 / 500 - 1 = +2.0%
    adjusted_ret = adj_bars["close"][1] / adj_bars["close"][0] - 1.0
    assert abs(adjusted_ret - 0.02) < 1e-6

    # Turnover is invariant to stock split
    assert adj_bars["turnover"][0] == 1_000_000.0
    # Pre-split volume scaled from 1000 -> 2000 shares
    assert adj_bars["volume"][0] == 2000


def test_pit_bound_prevents_lookahead_leakage():
    """Invariant P-05: adjustments are PIT-bounded by as_of."""
    bars = pl.DataFrame({
        "symbol": ["INFY", "INFY"],
        "d": [date(2026, 6, 1), date(2026, 6, 2)],
        "open": [990.0, 505.0],
        "high": [1010.0, 515.0],
        "low": [980.0, 495.0],
        "close": [1000.0, 510.0],
        "prev_close": [995.0, 500.0],
        "volume": [1000, 2000],
        "turnover": [1_000_000.0, 1_020_000.0],
        "knowledge_time": [
            datetime(2026, 6, 1, 18, 0, tzinfo=UTC),
            datetime(2026, 6, 2, 18, 0, tzinfo=UTC),
        ],
    })
    actions = infer_corporate_actions(bars)
    ex_kt = actions["knowledge_time"][0]

    # As of June 1 (before ex-date knowledge_time): unadjusted (close stays 1000)
    as_of_before = AsOf(datetime(2026, 6, 1, 23, 59, tzinfo=UTC))
    bars_before = apply_adjustments(bars, actions, as_of=as_of_before)
    assert bars_before["close"][0] == 1000.0

    # As of ex-date knowledge_time: adjusted (close becomes 500)
    as_of_at = AsOf(ex_kt)
    bars_at = apply_adjustments(bars, actions, as_of=as_of_at)
    assert abs(bars_at["close"][0] - 500.0) < 1e-6
