"""THE Indian equity transaction-cost model.

This module is used by BOTH the backtester and the paper broker. That shared
use is deliberate and is the single strongest guard against backtest/live
divergence: if costs are modelled in two places they will drift, and the
backtest will quietly become optimistic.

Rates below are NSE cash-segment, Zerodha-equivalent, verified Oct-2026.
They are a frozen dataclass rather than a YAML read because `ta_core` has
ZERO I/O -- the app layer loads config/costs.yaml and passes an override in.

Cost asymmetries that matter and are easy to get wrong:
  * STT is 0.1% on BOTH legs for delivery, but 0.025% on the SELL leg only
    for intraday.
  * Stamp duty is charged on the BUY leg only.
  * Intraday brokerage is per EXECUTED ORDER, so partial fills multiply it.
  * The DP charge is a FLAT rupee amount per scrip per delivery-sell day.
    It does not scale with size, so it dominates small positions: on a
    Rs 25,000 position it is 6.1 bps; on Rs 2,00,000 it is 0.8 bps.
    This is what sets a minimum sensible position size.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .enums import ProductType

__all__ = ["DEFAULT_COSTS", "CostBreakdown", "CostConfig", "compute_costs"]


def _r2(x: float) -> float:
    """Round half-up to 2 dp, the way money rounds (not banker's rounding)."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class CostConfig:
    """All rates as decimal fractions (0.001 == 0.1%).

    source: Zerodha charges page + NSE circular on transaction charges
            (NSE cash txn charge revised to 0.00297% w.e.f. 2024-10-01).
    last_updated: 2026-10-31
    """

    # --- delivery (CNC) ---
    delivery_brokerage_rate: float = 0.0
    delivery_stt_buy: float = 0.001        # 0.1 % on buy turnover
    delivery_stt_sell: float = 0.001       # 0.1 % on sell turnover
    delivery_stamp_duty_buy: float = 0.00015   # 0.015 % buy side only
    dp_charge_per_sell: float = 15.34      # FLAT, per scrip per sell day

    # --- intraday (MIS) ---
    intraday_brokerage_rate: float = 0.0003   # 0.03 %
    intraday_brokerage_cap: float = 20.0      # per executed order
    intraday_stt_sell: float = 0.00025        # 0.025 % SELL leg only
    intraday_stamp_duty_buy: float = 0.00003  # 0.003 % buy side only

    # --- common ---
    exchange_txn_rate: float = 0.0000297   # 0.00297 % on total turnover
    sebi_turnover_rate: float = 0.000001   # Rs 10 per crore
    gst_rate: float = 0.18                 # on brokerage + exch + sebi


DEFAULT_COSTS = CostConfig()


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Itemised cost of one round trip. All values in rupees."""

    brokerage: float
    stt: float
    exchange_txn: float
    sebi_fee: float
    stamp_duty: float
    gst: float
    dp_charge: float

    @property
    def total(self) -> float:
        return _r2(
            self.brokerage + self.stt + self.exchange_txn
            + self.sebi_fee + self.stamp_duty + self.gst + self.dp_charge
        )

    def bps_of(self, buy_turnover: float) -> float:
        """Total cost expressed as basis points of the position's buy value."""
        if buy_turnover <= 0:
            return 0.0
        return _r2(self.total / buy_turnover * 10_000)


def compute_costs(
    buy_turnover: float,
    sell_turnover: float,
    product: ProductType,
    *,
    n_buy_orders: int = 1,
    n_sell_orders: int = 1,
    cfg: CostConfig = DEFAULT_COSTS,
) -> CostBreakdown:
    """Full round-trip cost for one scrip.

    `n_buy_orders` / `n_sell_orders` matter only for intraday, where brokerage
    is capped PER ORDER -- scaling into a position multiplies the cap.

    A Rs 1,00,000 delivery round trip:

    >>> c = compute_costs(100_000, 100_000, ProductType.DELIVERY)
    >>> c.stt, c.stamp_duty, c.dp_charge
    (200.0, 15.0, 15.34)
    >>> c.total
    237.59
    >>> c.bps_of(100_000)
    23.76

    The same notional intraday is far cheaper -- no DP charge, STT on one leg:

    >>> i = compute_costs(100_000, 100_000, ProductType.INTRADAY)
    >>> i.brokerage, i.stt
    (40.0, 25.0)
    >>> i.total
    82.45
    >>> i.bps_of(100_000)
    8.25

    The flat DP charge is what punishes small delivery positions:

    >>> compute_costs(25_000, 25_000, ProductType.DELIVERY).bps_of(25_000)
    28.36
    >>> compute_costs(200_000, 200_000, ProductType.DELIVERY).bps_of(200_000)
    22.99

    Scaling into an intraday position multiplies the per-order brokerage cap:

    >>> compute_costs(100_000, 100_000, ProductType.INTRADAY,
    ...               n_buy_orders=3, n_sell_orders=1).brokerage
    80.0
    """
    if buy_turnover < 0 or sell_turnover < 0:
        raise ValueError("turnover must be non-negative")
    if n_buy_orders < 1 or n_sell_orders < 1:
        raise ValueError("order counts must be >= 1")

    total_turnover = buy_turnover + sell_turnover

    if product is ProductType.DELIVERY:
        brokerage = cfg.delivery_brokerage_rate * total_turnover
        stt = cfg.delivery_stt_buy * buy_turnover + cfg.delivery_stt_sell * sell_turnover
        stamp = cfg.delivery_stamp_duty_buy * buy_turnover
        dp = cfg.dp_charge_per_sell if sell_turnover > 0 else 0.0
    elif product is ProductType.INTRADAY:
        per_buy = min(cfg.intraday_brokerage_rate * buy_turnover, cfg.intraday_brokerage_cap)
        per_sell = min(cfg.intraday_brokerage_rate * sell_turnover, cfg.intraday_brokerage_cap)
        brokerage = per_buy * n_buy_orders + per_sell * n_sell_orders
        stt = cfg.intraday_stt_sell * sell_turnover
        stamp = cfg.intraday_stamp_duty_buy * buy_turnover
        dp = 0.0
    else:  # pragma: no cover - ProductType is closed
        raise ValueError(f"unknown product type: {product}")

    exch = cfg.exchange_txn_rate * total_turnover
    sebi = cfg.sebi_turnover_rate * total_turnover
    gst = cfg.gst_rate * (brokerage + exch + sebi)

    return CostBreakdown(
        brokerage=_r2(brokerage),
        stt=_r2(stt),
        exchange_txn=_r2(exch),
        sebi_fee=_r2(sebi),
        stamp_duty=_r2(stamp),
        gst=_r2(gst),
        dp_charge=_r2(dp),
    )
