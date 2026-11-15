"""Domain primitives for order placement, bar observation, fills, and open positions.

Shared between the live paper broker and the historical backtester to guarantee
exact parity in execution semantics and cost attribution (Invariant P-01).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from ta_core.enums import Arm, BarrierHit, Horizon, Sleeve

__all__ = ["BarData", "FillResult", "OpenLot", "Order"]


@dataclass(frozen=True, slots=True)
class Order:
    """An executable order ready for next-session execution.

    Parameters:
      order_id: Unique identifier linking to the recommendation.
      symbol: Trading symbol (e.g. 'INFY').
      industry: Official industry classification (e.g. 'Information Technology').
      placed_d: Session date on which the order was staged (evaluates on d > placed_d).
      entry_lo: Lower bound of the acceptable entry range.
      entry_hi: Upper bound of the acceptable entry range (gaps above cancel).
      stop: Initial protective stop price.
      target: Profit taking target price.
      qty: Order quantity (whole integer shares).
      horizon: Holding period horizon (INTRADAY, SWING, POSITIONAL).
      sleeve: Strategy sleeve generating this order.
      arm: Arm generating this order (DETERMINISTIC, FROZEN_CONTROL, etc.).
      reasons: Closed-enum reason codes justifying the trade.
    """

    order_id: str
    symbol: str
    industry: str
    placed_d: date
    entry_lo: float
    entry_hi: float
    stop: float
    target: float
    qty: int
    horizon: Horizon = Horizon.SWING
    sleeve: Sleeve = Sleeve.MOMENTUM_BREAKOUT
    arm: Arm = Arm.DETERMINISTIC
    reasons: tuple[str, ...] = ()
    max_sessions: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert order to a dictionary suitable for ledger serialization."""
        out: dict[str, Any] = {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "industry": self.industry,
            "placed_d": self.placed_d.isoformat(),
            "entry_lo": self.entry_lo,
            "entry_hi": self.entry_hi,
            "stop": self.stop,
            "target": self.target,
            "qty": self.qty,
            "horizon": self.horizon.value,
            "sleeve": self.sleeve.value,
            "arm": self.arm.value,
            "reasons": list(self.reasons),
        }
        if self.max_sessions > 0:
            out["max_sessions"] = self.max_sessions
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Order:
        """Reconstruct an Order from a dictionary."""
        placed = data["placed_d"]
        placed_d = placed if isinstance(placed, date) else date.fromisoformat(str(placed))
        return cls(
            order_id=str(data["order_id"]),
            symbol=str(data["symbol"]),
            industry=str(data["industry"]),
            placed_d=placed_d,
            entry_lo=float(data["entry_lo"]),
            entry_hi=float(data["entry_hi"]),
            stop=float(data["stop"]),
            target=float(data["target"]),
            qty=int(data["qty"]),
            horizon=Horizon(str(data.get("horizon", Horizon.SWING))),
            sleeve=Sleeve(str(data.get("sleeve", Sleeve.MOMENTUM_BREAKOUT))),
            arm=Arm(str(data.get("arm", Arm.DETERMINISTIC))),
            reasons=tuple(str(r) for r in data.get("reasons", ())),
            max_sessions=int(data.get("max_sessions", 0)),
        )


@dataclass(frozen=True, slots=True)
class BarData:
    """One session's OHLC market observation for a single symbol."""

    symbol: str
    d: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class FillResult:
    """Outcome of evaluating an order entry or an open lot exit against a bar."""

    filled: bool
    fill_price: float = 0.0
    qty: int = 0
    gross_notional: float = 0.0
    cost_rupees: float = 0.0
    net_notional: float = 0.0
    barrier_hit: BarrierHit = BarrierHit.OPEN
    stop_slippage_bps: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class OpenLot:
    """An active held position resulting from an executed entry order.

    Tracks execution price, costs, resting stop, time-in-trade, and extremes.
    """

    order_id: str
    symbol: str
    industry: str
    qty: int
    entry_date: date
    entry_price: float
    entry_cost: float
    stop: float
    initial_stop: float
    target: float
    last_price: float
    sessions_held: int = 0
    horizon: Horizon = Horizon.SWING
    sleeve: Sleeve = Sleeve.MOMENTUM_BREAKOUT
    arm: Arm = Arm.DETERMINISTIC
    reasons: tuple[str, ...] = ()
    min_low_seen: float = 0.0
    max_high_seen: float = 0.0
    missing_bars: int = 0
    max_sessions: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert lot to a dictionary suitable for ledger serialization."""
        out: dict[str, Any] = {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "industry": self.industry,
            "qty": self.qty,
            "entry_date": self.entry_date.isoformat(),
            "entry_price": self.entry_price,
            "entry_cost": self.entry_cost,
            "stop": self.stop,
            "initial_stop": self.initial_stop,
            "target": self.target,
            "last_price": self.last_price,
            "sessions_held": self.sessions_held,
            "horizon": self.horizon.value,
            "sleeve": self.sleeve.value,
            "arm": self.arm.value,
            "reasons": list(self.reasons),
            "min_low_seen": self.min_low_seen,
            "max_high_seen": self.max_high_seen,
            "missing_bars": self.missing_bars,
        }
        if self.max_sessions > 0:
            out["max_sessions"] = self.max_sessions
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OpenLot:
        """Reconstruct an OpenLot from a dictionary."""
        e_date = data["entry_date"]
        entry_d = e_date if isinstance(e_date, date) else date.fromisoformat(str(e_date))
        return cls(
            order_id=str(data["order_id"]),
            symbol=str(data["symbol"]),
            industry=str(data["industry"]),
            qty=int(data["qty"]),
            entry_date=entry_d,
            entry_price=float(data["entry_price"]),
            entry_cost=float(data["entry_cost"]),
            stop=float(data["stop"]),
            initial_stop=float(data.get("initial_stop", data["stop"])),
            target=float(data["target"]),
            last_price=float(data["last_price"]),
            sessions_held=int(data.get("sessions_held", 0)),
            horizon=Horizon(str(data.get("horizon", Horizon.SWING))),
            sleeve=Sleeve(str(data.get("sleeve", Sleeve.MOMENTUM_BREAKOUT))),
            arm=Arm(str(data.get("arm", Arm.DETERMINISTIC))),
            reasons=tuple(str(r) for r in data.get("reasons", ())),
            min_low_seen=float(data.get("min_low_seen", 0.0)),
            max_high_seen=float(data.get("max_high_seen", 0.0)),
            missing_bars=int(data.get("missing_bars", 0)),
            max_sessions=int(data.get("max_sessions", 0)),
        )
