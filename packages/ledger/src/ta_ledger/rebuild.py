"""Deterministic ledger projection and state reconstruction.

Invariant P-03: The ledger is hash-chained. rebuild_from_ledger() executes verify_chain()
before replaying, ensuring that no state is ever projected from a corrupted, gapped,
or tampered event stream.

All downstream analytics (portfolio, positions, equity curve, outcomes, provenance)
derive purely from replaying ev.ledger_events from genesis.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from ta_core.enums import Arm, Horizon, Sleeve

from .broker.base import OpenLot, Order
from .events import EventKind, LedgerEvent, verify_chain
from .manifest import RunManifest
from .risk import Portfolio, Position

__all__ = ["LedgerProjection", "rebuild_from_ledger"]


@dataclass(frozen=True, slots=True)
class LedgerProjection:
    """Projected state reconstructed deterministically from a ledger event sequence."""

    portfolio: Portfolio
    open_lots: tuple[OpenLot, ...]
    pending_orders: tuple[Order, ...]
    equity_curve: list[dict[str, Any]]
    outcomes: list[dict[str, Any]]
    manifests: list[RunManifest]


def rebuild_from_ledger(
    events: Sequence[LedgerEvent],
    *,
    initial_equity: float = 500_000.0,
    arm: str = "DETERMINISTIC",
) -> LedgerProjection:
    """Rebuild the complete ledger state from an event sequence.

    First verifies hash chain integrity and sequence contiguity (Invariant P-03).
    Replays every event deterministically from genesis to reproduce portfolios,
    open lots, daily equity marks, and resolved outcomes.
    """
    verify_chain(events)

    cash = float(initial_equity)
    watermark = float(initial_equity)
    open_lots: dict[str, OpenLot] = {}
    pending_orders: dict[str, Order] = {}
    equity_curve: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    manifests: list[RunManifest] = []

    for event in events:
        p = event.payload

        if event.kind == EventKind.RUN_MANIFEST:
            manifest = RunManifest.from_dict(p)
            manifests.append(manifest)

        elif event.kind == EventKind.ORDER_PLACED:
            order = Order.from_dict(p)
            pending_orders[order.order_id] = order

        elif event.kind == EventKind.ORDER_FILLED:
            order_id = str(p["order_id"])
            pending_orders.pop(order_id, None)
            cash -= float(p["net_notional"])
            fill_d = p["fill_d"]
            entry_d = fill_d if isinstance(fill_d, date) else date.fromisoformat(str(fill_d))
            lot = OpenLot(
                order_id=order_id,
                symbol=str(p["symbol"]),
                industry=str(p["industry"]),
                qty=int(p["qty"]),
                entry_date=entry_d,
                entry_price=float(p["fill_price"]),
                entry_cost=float(p["cost_rupees"]),
                stop=float(p["stop"]),
                initial_stop=float(p.get("initial_stop", p["stop"])),
                target=float(p["target"]),
                last_price=float(p["fill_price"]),
                sessions_held=int(p.get("sessions_held", 0)),
                horizon=Horizon(str(p.get("horizon", Horizon.SWING))),
                sleeve=Sleeve(str(p.get("sleeve", Sleeve.MOMENTUM_BREAKOUT))),
                arm=Arm(str(p.get("arm", arm))),
                reasons=tuple(str(r) for r in p.get("reasons", ())),
                min_low_seen=float(p.get("min_low_seen", p["fill_price"])),
                max_high_seen=float(p.get("max_high_seen", p["fill_price"])),
            )
            open_lots[order_id] = lot

        elif event.kind == EventKind.ORDER_EXPIRED:
            order_id = str(p["order_id"])
            pending_orders.pop(order_id, None)

        elif event.kind == EventKind.STOP_TRAILED:
            order_id = str(p["order_id"])
            new_stop = float(p["new_stop"])
            if order_id in open_lots:
                open_lots[order_id] = replace(open_lots[order_id], stop=new_stop)

        elif event.kind == EventKind.POSITION_CLOSED:
            order_id = str(p.get("order_id") or p.get("rec_id"))
            open_lots.pop(order_id, None)
            cash += float(p["net_notional"])
            outcome_dict = {
                "rec_id": order_id,
                "symbol": str(p["symbol"]),
                "arm": str(p["arm"]),
                "horizon": str(p["horizon"]),
                "entry_date": str(p["entry_date"]),
                "entry_price": float(p["entry_price"]),
                "exit_date": str(p["exit_date"]),
                "exit_price": float(p["exit_price"]),
                "barrier_hit": str(p["barrier_hit"]),
                "r_multiple": float(p["r_multiple"]),
                "pnl_rupees": float(p["pnl_rupees"]),
                "mae_r": float(p["mae_r"]),
                "mfe_r": float(p["mfe_r"]),
                "holding_days": int(p["holding_days"]),
            }
            outcomes.append(outcome_dict)

        elif event.kind == EventKind.EQUITY_MARKED:
            cash = float(p["cash"])
            watermark = float(p["watermark"])
            equity_record = {k: v for k, v in p.items() if k != "open_lots"}
            equity_curve.append(equity_record)
            if "open_lots" in p:
                open_lots = {
                    lot_d["order_id"]: OpenLot.from_dict(lot_d)
                    for lot_d in p["open_lots"]
                }

    positions = tuple(
        Position(
            symbol=lot.symbol,
            industry=lot.industry,
            qty=lot.qty,
            entry=lot.entry_price,
            last=lot.last_price,
            stop=lot.stop,
            opened=lot.entry_date,
            target=lot.target,
            horizon=lot.horizon.value,
        )
        for lot in open_lots.values()
    )
    deployed = round(sum(p.notional for p in positions), 2)
    equity = round(cash + deployed, 2)
    portfolio = Portfolio(
        equity=equity,
        cash=round(cash, 2),
        positions=positions,
        watermark=round(watermark, 2),
    )

    return LedgerProjection(
        portfolio=portfolio,
        open_lots=tuple(open_lots.values()),
        pending_orders=tuple(pending_orders.values()),
        equity_curve=equity_curve,
        outcomes=outcomes,
        manifests=manifests,
    )
