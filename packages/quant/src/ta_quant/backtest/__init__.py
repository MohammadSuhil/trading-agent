"""Backtest package re-exporting core simulation primitives and engine.

Enforces Invariant P-01:
  Paper broker and backtester call the SAME simulate_fill() / apply_costs() objects
  (identity, not copies).
"""
from __future__ import annotations

from ta_ledger.broker.paper import apply_costs, simulate_fill

from .engine import (
    BacktestResult,
    _screen_session,
    orders_from_recommendations,
    run_backtest,
)

__all__ = [
    "BacktestResult",
    "_screen_session",
    "apply_costs",
    "orders_from_recommendations",
    "run_backtest",
    "simulate_fill",
]
