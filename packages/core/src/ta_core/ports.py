"""Abstract port interfaces for market data and broker adapters.

These Protocols decouple strategy and ledger logic from concrete storage engines
(ClickHouse, MongoDB) and execution targets (PaperBroker, Live Broker API).
Core remains zero-I/O by depending only on these interfaces.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from ta_core.asof import AsOf
from ta_core.dto import CorporateAction, Fill, OrderIntent

__all__ = [
    "BrokerPort",
    "DataSourcePort",
    "EdgeEstimatorPort",
    "FeatureProviderPort",
    "MarketDataPort",
    "ProbabilityCalibratorPort",
]


@runtime_checkable
class MarketDataPort(Protocol):
    """Abstract port for querying historical market data and corporate actions."""

    def get_bars(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        as_of: AsOf | datetime | None = None,
    ) -> Sequence[object]:
        """Fetch historical bars within [start, end] bounded by knowledge_time <= as_of."""
        ...

    def get_corporate_actions(
        self,
        symbols: Sequence[str],
        *,
        as_of: AsOf | datetime | None = None,
    ) -> Sequence[CorporateAction]:
        """Fetch corporate actions bounded by knowledge_time <= as_of."""
        ...


@runtime_checkable
class BrokerPort(Protocol):
    """Abstract port for order simulation, submission, and state queries."""

    def submit_order(self, intent: OrderIntent) -> Fill:
        """Submit an order intent for execution, returning the fill result."""
        ...

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID, returning True if cancelled."""
        ...


@runtime_checkable
class DataSourcePort(Protocol):
    """Abstract port for raw market/alternative data fetchers."""

    name: str

    def fetch(self, d: date, *, as_of: AsOf | datetime | None = None) -> object:
        """Fetch raw data for session `d` bounded by knowledge_time <= as_of."""
        ...


@runtime_checkable
class FeatureProviderPort(Protocol):
    """Abstract port for computing additional feature sets over a panel."""

    name: str

    def compute(
        self, panel: object, *, d: date | None = None, as_of: AsOf | datetime | None = None
    ) -> object:
        """Compute and append additional feature columns to `panel`."""
        ...


@runtime_checkable
class EdgeEstimatorPort(Protocol):
    """Abstract port for model-driven edge estimators (e.g. GBDT meta-labeler)."""

    def estimate_edge(self, row: Mapping[str, object], base_estimate: object) -> float:
        """Estimate incremental edge (additive probability adjustment) for a candidate row."""
        ...


@runtime_checkable
class ProbabilityCalibratorPort(Protocol):
    """Abstract port for point-in-time probability calibration (e.g. isotonic)."""

    def calibrate(
        self, p_raw: float, *, horizon: str = "SWING", regime: str = "UNKNOWN"
    ) -> float:
        """Map raw model probability to calibrated empirical win probability."""
        ...
