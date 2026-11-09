# ta_core — orientation

**Zero I/O.** stdlib + pydantic only: no sockets, files or databases. That rule is
what lets the whole test suite run with no services. Config (costs.yaml, holiday
YAML) is loaded by the *app* layer and passed in.

Signatures: [docs/CODEMAP.md](../../docs/CODEMAP.md) § packages/core.

| Module | Owns | Guard it embodies |
| --- | --- | --- |
| `asof.py` | `AsOf`, `build_pit_query`, `assert_pit_bounded`, `PIT_TABLES` | **The most important module.** Every PIT read is bounded by `knowledge_time <=` a *literal* equal to the run's as-of; `now()` is refused |
| `costs.py` | `compute_costs`, `CostConfig`, `CostBreakdown` | One cost model shared by backtester and paper broker, so they cannot drift. ₹1L delivery round trip = ₹237.59 |
| `calendar.py` | `TradingCalendar` (`from_observed`, `shift_sessions`, …) | History = sessions NSE actually published; handles Muhurat on a weekend |
| `enums.py` | `ReasonCode`, `FailureMode`, `Arm`, `Regime`, `Horizon`, `HORIZON_SESSIONS`, … | **Closed sets** — attribution needs repeating categories; adding a member is versioned |
| `timefmt.py` | `parse_feed_datetime`, `to_ist` | Tz-naive → IST, never silently UTC; never defaults to now() |
| `glossary.py` | `describe`, `as_dict` | Tooltip text lives with the rule, not in React |

**Still to write:** `ports.py`, `dto.py`, `ids.py`, `settings.py` (M2, item 1).

**Tests:** `tests/pit/` (the leakage suite, incl. the `now()` hole), doctests in
`costs.py`/`calendar.py`/`timefmt.py`.

**Before changing:**
- Adding a table that carries `knowledge_time`? Add it to `PIT_TABLES` and a PIT test.
- Touching `asof._BOUND`? Re-read `tests/pit/test_guard_hardening.py` first — it
  documents three holes the guard used to have.
- Cost rates are Zerodha-equivalent, verified Oct-2026; changing one moves every
  backtest number.
