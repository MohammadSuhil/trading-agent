"""PaperBroker and deterministic fill simulation.

Invariants:
  P-01: Paper broker and backtester call the SAME simulate_fill() / apply_costs()
        objects (identity, not copies).
  I-14: Same-bar ambiguity resolves pessimistically (DOWN / stop first). When both
        stop and target are touched in the same bar, stop triggers unconditionally.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from ta_core.costs import DEFAULT_COSTS, CostBreakdown, CostConfig, compute_costs
from ta_core.enums import HORIZON_SESSIONS, Arm, BarrierHit, ProductType

from ..events import GENESIS_HASH, EventKind, LedgerEvent
from ..manifest import RunManifest
from ..risk import Portfolio, Position
from .base import BarData, FillResult, OpenLot, Order

__all__ = ["PaperBroker", "apply_costs", "simulate_fill"]


def apply_costs(
    buy_turnover: float,
    sell_turnover: float,
    product: ProductType = ProductType.DELIVERY,
    *,
    cfg: CostConfig = DEFAULT_COSTS,
) -> CostBreakdown:
    """Transaction cost computation via ta_core.costs.compute_costs.

    Both paper broker and backtester call this single entrypoint (Invariant P-01).

    >>> round(apply_costs(100_000, 100_000).total, 2) == 237.59
    True
    >>> c_tot = apply_costs(2000, 2000)
    >>> c_buy = apply_costs(2000, 0)
    >>> c_sell = apply_costs(0, 2000)
    >>> abs(c_tot.total - (c_buy.total + c_sell.total)) < 1e-9
    True
    """
    return compute_costs(buy_turnover, sell_turnover, product, cfg=cfg)


def simulate_fill(
    item: Order | OpenLot,
    bar: BarData,
    *,
    product: ProductType = ProductType.DELIVERY,
    cfg: CostConfig = DEFAULT_COSTS,
) -> FillResult:
    """Simulate order entry or lot exit against a daily bar.

    Pure function, zero I/O. Used identically by backtest and paper execution.

    Enforces Invariant I-14:
      Same-bar ambiguity resolves DOWN (stop first). If both stop and target
      are touched in the same bar, the stop exits the trade at the stop price
      with reason 'SAME_BAR_STOP_FIRST'.
    """
    if isinstance(item, Order):
        if item.qty <= 0:
            return FillResult(filled=False, barrier_hit=BarrierHit.CANCELLED, reason="INVALID_QTY")
        if bar.d <= item.placed_d:
            return FillResult(filled=False, barrier_hit=BarrierHit.OPEN, reason="SAME_SESSION")

        if bar.open > item.entry_hi:
            return FillResult(
                filled=False, barrier_hit=BarrierHit.CANCELLED, reason="GAP_ABOVE_ENTRY_HI"
            )
        if bar.open <= item.stop:
            return FillResult(
                filled=False, barrier_hit=BarrierHit.CANCELLED, reason="GAP_BELOW_STOP"
            )
        if item.entry_lo > 0 and bar.open < item.entry_lo:
            return FillResult(
                filled=False, barrier_hit=BarrierHit.CANCELLED, reason="GAP_BELOW_ENTRY_LO"
            )

        fill_price = bar.open
        gross = item.qty * fill_price
        entry_cost = compute_costs(gross, 0.0, product, cfg=cfg).total
        net = gross + entry_cost
        return FillResult(
            filled=True,
            fill_price=fill_price,
            qty=item.qty,
            gross_notional=gross,
            cost_rupees=entry_cost,
            net_notional=net,
            barrier_hit=BarrierHit.OPEN,
            reason="ENTRY_OPEN",
        )

    if isinstance(item, OpenLot):
        # 1. Gap-through-stop
        if bar.open <= item.stop:
            fill_price = bar.open
            slippage_bps = (
                max(0.0, (item.stop - bar.open) / item.stop * 10_000.0) if item.stop > 0 else 0.0
            )
            gross = item.qty * fill_price
            exit_cost = compute_costs(0.0, gross, product, cfg=cfg).total
            net = gross - exit_cost
            return FillResult(
                filled=True,
                fill_price=fill_price,
                qty=item.qty,
                gross_notional=gross,
                cost_rupees=exit_cost,
                net_notional=net,
                barrier_hit=BarrierHit.DOWN,
                stop_slippage_bps=round(slippage_bps, 2),
                reason="GAP_STOP",
            )

        # 2. Intraday stop hit (Invariant I-14: stop beats target on same bar)
        if bar.low <= item.stop:
            fill_price = item.stop
            reason = "SAME_BAR_STOP_FIRST" if bar.high >= item.target else "STOP_HIT"
            gross = item.qty * fill_price
            exit_cost = compute_costs(0.0, gross, product, cfg=cfg).total
            net = gross - exit_cost
            return FillResult(
                filled=True,
                fill_price=fill_price,
                qty=item.qty,
                gross_notional=gross,
                cost_rupees=exit_cost,
                net_notional=net,
                barrier_hit=BarrierHit.DOWN,
                stop_slippage_bps=0.0,
                reason=reason,
            )

        # 3. Gap-above-target
        if bar.open >= item.target:
            fill_price = bar.open
            gross = item.qty * fill_price
            exit_cost = compute_costs(0.0, gross, product, cfg=cfg).total
            net = gross - exit_cost
            return FillResult(
                filled=True,
                fill_price=fill_price,
                qty=item.qty,
                gross_notional=gross,
                cost_rupees=exit_cost,
                net_notional=net,
                barrier_hit=BarrierHit.UP,
                stop_slippage_bps=0.0,
                reason="GAP_TARGET",
            )

        # 4. Intraday target hit
        if bar.high >= item.target:
            fill_price = item.target
            gross = item.qty * fill_price
            exit_cost = compute_costs(0.0, gross, product, cfg=cfg).total
            net = gross - exit_cost
            return FillResult(
                filled=True,
                fill_price=fill_price,
                qty=item.qty,
                gross_notional=gross,
                cost_rupees=exit_cost,
                net_notional=net,
                barrier_hit=BarrierHit.UP,
                stop_slippage_bps=0.0,
                reason="TARGET_HIT",
            )

        # 5. Vertical time-stop
        max_sessions = item.max_sessions if item.max_sessions > 0 else HORIZON_SESSIONS.get(item.horizon, 10)
        if item.sessions_held + 1 >= max_sessions:
            fill_price = bar.close
            gross = item.qty * fill_price
            exit_cost = compute_costs(0.0, gross, product, cfg=cfg).total
            net = gross - exit_cost
            return FillResult(
                filled=True,
                fill_price=fill_price,
                qty=item.qty,
                gross_notional=gross,
                cost_rupees=exit_cost,
                net_notional=net,
                barrier_hit=BarrierHit.VERTICAL,
                stop_slippage_bps=0.0,
                reason="TIME_STOP",
            )

        # 6. Otherwise: no exit
        return FillResult(filled=False, barrier_hit=BarrierHit.OPEN)

    raise TypeError(f"item must be Order or OpenLot, got {type(item).__name__}")


class PaperBroker:
    """Session-advancing paper trading broker backed by a hash-chained event ledger."""

    def __init__(
        self,
        *,
        initial_equity: float = 500_000.0,
        arm: Arm | str = Arm.DETERMINISTIC,
        product: ProductType = ProductType.DELIVERY,
        cost_cfg: CostConfig = DEFAULT_COSTS,
    ) -> None:
        self.initial_equity = float(initial_equity)
        self.cash = float(initial_equity)
        self.watermark = float(initial_equity)
        self.arm = Arm(arm) if isinstance(arm, str) else arm
        self.product = product
        self.cost_cfg = cost_cfg

        self.open_lots: dict[str, OpenLot] = {}
        self.pending_orders: dict[str, Order] = {}
        self.pending_exits: dict[str, str] = {}
        self.events: list[LedgerEvent] = []
        self.equity_curve: list[dict[str, Any]] = []
        self.outcomes: list[dict[str, Any]] = []
        self.manifests: list[RunManifest] = []

    def _append_event(
        self,
        *,
        ts: datetime,
        run_id: str,
        kind: EventKind,
        payload: Mapping[str, Any],
    ) -> LedgerEvent:
        seq = len(self.events) + 1
        prev_hash = self.events[-1].hash if self.events else GENESIS_HASH
        event = LedgerEvent.create(
            seq=seq,
            ts=ts,
            run_id=run_id,
            kind=kind,
            payload=payload,
            prev_hash=prev_hash,
        )
        self.events.append(event)
        return event

    def record_manifest(self, manifest: RunManifest) -> LedgerEvent:
        """Record the genesis or execution manifest into the event chain."""
        seq = len(self.events) + 1
        prev_hash = self.events[-1].hash if self.events else GENESIS_HASH
        event = manifest.to_event(seq=seq, prev_hash=prev_hash)
        self.events.append(event)
        self.manifests.append(manifest)
        return event

    def place_orders(
        self,
        orders: Sequence[Order],
        *,
        run_id: str,
        ts: datetime,
    ) -> list[LedgerEvent]:
        """Stage pending orders for execution in subsequent sessions."""
        events: list[LedgerEvent] = []
        for order in orders:
            self.pending_orders[order.order_id] = order
            ev = self._append_event(
                ts=ts,
                run_id=run_id,
                kind=EventKind.ORDER_PLACED,
                payload=order.to_dict(),
            )
            events.append(ev)
        return events

    def trail_stop(
        self,
        order_id: str,
        new_stop: float,
        *,
        run_id: str,
        ts: datetime,
    ) -> LedgerEvent:
        """Trail the protective stop of an open position upwards."""
        if order_id not in self.open_lots:
            raise KeyError(f"No open lot found for order_id {order_id}")
        lot = self.open_lots[order_id]
        if new_stop <= lot.stop:
            raise ValueError(
                f"Trailed stop ({new_stop}) must be strictly higher than current stop ({lot.stop})"
            )
        prev_stop = lot.stop
        self.open_lots[order_id] = replace(lot, stop=new_stop)
        return self._append_event(
            ts=ts,
            run_id=run_id,
            kind=EventKind.STOP_TRAILED,
            payload={
                "order_id": order_id,
                "symbol": lot.symbol,
                "prev_stop": prev_stop,
                "new_stop": new_stop,
            },
        )

    def schedule_exit(self, order_id: str, *, reason: str = "SIGNAL_EXIT") -> None:
        """Schedule an exit for an open position."""
        if order_id in self.open_lots:
            self.pending_exits[order_id] = reason

    def step_session(
        self,
        d: date,
        bars_by_symbol: Mapping[str, BarData],
        *,
        run_id: str,
        ts: datetime,
    ) -> list[LedgerEvent]:
        """Advance one trading session: evaluate entries, exits, lot marks, and equity."""
        emitted: list[LedgerEvent] = []

        # 1. Process pending orders from prior sessions
        for order_id, order in list(self.pending_orders.items()):
            if order.placed_d >= d:
                continue
            if any(lot.symbol == order.symbol for lot in self.open_lots.values()):
                del self.pending_orders[order_id]
                ev = self._append_event(
                    ts=ts,
                    run_id=run_id,
                    kind=EventKind.ORDER_EXPIRED,
                    payload={
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "d": d.isoformat(),
                        "reason": "ALREADY_OPEN",
                    },
                )
                emitted.append(ev)
                continue
            bar = bars_by_symbol.get(order.symbol)
            if not bar:
                continue

            fill = simulate_fill(order, bar, product=self.product, cfg=self.cost_cfg)
            if fill.filled:
                del self.pending_orders[order_id]
                self.cash -= fill.net_notional
                lot = OpenLot(
                    order_id=order.order_id,
                    symbol=order.symbol,
                    industry=order.industry,
                    qty=fill.qty,
                    entry_date=d,
                    entry_price=fill.fill_price,
                    entry_cost=fill.cost_rupees,
                    stop=order.stop,
                    initial_stop=order.stop,
                    target=order.target,
                    last_price=fill.fill_price,
                    sessions_held=0,
                    horizon=order.horizon,
                    sleeve=order.sleeve,
                    arm=order.arm,
                    reasons=order.reasons,
                    min_low_seen=bar.low,
                    max_high_seen=bar.high,
                    max_sessions=order.max_sessions,
                )
                self.open_lots[order_id] = lot
                ev = self._append_event(
                    ts=ts,
                    run_id=run_id,
                    kind=EventKind.ORDER_FILLED,
                    payload={
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "industry": order.industry,
                        "fill_d": d.isoformat(),
                        "fill_price": fill.fill_price,
                        "qty": fill.qty,
                        "gross_notional": fill.gross_notional,
                        "cost_rupees": fill.cost_rupees,
                        "net_notional": fill.net_notional,
                        "stop": order.stop,
                        "target": order.target,
                        "horizon": order.horizon.value,
                        "sleeve": order.sleeve.value,
                        "arm": order.arm.value,
                        "reasons": list(order.reasons),
                    },
                )
                emitted.append(ev)
            elif fill.barrier_hit == BarrierHit.CANCELLED:
                del self.pending_orders[order_id]
                ev = self._append_event(
                    ts=ts,
                    run_id=run_id,
                    kind=EventKind.ORDER_EXPIRED,
                    payload={
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "d": d.isoformat(),
                        "reason": fill.reason,
                    },
                )
                emitted.append(ev)

        # 2. Evaluate exits on open lots held into today (entry_date < d)
        for order_id, lot in list(self.open_lots.items()):
            if lot.entry_date >= d:
                continue
            bar = bars_by_symbol.get(lot.symbol)
            if not bar:
                # Handle missing bar (delisting, rename, suspension)
                new_missing = lot.missing_bars + 1
                if new_missing >= 5:
                    # Liquidate zombie lot at last_price
                    fill_price = lot.last_price
                    gross = lot.qty * fill_price
                    exit_cost = compute_costs(0.0, gross, self.product, cfg=self.cost_cfg).total
                    net = gross - exit_cost
                    del self.open_lots[order_id]
                    self.pending_exits.pop(order_id, None)
                    self.cash += net

                    pnl_rupees = round(net - (lot.qty * lot.entry_price + lot.entry_cost), 2)
                    risk_unit = lot.entry_price - lot.initial_stop
                    r_multiple = (
                        round((fill_price - lot.entry_price) / risk_unit, 4)
                        if risk_unit > 0
                        else 0.0
                    )
                    holding_days = (d - lot.entry_date).days

                    outcome_dict = {
                        "rec_id": lot.order_id,
                        "symbol": lot.symbol,
                        "arm": lot.arm.value,
                        "horizon": lot.horizon.value,
                        "entry_date": lot.entry_date.isoformat(),
                        "entry_price": lot.entry_price,
                        "exit_date": d.isoformat(),
                        "exit_price": fill_price,
                        "barrier_hit": BarrierHit.VERTICAL.value,
                        "r_multiple": r_multiple,
                        "pnl_rupees": pnl_rupees,
                        "mae_r": 0.0,
                        "mfe_r": 0.0,
                        "holding_days": holding_days,
                    }
                    self.outcomes.append(outcome_dict)

                    ev = self._append_event(
                        ts=ts,
                        run_id=run_id,
                        kind=EventKind.POSITION_CLOSED,
                        payload={
                            **outcome_dict,
                            "qty": lot.qty,
                            "gross_notional": gross,
                            "cost_rupees": exit_cost,
                            "net_notional": net,
                            "stop_slippage_bps": 0.0,
                            "reason": "DELISTED_OR_RENAMED",
                        },
                    )
                    emitted.append(ev)
                else:
                    # Update missing_bars and continue tracking
                    self.open_lots[order_id] = replace(lot, missing_bars=new_missing)
                continue

            # Bar IS present: check for pending exits first
            if order_id in self.pending_exits:
                sched_reason = self.pending_exits.pop(order_id)
                fill_price = bar.open
                gross = lot.qty * fill_price
                exit_cost = compute_costs(0.0, gross, self.product, cfg=self.cost_cfg).total
                net = gross - exit_cost
                del self.open_lots[order_id]
                self.cash += net

                pnl_rupees = round(net - (lot.qty * lot.entry_price + lot.entry_cost), 2)
                risk_unit = lot.entry_price - lot.initial_stop
                r_multiple = (
                    round((fill_price - lot.entry_price) / risk_unit, 4)
                    if risk_unit > 0
                    else 0.0
                )
                holding_days = (d - lot.entry_date).days

                outcome_dict = {
                    "rec_id": lot.order_id,
                    "symbol": lot.symbol,
                    "arm": lot.arm.value,
                    "horizon": lot.horizon.value,
                    "entry_date": lot.entry_date.isoformat(),
                    "entry_price": lot.entry_price,
                    "exit_date": d.isoformat(),
                    "exit_price": fill_price,
                    "barrier_hit": BarrierHit.VERTICAL.value,
                    "r_multiple": r_multiple,
                    "pnl_rupees": pnl_rupees,
                    "mae_r": 0.0,
                    "mfe_r": 0.0,
                    "holding_days": holding_days,
                }
                self.outcomes.append(outcome_dict)

                ev = self._append_event(
                    ts=ts,
                    run_id=run_id,
                    kind=EventKind.POSITION_CLOSED,
                    payload={
                        **outcome_dict,
                        "qty": lot.qty,
                        "gross_notional": gross,
                        "cost_rupees": exit_cost,
                        "net_notional": net,
                        "stop_slippage_bps": 0.0,
                        "reason": sched_reason,
                    },
                )
                emitted.append(ev)
            else:
                # Normal exit evaluation
                exit_fill = simulate_fill(lot, bar, product=self.product, cfg=self.cost_cfg)
                if exit_fill.filled:
                    del self.open_lots[order_id]
                    self.cash += exit_fill.net_notional

                    pnl_rupees = round(exit_fill.net_notional - (lot.qty * lot.entry_price + lot.entry_cost), 2)
                    risk_unit = lot.entry_price - lot.initial_stop
                    r_multiple = (
                        round((exit_fill.fill_price - lot.entry_price) / risk_unit, 4)
                        if risk_unit > 0
                        else 0.0
                    )
                    holding_days = (d - lot.entry_date).days
                    min_l = min(lot.min_low_seen, bar.low)
                    max_h = max(lot.max_high_seen, bar.high)
                    mae_r = round((min_l - lot.entry_price) / risk_unit, 4) if risk_unit > 0 else 0.0
                    mfe_r = round((max_h - lot.entry_price) / risk_unit, 4) if risk_unit > 0 else 0.0

                    outcome_dict = {
                        "rec_id": lot.order_id,
                        "symbol": lot.symbol,
                        "arm": lot.arm.value,
                        "horizon": lot.horizon.value,
                        "entry_date": lot.entry_date.isoformat(),
                        "entry_price": lot.entry_price,
                        "exit_date": d.isoformat(),
                        "exit_price": exit_fill.fill_price,
                        "barrier_hit": exit_fill.barrier_hit.value,
                        "r_multiple": r_multiple,
                        "pnl_rupees": pnl_rupees,
                        "mae_r": mae_r,
                        "mfe_r": mfe_r,
                        "holding_days": holding_days,
                    }
                    self.outcomes.append(outcome_dict)

                    ev = self._append_event(
                        ts=ts,
                        run_id=run_id,
                        kind=EventKind.POSITION_CLOSED,
                        payload={
                            **outcome_dict,
                            "qty": exit_fill.qty,
                            "gross_notional": exit_fill.gross_notional,
                            "cost_rupees": exit_fill.cost_rupees,
                            "net_notional": exit_fill.net_notional,
                            "stop_slippage_bps": exit_fill.stop_slippage_bps,
                            "reason": exit_fill.reason,
                        },
                    )
                    emitted.append(ev)
                else:
                    # No fill: reset missing_bars and update lot
                    self.open_lots[order_id] = replace(
                        lot,
                        missing_bars=0,
                        last_price=bar.close,
                        sessions_held=lot.sessions_held + 1,
                        min_low_seen=min(lot.min_low_seen, bar.low),
                        max_high_seen=max(lot.max_high_seen, bar.high),
                    )

        # 3. Update lots opened today at open with session close statistics
        for order_id, lot in list(self.open_lots.items()):
            if lot.entry_date == d:
                bar = bars_by_symbol.get(lot.symbol)
                if bar:
                    self.open_lots[order_id] = replace(
                        lot,
                        last_price=bar.close,
                        sessions_held=1,
                        min_low_seen=min(lot.min_low_seen, bar.low),
                        max_high_seen=max(lot.max_high_seen, bar.high),
                    )

        # 4. Mark daily equity and risk metrics
        deployed = round(sum(lot.qty * lot.last_price for lot in self.open_lots.values()), 2)
        equity = round(self.cash + deployed, 2)
        self.watermark = max(self.watermark, equity)
        drawdown = round((self.watermark - equity) / self.watermark, 4) if self.watermark > 0 else 0.0
        heat = sum(max(0.0, (lot.last_price - lot.stop) * lot.qty) for lot in self.open_lots.values())
        heat_pct = round(heat / equity, 4) if equity > 0 else 0.0
        arm_str = self.arm.value

        equity_record = {
            "d": d.isoformat(),
            "arm": arm_str,
            "equity": equity,
            "cash": round(self.cash, 2),
            "deployed": deployed,
            "heat_pct": heat_pct,
            "drawdown": drawdown,
            "watermark": round(self.watermark, 2),
        }
        self.equity_curve.append(equity_record)

        ev_equity = self._append_event(
            ts=ts,
            run_id=run_id,
            kind=EventKind.EQUITY_MARKED,
            payload={
                **equity_record,
                "open_lots": [lot.to_dict() for lot in self.open_lots.values()],
            },
        )
        emitted.append(ev_equity)
        return emitted

    def portfolio(self) -> Portfolio:
        """Construct the live Portfolio snapshot from current lots and cash."""
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
            for lot in self.open_lots.values()
        )
        deployed = round(sum(p.notional for p in positions), 2)
        equity = round(self.cash + deployed, 2)
        return Portfolio(
            equity=equity,
            cash=round(self.cash, 2),
            positions=positions,
            watermark=round(self.watermark, 2),
        )
