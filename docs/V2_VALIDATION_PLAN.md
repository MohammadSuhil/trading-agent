# V2 validation plan — proving the live strategy is not a lucky pick

**Owner hand-off, 2026-11-12.** Written for an agent (Gemini) continuing on another machine.
Read `CLAUDE.md` → "Session hygiene" and `docs/milestones/INVARIANTS.md` first. Do the steps
in order; each has its own commit. Branch: `fix/backtest-pit-baseline-equity-chart`
(everything up to `cf307dd` is pushed).

## Where things stand

| | 5-year (1,250 sessions) |
| --- | --- |
| Live rules v2 (`config/weights.v2.json`, ADR-0007) | +151.9%, Sharpe 1.02, PSR 98.6%, **DSR 45.1%**, MinTRL 663 sessions |
| Frozen control v1 | −10.3%, Sharpe −0.18 |
| Nifty 500 (price) | +53.6% |

DSR asks: "is this Sharpe higher than the best Sharpe expected from N edge-less tries?"
With N = 36 the luck hurdle is ≈ 1.05 annualised, so a 1.02 Sharpe gives DSR ≈ 50%.

Two facts drive the plan:

1. **More history alone will not raise DSR.** At ≥ ~1,000 sessions the hurdle is set by the
   assumed cross-trial Sharpe spread (`std_sr_trials` floor of 0.5 annualised in
   `ta_quant.stats.compute_backtest_stats`), not by sample size. More data only narrows the
   estimate around the true Sharpe.
2. **N = 36 is understated.** The real count from the 2026-11-12 session is ≈ 49 (table in
   Step 2). DSR with the honest count is lower (~40%).

**Rules for the whole plan**

- Do **not** tune v2 to raise the backtest Sharpe. Every variant tried is a trial; tuning raises
  the hurdle and contaminates the evidence.
- Do **not** lower N to flatter DSR. N only goes down via Step 3's *measured* correlation.
- `config/weights.v1.json` never changes (P-04). After Step 1, neither does `weights.v2.json`.
- Commands: always `uv run --frozen`. Verify each step with
  `uv run --frozen python scripts/gen_codemap.py && uv run --frozen pytest -q`.
- Silence long outputs (`>/dev/null 2>&1; echo exit=$?`) — the 1,250-session backtest prints a lot.

---

## Step 1 — Freeze v2 (and fix the v1 freeze, which never actually binds)

**Why.** From the freeze date on, every nightly result is out-of-sample evidence nobody tuned.
Without a binding freeze that evidence is worthless.

**Bug to fix first.** `packages/quant/src/ta_quant/weights.py` sets
`WEIGHTS_V1_SHA256 = _compute_v1_sha256()` — it hashes the file *at import*, so editing
`weights.v1.json` changes the "expected" hash too and `tests/test_weights_immutable.py` still
passes. The P-04 guard is tautological today.

**Do.**

1. In `weights.py` pin literal digests (current values, verified 2026-11-12):
   ```python
   EXPECTED_V1_SHA256 = "cc60790d565e113b158ec44efa3ca6d1a12b1c6b57e7f2384f97210662cc343b"
   EXPECTED_V2_SHA256 = "947f172d8b4e3bfd0eb86c2c66e59fd953b9ff68ce3d52b8829ec3afd26225bd"
   ```
   Re-check with `shasum -a 256 config/weights.v1.json config/weights.v2.json` before pinning.
   `load_weights(verify_v1=True)` compares against the literal. Keep `WEIGHTS_V1_SHA256` as an
   alias of the literal so existing imports (API `/api/backtest`) keep working.
2. Add `verify: bool = True` to `load_challenger_weights`; raise
   `ValueError("... (Invariant P-16)")` on mismatch. Add `verify_v2_integrity() -> bool`.
3. Tests in `tests/test_weights_immutable.py`:
   - `test_weights_v2_is_frozen` — digest equals the literal; a tampered copy in `tmp_path`
     raises with `verify=True` and loads with `verify=False`.
   - `test_v1_guard_is_not_tautological` — assert `EXPECTED_V1_SHA256` is a string literal in
     the source (e.g. `inspect.getsource` contains the hex) rather than computed.
4. `docs/milestones/INVARIANTS.md` → Active: add
   `P-16 | weights.v2.json never changes after ADR-0007; live v2 is evaluated forward from the freeze date | M3 | tests/test_weights_immutable.py::test_weights_v2_is_frozen`.
   (`tests/test_milestone_docs.py` fails if a named test does not exist.)
5. Record the freeze date: add `V2_FROZEN_ON = date(2026, 11, 12)` next to
   `LIVE_STRATEGY_VERSION` in `packages/quant/src/ta_quant/nightly.py`. Step 5 measures from it.
6. API `/api/backtest`: expose `weights_v2_verified` alongside `weights_verified`; Honesty page
   chip "Live rules (v2): Locked" next to the existing v1 chip (`apps/ui/src/views/Honesty.tsx`).

**Done when** editing one byte of either weights file makes the suite fail.

---

## Step 2 — Trial ledger: record every variant, use the honest N

**Why.** DSR is only as honest as N. M3 already required "persisted trial counter; honest from
run #1" — it was never built; `n_trials=36` is hard-coded in `nightly.execute_backtest`.

**Do.**

1. Schema — append to `infra/clickhouse/schema_v2.sql` and apply with
   `./infra/apply_schema.sh infra/clickhouse/schema_v2.sql`:
   ```sql
   CREATE TABLE IF NOT EXISTS ev.strategy_trials (
       trial_id      String,
       recorded_at   DateTime64(3, 'UTC'),
       family        LowCardinality(String),   -- 'V1_SWING' | 'V2_MOMENTUM' | 'REGIME_BLOCK'
       label         String,
       params_json   String,
       data_note     String,                   -- e.g. 'unadjusted splits' for pre-fix runs
       window        String,                   -- '5Y' | 'OOS' | 'IS' | ...
       total_return  Nullable(Float64),
       sharpe        Nullable(Float64),
       daily_returns Array(Float32),           -- empty for back-filled rows until Step 3
       selected      UInt8                     -- 1 = the variant that went live
   ) ENGINE = MergeTree ORDER BY (family, trial_id);
   ALTER TABLE ev.backtest_runs ADD COLUMN IF NOT EXISTS n_trials UInt16 DEFAULT 0;
   ```
   Use `recorded_at`, **not** `knowledge_time` — a `knowledge_time` column must be registered in
   `PIT_TABLES` (I-03) and this is an audit log, not market data.
2. `packages/quant/src/ta_quant/trials.py` (new, ≤ 80 lines): `record_trial(...)`,
   `trial_count(ch, families=...) -> int` (distinct `trial_id`), `load_trial_returns(ch)`.
3. Back-fill the 2026-11-12 session's trials (script `scripts/backfill_trials_2026_11_12.py`,
   idempotent on `trial_id`). Every variant counts, including ones run on buggy data — the
   selection happened regardless. Values are 5-year (1,250 sessions) unless noted.

   | trial_id | family | label | 5Y return | 5Y Sharpe | data note |
   | --- | --- | --- | --- | --- | --- |
   | A0 | V1_SWING | baseline swing (v0 approx) | −13.57% | −0.24 | unadjusted splits |
   | A0b | REGIME_BLOCK | baseline + index bear/sideways block | +6.62% | 0.19 | unadjusted |
   | A1 | V2_MOMENTUM | positional h60, stop 2.5, tgt 8, trail 3 ATR | −1.81% | −0.01 | unadjusted |
   | A2 | V2_MOMENTUM | + uptrend-conditioned barrier | −15.70% | −0.31 | unadjusted |
   | A3 | V2_MOMENTUM | + XS risk-adj momentum + stage-2 | +27.08% | 0.75 | unadjusted |
   | A4 | V2_MOMENTUM | + 2% entry band, scaled max notional | +30.56% | 0.75 | unadjusted |
   | A5 | V2_MOMENTUM | + 1.25% risk, relaxed bull chop | +20.94% | 0.42 | unadjusted |
   | A6 | V2_MOMENTUM | A5 + index bear block | +24.88% | 0.50 | unadjusted |
   | A7 | V2_MOMENTUM | A5 + index bear/sideways block | +21.70% | 0.48 | unadjusted |
   | B-A..B-F | V2_MOMENTUM | positional full-expectancy barrier family (6 configs) | −15.7%..+41.5% | 0.28..0.71 | unadjusted |
   | R0 | V2_MOMENTUM | diagnostic, 6% heat cap | OOS +117% | OOS 1.54 | unadjusted |
   | M1..M3 | V2_MOMENTUM | risk 1.2–1.5%, stop 3–3.5 ATR | +30.5%..+42.8% | 0.58..0.84 | unadjusted |
   | P1..P3 | V2_MOMENTUM | 7 pos, 11% heat, exits sma200 / rank>100 | −19.8%..+16.4% | −0.26..0.29 | unadjusted |
   | RB1..RB5 | V2_MOMENTUM | rank-buffer, NSE-mom / mom-plus, 7–8 pos | +79.3%..+158.2% | 0.59..0.89 | unadjusted |
   | S1..S3 | V2_MOMENTUM | rank-buffer + regime slot scaling | +17.8%..+31.3% | 0.27..0.39 | unadjusted |
   | C1..C3 | V2_MOMENTUM | rank-buffer on split-fixed Nifty 500 | +141.5%..+261.2% | 0.89..1.38 | fixed |
   | Z1, Z2 | V2_MOMENTUM | PIT top-500, monthly / biweekly | +68.3% / −25.7% | 0.58 / −0.13 | fixed |
   | F1, F2 | V2_MOMENTUM | FIP momentum 7 / 8 pos (slot-floor bug) | −26.1% / −19.5% | −0.84 / −0.61 | fixed |
   | F3, F4 | V2_MOMENTUM | FIP on Nifty 500 / PIT top-500 | +85.8% / +90.3% | 0.75 / 0.77 | fixed |
   | SO1 | V2_MOMENTUM | Sortino momentum on Nifty 500 | +144.7% | 1.02 | fixed |
   | **SO2** | V2_MOMENTUM | **Sortino momentum, PIT top-350 (selected → v2)** | +198.9% | 1.19 | fixed |
   | RF1, RF2 | V2_MOMENTUM | mid-month refill / biweekly cadence | +99.8% / +138.9% | 0.69 / 0.93 | fixed |
   | G1..G5 | REGIME_BLOCK | ADR-0004 REG-01 + ADR-0006 V1–V4 | see ADRs | — | v1 engine |

   Ranges stand for one row per config (use labels like `B-A`, `B-B`, …). Total ≈ **49**.
   Mark `SO2` as `selected = 1`. The live engine's own 1,250-session result is +151.9% /
   Sharpe 1.02 (differences come from the engine refactor; record it as `V2_LIVE_ENGINE`, not a
   new trial).
4. `nightly.execute_backtest`: replace hard-coded `n_trials=36` with
   `max(1, trial_count(ch, families={'V2_MOMENTUM', 'REGIME_BLOCK'}))` for the live line; keep
   `n_trials=1` for the frozen control (it was never selected). Store `n_trials` in
   `ev.backtest_runs`; `/api/backtest` returns it; the DSR tile footnote reads
   "Deflated Sharpe (N = 49)".
5. Test: `tests/test_trials.py` — recording is idempotent; count is distinct ids; DSR falls
   monotonically as N rises (reuse `deflated_sharpe_ratio`).

**Done when** the Honesty page DSR tile states its N and the number comes from the ledger.

---

## Step 3 — Effective number of trials (measured, not guessed)

**Why.** Most of the 49 variants share one momentum core, so their returns are highly correlated.
Correlated trials are fewer *independent* tries; DSR should use the effective N and the measured
spread of cluster Sharpes instead of the 0.5 floor. This is the only legitimate way to lower the
hurdle. (Bailey & López de Prado 2014; López de Prado, "Detection of false investment strategies
using unsupervised learning", 2019.)

**Do.**

1. Make the v2 engine replayable for research **without touching the frozen config**:
   `run_backtest_v2(..., weights=replace(load_challenger_weights(), **overrides), score=...)`.
   Add a `score: str = "sortino"` parameter to `score_universe_v2` with the alternatives used
   in the session: `"nse_mom"` (0.5·z(ret_252/vol_252) + 0.5·z(ret_126/vol_252)),
   `"mom_plus"` (0.40/0.35/0.15/0.10 on mom_12_1/vol, ret_126/vol, dist_52w_high,
   industry drift), `"fip"` (adds er_126 and up-day fraction). Default stays `"sortino"`; a test
   asserts the default path is bit-identical to today's output.
2. `scripts/replay_trials.py`: re-run every **reconstructible** trial (the `RB*`, `C*`, `Z*`,
   `F*`, `SO*`, `RF*` rows map onto parameters above; v1/regime rows run via
   `run_backtest(... entry_block=...)`) over the same 1,250-session window on today's
   split-fixed data, and store `daily_returns` in `ev.strategy_trials`. Trials that cannot be
   reconstructed (`A*`, `B-*`, `M*`, `P*`, `S*`, `R0` were bespoke in-memory sims) keep empty
   returns and count as **independent** (conservative). Do **not** pick anything from these
   reruns — they exist only to measure correlation.
3. In `packages/quant/src/ta_quant/stats.py` add, with doctests:
   - `effective_trials(returns: Sequence[Sequence[float]]) -> float` — eigenvalue form
     $N_{eff} = (\sum\lambda_i)^2 / \sum\lambda_i^2$ of the correlation matrix (1 when all
     identical, N when uncorrelated). Optionally the ONC clustering variant.
   - `cluster_sharpe_std(...)` — per-session Sharpe std across cluster-average returns.
   - Total N for DSR = $N_{eff}$(replayed) + count(non-replayed).
4. Use these in `execute_backtest` for the live line; keep the 0.5-annualised floor only as a
   fallback when fewer than 3 return series exist. Show "N_eff = x of 49" on the DSR tile.

**Done when** DSR is computed from measured N_eff and measured Sharpe spread, both visible on
the page, and the default v2 path is unchanged (test).

---

## Step 4 — One pre-registered long-history check (includes COVID)

**Why.** No test so far started before 2021-02; 2020 (COVID crash and recovery) has never been
seen by any v2 decision. It costs exactly **one** trial.

**Do.**

1. **Before running anything**, commit `docs/prereg/2026-11-v2-long-history.md` containing:
   - Rules: v2 exactly as frozen (`weights.v2.json` digest from Step 1), no changes.
   - Window: first scorable session ≈ 2020-01-15 (needs 252 bars from 2019-01-01 for
     `mom_12_1` / `dvol_252`) → latest session. Sub-window 2020-01-15 → 2021-01-29.
   - Pass criteria (all must hold):
     1. Full window: return > Nifty 500 and > frozen control.
     2. Sub-window 2020-01-15 → 2021-01-29: max drawdown ≤ Nifty 500 drawdown + 10 pts.
     3. Five starts shifted 0/5/10/15/20 sessions: beats Nifty 500 in ≥ 4/5.
   - Fail action: v2 stays paper-only, finding recorded in a new ADR; no re-tuning to pass.
   The commit hash proves the criteria preceded the result.
2. Run (one process per start; silence output):
   ```python
   from datetime import date
   from ta_core.enums import Arm
   from ta_quant.challenger import run_backtest_v2
   # load bars/members via nightly.load_panel_inputs(ch, d, a=AsOf(now), lookback=2200),
   # keep series == 'EQ' (PIT top-350 universe), then:
   res = run_backtest_v2(bars, members, start_date=date(2020, 1, 15), end_date=d,
                         arm=Arm.DETERMINISTIC, use_pit_liquidity=True, warmup_sessions=0)
   ```
   Benchmark: `nightly.benchmark_return(ch, a, start, end)`. Frozen control:
   `run_backtest(... arm=Arm.FROZEN_CONTROL, strategy_version="v1")` on Nifty 500 bars.
3. Record as one row in `ev.strategy_trials` (`label = 'PREREG_LONG_HISTORY'`), whatever the
   outcome. Write the verdict into the prereg file and an ADR (pass or fail).

---

## Step 5 — Forward evidence from the freeze date

**Why.** This is the only evidence that cannot be overfit. MinTRL at Sharpe ≈ 1.0 is ≈ 663
sessions (~2.6 years); interim checkpoints catch a broken strategy much sooner.

**Prerequisite — paper-book parity (fix before trusting forward numbers).**
`nightly.step_paper_book` does not execute like the backtest:
- auto-BUY fills at the **signal day's** entry-band midpoint (`(entry_lo + entry_hi) / 2`),
  while `PaperBroker` fills at the **next session's open** with gap checks;
- signal SELLs exit at the signal day's close, not the next open;
- a hard 14-calendar-day exit applies to SWING lots (POSITIONAL lots get 365 days since
  2026-11-12).
Forward results would therefore be optimistic and not comparable to the backtest. Route the
live book through `PaperBroker.step_session` (`place_orders` for BUY, `schedule_exit` for SELL,
`trail_stop` for HOLD stop raises), persisting events to `ev.ledger_events` and projecting
`ev.positions` / `ev.equity_daily` via `rebuild_from_ledger`. Add a test that one session of
live stepping equals one session of `run_backtest_v2` on the same inputs (P-01).

Also decide what to do with the two v1-era SWING lots (IGIL, CEMPRO): either let them run off,
or start the forward record from the first session after they close. State the choice in the ADR.

**Do.**

1. Ensure the nightly job runs: `./infra/install_launchd.sh install && ./infra/install_launchd.sh status`
   (`com.tradingagent.nightly` must be loaded; it runs `ta daily` after the bhavcopy lands).
2. `ta review` CLI command (or `/api/forward` endpoint, one SELECT per I-19) reporting, from
   `V2_FROZEN_ON` to today, for the live paper book (`ev.equity_daily`, arm `DETERMINISTIC`):
   return, Nifty 500 return over the same dates, excess, max drawdown, Sharpe, PSR with
   `n_trials = 1` (forward returns were not selected, so no deflation), sessions so far vs MinTRL.
3. Pre-register checkpoints now (same prereg discipline as Step 4), e.g.:

   | Checkpoint | Continue if | Demote to watch-only if |
   | --- | --- | --- |
   | 6 months | drawdown < 30% | trails Nifty 500 by > 15 pts or drawdown ≥ 30% |
   | 12 months | excess ≥ 0 or within backtest 5-start range | trails by > 15 pts |
   | 24 months | forward PSR ≥ 0.90 | forward PSR < 0.5 |

   No real capital (M11) before the 24-month checkpoint passes **and** combined DSR ≥ 0.95.
4. Calendar reminders for each checkpoint; results go into `docs/milestones/M3.md` `## Built`.

---

## Verification after every step

```bash
uv run --frozen python scripts/gen_codemap.py
uv run --frozen pytest -q >/dev/null 2>&1; echo "pytest exit=$?"
npm --prefix apps/ui run build >/dev/null 2>&1; echo "ui exit=$?"
launchctl kickstart -k "gui/$(id -u)/com.tradingagent.api"
```

Re-store backtests after Steps 2–3 change DSR (one window at a time, output silenced):

```bash
uv run --frozen python -c "import uuid; from ta_quant.nightly import execute_backtest; r=f'BT-{uuid.uuid4().hex[:12]}'; execute_backtest(sessions=1250, persist_results=True, run_id=r); execute_backtest(sessions=1250, regime_filter=True, run_id=r)" >/dev/null 2>&1; echo exit=$?
```
