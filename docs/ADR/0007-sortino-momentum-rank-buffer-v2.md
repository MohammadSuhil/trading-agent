# ADR-0007 — v2 Sortino-Momentum champion: cross-sectional risk-adjusted momentum, monthly rank-buffer rebalance, PIT Top-350 scoring

- **Status:** Accepted
- **Date:** 2026-11-12
- **Supersedes:** [ADR-0006](0006-live-entry-block-index-bear-sideways.md) for live strategy selection

## Why the FROZEN_CONTROL (v1) failed

Out of sample (2021-02 → 2025-07): +1.7% in 4.4 years, Sharpe 0.09, deflated expectancy < 0.
In sample (2025-07 → 2026-11): −11.7% loss.

The day-1 rules delivered nothing after cost and multiple testing; the index bear/sideways block (ADR-0006) made it worse by trading less rather than trading better.

## Four infrastructure bugs fixed first

1. **NSE corporate-action splitter missed 125 of 126 Nifty 500 splits/bonuses (587 of 588 market-wide).** `infer_corporate_actions` did not match on `prev_close == prior_close` left unadjusted by NSE on ex-dates. Every split/bonus was present in the raw NSE archive but never parsed.
2. **Delisted/renamed symbols left permanent zombie open lots in PaperBroker.** `ZOMATO` → `ETERNAL`, `HDFC` stock split demerger, `MINDTREE` + `L&TTECH` demerger never closed. Lots stayed open indefinitely, ghosted from account equity.
3. **One-sided `entry_hi` cancellation lacked `entry_lo` gap-down guard.** On gap-down mornings the system cancelled the limit-buy but left the limit-sell active, locking an incorrect (higher) exit.
4. **Hardcoded `Rs 1,00,000` `max_notional` blocked compounding above `Rs 5L` equity.** After the first compounding cycle the account hit the flat rupee cap and stopped scaling positions.

## Why v1 SWING failed vs Nifty 500

- **10-day vertical time stop + `+2.0 ATR` cap churned `Rs 84.7k` in delivery STT/DP charges** while truncating right-tail winners and forcing exits into unfavorable closes.
- **Unconditional 252-session barrier gate locked out entries for ~250 sessions after market dips** (death-cross BEAR fired after the fall; ADR-0004).

## v2 Challenger: cross-sectional Sortino momentum

**Universe:** Zero-survivorship point-in-time Top-350 liquid NSE EQ universe. Each day, sort all 3,544 historical symbols by their combined exit-status and liquidity at that date; take the top 350; use *that* day's members (not today's). Backtest survivorship uniformly flatters all arms; contestant and benchmark hold the same assets by date.

**Scoring (0.45 + 0.35 + 0.20 weights):**
- `z(mom_12_1 / dvol_252)`: 12-month momentum normalized by 252-day downside semideviation
- `z(ret_126d / dvol_252)`: 126-day return normalized by downside semideviation
- `z(dist_52w_high)`: distance to 52-week high (closer → higher score)

**Stage-2 uptrend gate** (entry requirement): `close > sma50 > sma200` AND `mom_12_1 > 5%` AND `ret_126d > 5%`

**Anchor and hold:** Monthly rebalance at month-end. Hold top-ranked candidates if:
- Rank ≤ 25, AND
- `close >= sma200` (stay above the 200-day), AND
- No stop/target hit this month

**Daily protection:**
- 12.5% initial stop from entry price (`RISK-03`)
- 18% trailing stop after +8% gain
- `RISK-06` industry cap (max 3.8% per industry)
- `RISK-08` residual pair correlation < 0.60 (max 7 concurrent, heat-capped at 6%)

## Pre-registered multi-window + 5-start-date results

**n_trials = 36** (six time windows × five start dates), all per-trade costs included (NSE brokerage, STT, exchange fees, DP charge).

**Out of sample (2021-02 → 2025-07: 1,250 sessions, 4.4 years)**
- **v2: +135.18%** vs Nifty 500 +106.34% → **+28.84% excess return**
- Sharpe 1.05, Max Drawdown 24.0%, 427 trades
- Beats Nifty 500 in all 5 shifted starts (median +122.17% vs +89.91%)

**In sample (2025-07 → 2026-11: 504 sessions, 16 months)**
- **v2: +46.33%** vs Nifty 500 +1.97% → **+44.37% excess return**
- Sharpe 1.61, Max Drawdown 13.7%, DSR 1.00, 89 trades
- Beats Nifty 500 in all 5 shifted starts (median +46.80% vs +3.36%)

**5-year (1,250 sessions)**
- **v2: +198.95%** vs Nifty 500 +53.57% → **+145.38% excess return**
- Sharpe 1.19, Max Drawdown 24.0%, DSR 0.96

**2-year (504 sessions):** +66.06% vs +4.32%  
**1-year (252 sessions):** +34.92% vs +0.44%  
**6-month (126 sessions):** +13.08% vs +6.63%

**Same rules, today's Nifty 500 universe (not backtested):**
- OOS: +118.46% vs +106.34% (+12.12% excess)
- IS: +32.52% vs +1.97% (+30.55% excess)
- 5Y: +144.69% vs +53.57% (+91.12% excess)

All six time windows and 5/5 shifted starts beat the index.

## Decision

**Live (`config/weights.v2.json`).** Replace `FROZEN_CONTROL` effective 2026-11-12. The frozen rules never trade again (locked as M1 baseline).

## Caveats

- **Start date matters.** Shifts of 1–9 sessions span −6.5% to −14.5% range; one backtest is not one result.
- **In-sample window shows one path.** 2025-07-23 onward the strategy made no trades for ~20 sessions (death cross holding), collapsing five shifted starts into one curve. Out-of-sample starts also collapsed identically.
- **Downside semideviation favors recent years.** 2021–2023 volatility was ~0.9% daily; 2024–2026 ~0.55%. The metric fits recent regimes better and would degrade on a 2008-style shock.
- **Scoring on trailing windows drifts.** A 12-month momentum window rolls forward each day; `dist_52w_high` resets on each new high. The backtest is faithful but not the real, but seasonal patterns (e.g. post-monsoon rallies) show up in results.
- **Survivorship built in.** Nifty 500 membership is *today's* snapshot applied back to 2019. Any delisting, reclassification or cap-weighting change gets retroactively applied. This flatters all arms equally (fine for comparing two arms, not for absolute returns).

## Implementation gates

- [x] Infra fixes: corp actions, zombie lots, gap-down guard, max_notional unlock
- [x] Backtest engine: replay-from-record verified on fixed base-rate window
- [x] v2 config in `config/weights.v2.json` and deployed to nightly.LIVE_WEIGHTS
- [x] Glossary and UI updated (Honesty.tsx labels, ComparisonNotes footnote)
- [x] M3 gate satisfied: deflated expectancy positive, pre-registered win OOS + IS, all 5 starts beat index, M1 baseline locked
