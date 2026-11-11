# ADR-0004 — Index regime filter and "empirical calibration" spec: adopted in part, bear block rejected

- **Status:** Accepted
- **Date:** 2026-11-11
- **Evaluates:** "Technical Specification: Deterministic Regime Filter & Empirical Calibration
  Engine" (user-supplied, 2026-11-11)
- **Evidence:** pre-registered out-of-sample backtest on 2019–2026 history (below)

## Context

The frozen-control backtest lost money from March 2026, mostly on entries taken while the market
was falling (TREND_DOWN entries: 25 trades, 20% win rate, −₹53k). The spec proposed (a) a
hard-coded index regime with a hard block on new longs in a bear market, and (b) a
Laplace-smoothed "empirical calibration" to replace "Calibration not established".

The system's regime gate at the time was a breadth heuristic (`pipeline.infer_regime`: % of
Nifty 500 above its 50-DMA, cross-sectional dispersion). It scales position caps but never
blocks entries; win probabilities are not regime-conditioned.

## Part-by-part assessment

| Spec section | Verdict | Why |
| --- | --- | --- |
| §2.1 benchmark-index inputs (SMA50/200, σ20, NATR14) | **Adopted** | Standard, deterministic, needs only published data. Built `md.index_1d` from NSE's `ind_close_all_DDMMYYYY.csv` (2019→) and `ta_quant.regime`. |
| §2.2 four-state classification | **Adopted as information** | Implemented exactly as specified (BEAR checked first), plus the documented-but-unbuilt RISK-26 hysteresis. Recorded nightly in `md.regime_daily`; shown on the dashboard as informational. |
| §2.2 "BEAR → HARD BLOCK" (REG-01) | **Rejected** | Lost to the control out of sample *and* in sample (evidence below). |
| §2.2 HIGH_VOL 50% / SIDEWAYS 75% heat | **Not adopted** | Existing caps are already 75% for CHOP and stricter (33%) for HIGH_VOL. |
| §3.2 Laplace `(k+1)/(N+2)` | **Rejected** | A uniform prior pulls estimates *toward 0.5*. With base rates ≈0.31 that **overstates** small-sample probabilities — the opposite of the spec's claim. `estimate_barriers` already shrinks toward the pooled base rate (κ=30), which is the right prior. |
| §3.3 n_eff with autocorrelation | **Not adopted as written** | The spec's own code sets `n_eff = N`, contradicting its formula. Overlap is handled by uniqueness weights (1/h) in the barrier library and, for the Calibration panel, by collapsing outcomes to distinct (symbol, exit session). |
| §3.3 fallback prior `1/(1+RR) = 0.429` | **Rejected** | That is the break-even probability *before costs*. Using it as the prior makes expectancy ≤ 0 by construction — a refusal disguised as a number. The system already refuses explicitly (I-15). |
| §4 Rule 2 `p > 1/(1+R)` | **Superseded** | The existing gate (`min_expectancy_atr = 0.05` after full Indian costs) is stricter. |
| §5.2 "Calibrated once 30 outcomes" | **Rejected** | A sample count is not calibration. This was exactly the bug in `/api/calibration` (it said OK over zero bins). It now reports INSUFFICIENT or NOT_BUILT; real isotonic calibration is M8. |
| §5.1 "stops the March–November 2026 drawdown" | **False** | In sample the filtered arm lost *more* (−13.9% vs −11.7%). |

## Evidence

Periods and arms were fixed before any result was seen. The bhavcopy history was extended to
2019 (legacy-format parser, verified field-identical to UDiFF on 2024-05-31) so the test could
run on data never used to design the rule. The first fully warmed-up date (252-session factors +
252-session base-rate window) is 2021-02-01.

**Strategy backtest, frozen control vs index regime + REG-01:**

| Period | Arm | Trades | Return | Sharpe | Max DD | Win |
| --- | --- | --- | --- | --- | --- | --- |
| OOS 2021-02 → 2025-07 (1,105 sessions) | control | 569 | **+1.66%** | **0.09** | −19.4% | 46.4% |
| | regime + REG-01 | 628 | −1.58% | 0.04 | −20.6% | 45.4% |
| IS 2025-07 → 2026-11 (321 sessions) | control | 122 | **−11.73%** | **−0.97** | −16.7% | 42.6% |
| | regime + REG-01 | 67 | −13.88% | −1.38 | −14.0% | 40.3% |

**Why — index-only check, 2019 → 2025-07** (mean Nifty 500 return over the next 10 sessions,
by regime on the day):

| Regime | Sessions | Mean fwd-10 | Share positive |
| --- | --- | --- | --- |
| BEAR | 251 | **+1.87%** | 71% |
| HIGH_VOL | 53 | +1.50% | 81% |
| BULL | 850 | +0.44% | 58% |
| SIDEWAYS | 266 | +0.21% | 68% |

A death cross (SMA50 < SMA200) confirms a fall months after it began. Its BEAR episodes were
2020-03-23 → 07-20 (the COVID recovery), 2022-04 → 08 (the 2022 bottom), 2025-01 → 05 — i.e. it
blocks the best entries in the history.

## Decision

- Keep `md.index_1d`, `ta_quant.regime` and `md.regime_daily`: real index data, an auditable
  regime record, and the research switch `ta backtest --regime-filter`.
- `nightly.REG01_ENABLED = False`. Live gating stays on the breadth heuristic, identical to the
  frozen control.
- The frozen control is untouched by any of this.

## Consequences

- **The frozen control has no edge either**: +1.7% over 4.4 years out of sample, Sharpe 0.09.
  The M3 gate ("positive deflated expectancy after full Indian costs") **fails**. Per M3 that
  means fixing the screener before adding anything — not adding filters on top of it.
- A future regime rule must beat the control **out of sample** before it gates a trade, and
  needs its own ADR. Candidates worth testing: faster trend signals (e.g. index below a falling
  50-DMA), drawdown-from-high, or breadth thrust — each pre-registered the same way.
- Caveat on all numbers: the universe is today's Nifty 500 applied back to 2019
  (survivorship bias flatters both arms equally; it does not change the comparison's sign).
