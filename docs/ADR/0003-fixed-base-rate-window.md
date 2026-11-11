# ADR-0003 — Barrier base rates come from a fixed 252-session window, cut by resolution date

- **Status:** Accepted
- **Date:** 2026-11-11
- **Resolves:** the frozen control's behaviour depending on how much history was loaded, and a
  look-ahead leak in the backtest's path library

## Context

`config/weights.v1.json` freezes the control arm's composite weights, barrier geometry and its
expectancy gate (`min_expectancy_atr: 0.05`). It does **not** freeze the probability that feeds
that gate: `p_up` / `p_dn` come from the triple-barrier path library, and the library was built
over *every loaded session*. So the "frozen" control changed behaviour whenever history was
backfilled:

| Sessions loaded | Base `p_up` | Frozen-control trades (same 320-session test) |
| --- | --- | --- |
| 420 | 0.337 | 95 |
| 580 | 0.313 | **0** |

A control that stops trading because older data was downloaded cannot be the yardstick PLAN.md
§Learning loop needs ("if the learned system can't beat this, the learning is cosmetic").

Separately, the backtest built the library once over the whole panel and filtered it by path
**start** date (`d < session`). A swing path that started 1–9 sessions earlier is decided by bars
*after* the session, so every simulated day could see ~10 sessions of future outcomes.

## Decision

1. Every path carries `exit_d`, the session its window closes on. Paths are filtered by
   `exit_d <= d`, never by start date.
2. Base rates use only the last **252** distinct exit sessions (`BASE_RATE_WINDOW_SESSIONS`).
3. Both cuts live in one function, `ta_quant.barriers.known_paths`, called by the live nightly
   (`pipeline.run_nightly`) **and** the backtest (`engine._screen_session`), so the two quote the
   same `p_up` for the same session.

`weights.v1.json` is unchanged (P-04 holds); the gate is still applied at 0.05.

## Alternatives rejected

- **Drop the gate for the control (rank-only).** History-independent, but overrides a field of
  the frozen spec — the control would no longer be the day-1 rule set.
- **Lower the gate until the control trades.** Tuning the yardstick to a desired result.
- **Keep the expanding window.** Honest, but the control's result becomes a function of backfill
  depth rather than of the rules.

## Consequences

- Over the same 320 sessions (2025-07-23 .. 2026-11-09) the control now makes **122 trades,
  −11.73%**, Sharpe −0.97 — instead of 0 trades. The day-1 rules lose money; that is the finding.
- The live nightly's `p_up` changes too (window 252 instead of ~420). Recommendations before this
  ADR were made with the expanding library; their `ev.recommendations` rows record the `p_up` they
  used (ADR-0002), so replay is unaffected.
- The base rate now tracks the last year of regime. That is a mild form of adaptation in the
  control; it is accepted because the alternative is dependence on an arbitrary backfill date.
- Tests: `tests/test_barrier_library_pit.py`.
