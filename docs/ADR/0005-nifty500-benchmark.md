# ADR-0005 — The Nifty 500 index is the benchmark every strategy must beat

- **Status:** Accepted
- **Date:** 2026-11-11
- **Decided by:** owner ("the Nifty index should be the benchmark for the workflow now and in
  future when models are integrated")

## Context

Until now the only yardstick was the frozen control: "does the learned system beat the day-1
rules?" That question cannot tell whether the system is worth running at all. Measured against
the market, the frozen control — which *is* the current live strategy, since nothing has
learned yet and REG-01 is off (ADR-0004) — loses everywhere:

| Window | Frozen control | Nifty 500 (price) | Excess |
| --- | --- | --- | --- |
| 2026-05-13 → 2026-11-11 (126 sessions) | −11.68% | +6.63% | −18.3 pts |
| 2021-10-28 → 2026-11-11 (1,250 sessions) | −7.61% | +53.57% | −61.2 pts |

A strategy can beat the frozen control and still lose to simply holding the index.

## Decision

1. **Benchmark = the Nifty 500 index.** It is the universe the system scores and trades, so it
   is the passive alternative to running the system. (Nifty 50 is available in `md.index_1d`
   for secondary comparison.)
2. **Price index, labelled as such.** NSE's free daily archive (`ind_close_all`) publishes no
   total-return series. The price index omits ~1.2%/yr of dividends, so it is slightly *easy*
   to beat: treat a narrow win as a tie. If a TRI source that meets the data posture appears,
   it replaces this with a new ADR.
3. **Measured like the strategy.** Benchmark return runs from the close of the session before a
   backtest's first day to its last day — the same span as the strategy's return from starting
   capital. Stored per run by the job (`ev.backtest_runs.bench_return`, `excess_return`), never
   recomputed by the API.
4. **Two bars to clear, both pre-committed.** No arm, rule change or model (GBDT_META,
   COMMITTEE, anything later) is promoted to drive the book unless, **out of sample and after
   full Indian costs**, it beats **both** the frozen control **and** the Nifty 500.

## Consequences

- The Calibration page shows a benchmark verdict, a Nifty 500 row and a "vs Nifty 500" column;
  the Portfolio chart draws the index against the live paper book.
- **Single-run returns are not evidence.** With identical rules and data, moving a 126-session
  backtest's start by one to nine sessions moved its return between −6.5% and −14.5% (the
  index: +2.4% to +6.6%); over the same final 126 days a 1,250-session run made −2.2% and the
  126-session run −11.7%. Path dependence (which names fill the first slots, capital carried in)
  dominates short windows. Judging any rule — including against this benchmark — needs the
  spread across start dates or long out-of-sample windows, not one number.
- Today's verdict is unambiguous anyway: every start date and every window trails the index.
  Per M3 ("fix the screener before adding anything"), that is the problem to solve before any
  model is integrated.
