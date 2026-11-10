"""Abstract port interfaces for market data and broker adapters.

These Protocols decouple strategy and ledger logic from concrete storage engines
(ClickHouse, MongoDB) and execution targets (PaperBroker, Live Broker API).
Core remains zero-I/O by depending only on these interfaces.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from ta_core.asof import AsOf
from ta_core.dto import CorporateAction, Fill, OrderIntent

__all__ = ["BrokerPort", "MarketDataPort"]


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
