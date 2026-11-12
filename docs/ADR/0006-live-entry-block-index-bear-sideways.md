# ADR-0006 — Live rules block new buys while the Nifty 500 index regime is bear or sideways

- **Status:** Accepted
- **Date:** 2026-11-12
- **Supersedes:** the "rejected" verdict of [ADR-0004](0004-index-regime-filter-rejected.md)
- **Applies to:** the live (DETERMINISTIC) arm only. The frozen control never takes a block.

## Why ADR-0004 was revisited

ADR-0004 rejected an index bear block because it lost to the control. Two things changed:

1. The backtest engine was brought into line with the live pipeline (commit `dbde9b7`: skip held
   symbols, apply the RISK-08 pair-correlation gate). The control's behaviour, and so every
   comparison against it, changed.
2. The first REG-01 also *replaced* the strategy's breadth regime with the index regime for
   position/heat caps, so it changed two things at once. Blocks are now a separate
   `regime.EntryBlock` that only vetoes new entries; caps always follow the breadth regime.

The owner also asked why buys are not blocked in sideways as well as down markets.

## Pre-registered contest

Fixed before any result was seen: four candidate blocks against the control (V0), two periods,
five start dates each (shifted 0/5/10/15/20 sessions), because one backtest's return moved by
8 points when its start moved by a day. Adopt only if, in **both** periods, the median return
beats V0, it beats V0 from ≥ 4 of 5 starts, and its median drawdown is no worse.

| Variant (no new buys while…) | OOS 2021-02 → 2025-07 median | IS 2025-07 → 2026-11 | Trades OOS / IS | Verdict |
| --- | --- | --- | --- | --- |
| V0 — nothing extra (control) | −2.40% (max DD −19.0%) | −12.60% (−14.4%) | 563 / 112 | baseline |
| V1 — index bear | +6.34% (−16.7%) | −9.51% (−9.7%) | 538 / 59 | pass |
| **V2 — index bear or sideways** | **+7.08% (−16.8%)** | **−0.85% (−1.9%)** | 478 / 16 | **pass — best OOS** |
| V3 — breadth down | +2.07% (−15.6%) | −2.74% (−8.3%) | 538 / 93 | pass |
| V4 — breadth down or sideways | −21.39% (−27.5%) | +3.54% (−4.9%) | 308 / 53 | fail |
| *Nifty 500 (price)* | *+89.91%* | *+3.36%* | | |

## Decision

`nightly.LIVE_ENTRY_BLOCK = regime.INDEX_BEAR_SIDEWAYS` (V2). Revert by setting it to `None`.

## Caveats that stay attached to this decision

- **The in-sample starts collapsed.** The strategy made no trades in the first 20 sessions after
  2025-07-23, so all five in-sample starts gave identical paths — "5/5" there is one comparison,
  not five. V2's out-of-sample starts collapsed the same way.
- **V2 wins largely by trading less** (16 trades in 16 months in sample). Losing less is not an
  edge.
- **Every variant trails the Nifty 500** by 80+ points out of sample (ADR-0005). By M3's gate the
  screener itself is the problem; this block only reduces the damage.
- **Four candidates were tried**; the best of four looks better than it is.
- Breadth "sideways" (V4) is the wrong signal: it fires in good markets too and threw away the
  profitable trades (−21% OOS, every start lost).

## Consequences

- On 2026-11-12 the index regime is sideways (since 2026-11-06), so the live arm opens **no new
  positions** until it turns bull or high-volatility. Cards that would have been BUYs show as WATCH
  with an `ENTRY-BLOCK` reason. Exits are unaffected.
- The Calibration page's comparison line is now "Live rules" (control + V2).
- Each stored backtest curve carries both regimes per day: breadth (sets caps) and index (drives
  the block).
