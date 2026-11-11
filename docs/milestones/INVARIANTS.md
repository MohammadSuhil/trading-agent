# Invariants — rules that outlive the milestone that set them

**Read this every session, whatever milestone you are on.** A milestone slice tells
you what to build; this file tells you what you must not break while building it.
Each rule names the milestone that established it and what enforces it.

**A rule whose "Enforced by" is `—` is a promise, not a guarantee.** Adding the test
is in scope for whoever next touches that code. `tests/test_milestone_docs.py` fails
if any test named below stops existing — so a deleted test cannot leave its rule
silently unguarded.

## Active — built and in force

| # | Invariant | Set in | Enforced by |
| --- | --- | --- | --- |
| I-01 | Every read of a PIT table is bounded by `knowledge_time <=` a **literal or driver-bound parameter**; `now()` and other volatile bounds are refused | M0 | `tests/pit/test_guard_hardening.py::test_a_volatile_bound_is_refused` |
| I-02 | …and that literal must **equal the run's as-of** — a stale constant copied from another query is a leak | M0 | `tests/pit/test_guard_hardening.py::test_a_stale_literal_is_refused_when_the_as_of_is_known` |
| I-03 | Every table in the schema that carries `knowledge_time` is registered in `PIT_TABLES` | M0 | `tests/pit/test_guard_hardening.py::test_every_pit_table_in_the_schema_is_registered_in_the_guard` |
| I-04 | `GuardedClient.query_admin` (unguarded) never appears in decision code | M1 | `tests/pit/test_guard_hardening.py::test_query_admin_is_not_used_in_decision_code` |
| I-05 | `ta_core` performs zero I/O — config is loaded by the app layer and passed in | M0 | `tests/test_golden_vectors.py::test_ta_core_contracts` |
| I-06 | One cost model (`ta_core.costs`) for every consumer; ₹1L delivery round trip = ₹237.59 | M0 | `packages/core/src/ta_core/costs.py` (doctest) |
| I-07 | Raw bytes land in `data/raw/YYYY/MM/DD/` **before** any parse | M1 | — |
| I-08 | Bars are deduplicated on `(symbol, d)` at read time; never rely on ReplacingMergeTree merge timing | M1 | — |
| I-09 | `ev.recommendations` is history: MergeTree with `run_id` in the key, so re-running never overwrites a past run | M2 | — |
| I-10 | Candidates are gated **sequentially against a growing book**, never each against the starting portfolio | M2 | `tests/test_risk_gates.py::test_sequential_construction_actually_binds_the_industry_cap` |
| I-11 | A gate rejects rather than resizing into compliance | M2 | `tests/test_risk_gates.py::test_risk_01_rejects_rather_than_rounding_up_to_the_floor` |
| I-12 | Portfolio heat uses the **current** resting stop, floored at zero | M2 | `tests/test_risk_gates.py::test_heat_uses_the_current_stop_not_the_entry_stop` |
| I-13 | Barrier geometry matches the trade: stop at `STOP_ATR = 1.5` ⇒ P(stop) measured at −1.5 ATR | M2 | — |
| I-14 | Same-bar ambiguity resolves **pessimistically** (DOWN / stop first) — in barriers now, and in every fill and outcome resolver later | M2 | `tests/test_ledger_rebuild.py::test_same_bar_ambiguity_resolves_to_stop` |
| I-15 | A probability is shown only when earned; otherwise the API refuses (em-dash), never guesses | M2 | `tests/test_api_contract.py::test_barrier_block_never_shows_a_number_it_has_not_earned` |
| I-16 | Portfolio-only actions (HOLD/SELL) never appear for an unowned name; WATCH cards carry no size | M2 | `tests/test_action_semantics.py::test_no_recommendation_uses_a_portfolio_only_action_for_an_unowned_name` |
| I-17 | Every code the API emits has a plain-English glossary entry | M2 | `tests/test_action_semantics.py::test_every_code_the_api_emits_has_a_plain_english_entry` |
| I-18 | Reason codes, failure modes, arms and regimes are **closed enums**; adding a member is a versioned act | M0 | — |
| I-19 | The API computes nothing: one SELECT per endpoint, every `*_pct` is a fraction of one | M1 | — |
| I-20 | The API binds loopback only (`127.0.0.1` + `::1`), never `0.0.0.0` | M1 | — |
| I-21 | `docs/CODEMAP.md` matches the code | M2 | `tests/test_codemap.py::test_committed_codemap_is_current` |
| I-22 | Barrier base rates use only paths with `exit_d <= d` (resolution date, never start date), from the last 252 exit sessions, via the one `barriers.known_paths` shared by live and backtest ([ADR-0003](../ADR/0003-fixed-base-rate-window.md)) | M3 | `tests/test_barrier_library_pit.py::test_backtest_library_filter_equals_live_library`, `::test_base_rates_do_not_depend_on_how_much_history_is_loaded` |
| I-23 | The Portfolio equity curve is the live paper book (`DETERMINISTIC`) only — no backtest arm is ever spliced in | M3 | `tests/test_api_contract.py::test_portfolio_equity_curve_is_only_the_live_paper_book` |
| I-24 | The index regime on session d uses only index bars ≤ d, and live and backtest read it identically; it is **informational** — `nightly.REG01_ENABLED` stays False until a rule beats the control out of sample ([ADR-0004](../ADR/0004-index-regime-filter-rejected.md)) | M3 | `tests/test_regime.py::test_regime_at_d_uses_only_bars_up_to_d`, `::test_live_pipeline_bear_block_downgrades_every_buy` |
| I-25 | `/api/calibration` never returns `OK` until M8's calibrator exists; n_eff merges overlapping windows per symbol and excludes backtest arms | M3 | `tests/test_api_contract.py::test_calibration_and_simulation_refuse_rather_than_guess` |
| I-26 | Every ingested archive row is dated the session that was requested (bars and index); a format change fails the file, never misfiles it. Weekends are probed — NSE trades some | M3 | `tests/test_ingest_guards.py::test_bar_file_dated_another_day_is_rejected`, `::test_month_first_index_file_lands_on_the_requested_session` |
| I-27 | The forward simulation is computed by the backtest job (never the API) and refuses below 750 sessions / 200 trades | M3 | `packages/quant/src/ta_quant/stats.py` (doctest `block_bootstrap_forward`) |
| I-28 | Variant and DETERMINISTIC backtests are report-only: they never write `ev.equity_daily` / `ev.outcomes`; a variant writes only its own `ev.backtest_runs` row (`line = 'REGIME_FILTER'`) | M3 | `tests/test_ingest_guards.py::test_regime_filter_backtest_never_touches_control_or_paper_book` |
| I-29 | Backtest results are stored **per window** (`ev.backtest_runs.sessions`): running one window never replaces another's; a window never run reports `NOT_RUN`, never another window's result. Statistics shown are the job's own, stored with the run | M3 | `tests/test_ingest_guards.py::test_each_window_keeps_its_own_last_run` |
| I-30 | Every stored backtest carries its benchmark (Nifty 500) return and excess return over the run's own dates, computed by the job from the session before the first day; no arm is promoted unless it beats both the frozen control and the Nifty 500 out of sample after costs ([ADR-0005](../ADR/0005-nifty500-benchmark.md)) | M3 | `tests/test_ingest_guards.py::test_each_window_keeps_its_own_last_run` |
| P-01 | Paper broker and backtester call the **same** `simulate_fill()` / `apply_costs()` objects (identity, not copies) | M2/M3 | `tests/test_backtest_matches_paper.py::test_backtest_matches_paper` |
| P-02 | One RunManifest, created in M2; M3 replay and M4 generation records **extend** it, never fork it | M2 | `tests/test_ledger_rebuild.py::test_run_manifest_roundtrip_and_extension` |
| P-03 | The ledger is hash-chained; a seq collision or gap fails `verify_chain()` | M2 | `tests/test_ledger_rebuild.py::test_seq_collision_and_gap_fail_verify_chain` |
| P-04 | `weights.v1.json` never changes after its tag — it is the control arm forever | M2 | `tests/test_weights_immutable.py::test_weights_v1_is_immutable` |
| P-05 | Every number that crosses a split/bonus uses **adjusted** prices, and the adjustment itself is PIT-bounded | M2 | `tests/test_corporate_actions.py::test_pit_bound_prevents_lookahead_leakage` |
| P-06 | Replay **re-derives** from the record; it never recalls a stored answer | M3 | `tests/test_replay.py::test_replay_rederives_and_catches_regression` |

## Planned — must hold from the milestone that builds it

The test named here does not exist yet; the milestone that builds the code must add it.

| # | Invariant | From | Test to add |
| --- | --- | --- | --- |
| P-07 | The LLM never emits a number absent from the dossier (V2, 0.5%) | M4 | validator tests |
| P-08 | At most one model resident: assert `GET /models` ≤ 1 **before** every load | M4 | `test_gpu_mutex` |
| P-09 | Every LLM-touched record carries `degraded_mode` and `model_tier` | M4 | generation-record test |
| P-10 | Committee control flow is fixed: golden stage sequence, no branching | M5 | `test_committee_shape` |
| P-11 | The committee runs in shadow and cannot write the paper book until the promotion gate passes | M5 | shadow-arm test |
| P-12 | PIT applies to **in-memory refits too** — calibration, posteriors, novelty, lesson-card retrieval use only data known at the decision | M7/M8 | per-module leakage tests |
| P-13 | Gate thresholds are frozen before data arrives, never adjusted after | M8 | frozen-config test |
| P-14 | The regime HMM is filtered, never smoothed, and only gates risk — it never flips a signal | M10 | regime leakage test |
| P-15 | No code path can place a live order | M11 | disabled order-port test |

When a planned rule's test lands, move the row to **Active** in the same commit.
