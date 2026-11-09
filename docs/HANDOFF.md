# Handoff — build state as of 2026-10-31

## Status: M0 complete. M1 and most of M2 built, running, and green.

```
uv run --frozen pytest        ->  116 passed
uv run --frozen ruff check    ->  All checks passed
./infra/smoke_test.sh         ->  ALL PASS (10 gates)
./infra/install_launchd.sh status -> 4 agents loaded
```

**The system is live.** Open <http://127.0.0.1:8000> — FastAPI serves the built SPA and the API
from one process.

### Real data in the store

```
md.bars_1d        1,220,615 rows · 3,197 symbols · 492 sessions · 2024-11-01 .. 2026-10-30
md.index_members  851 rows (NIFTY 50/100/200/500, dated snapshot)
ref.symbols       2,620
on disk           39.7 MiB for the whole 2-year panel
```

**Measured ClickHouse compression: 3.59×** — 123.85 MiB uncompressed → 34.52 MiB, read from
`system.parts.data_uncompressed_bytes`, not estimated. An earlier revision of this file claimed
5.3×; that came from assuming ~170 B/row rather than asking ClickHouse, and was wrong. PLAN.md
asked for a measured figure precisely so the oft-quoted 10–15× did not get inherited: the real
number sits just above the official 2.7–3.5× range.

### What runs

| Command | Does |
| --- | --- |
| `uv run --frozen ta daily` | the whole nightly run: ingest → symbols → membership → screen → record (~10 s incremental) |
| `uv run --frozen ta status` | what is in the database |
| `./infra/install_launchd.sh install\|status\|uninstall` | the four launchd agents |
| `http://127.0.0.1:8000` | the dashboard |

### Four bugs found and fixed while wiring this up — all were silent

1. **`md.bars_1d` accumulated duplicate rows.** ReplacingMergeTree collapses duplicates only on
   a background merge whose timing is not guaranteed, so re-ingesting a session left two rows
   for `(symbol, d)`. Not cosmetic: a duplicated bar shifts every trailing window by one, so
   SMA20 silently became a 20-row average of 19 distinct sessions and the entire factor stack
   was quietly wrong. Now deduped on read in `nightly.load_panel_inputs`, which does not depend
   on merge timing.
2. **`ev.recommendations` destroyed its own history.** `ReplacingMergeTree ORDER BY (d, arm,
   symbol)` meant re-running the job REPLACED the previous run's row for a symbol. The table
   whose purpose is "what did the system recommend on this date" was erased by recommending
   again. Now `MergeTree` with `run_id` in the sort key.
3. **The barrier geometry did not match the trade.** The library measured P(touch −1.0 ATR)
   while the stop sat at −1.5 ATR, overstating P(stop) enough to make every candidate look
   negative-expectancy. Same class of error the plan warns about for the time stop, applied to
   price barriers.
4. **The screener sized every candidate against the starting portfolio**, so the industry cap,
   heat cap and position count never bound — 25 cards that jointly breached every rule the
   system claims to enforce. Now gated sequentially against a growing book.

### ⚠️ What the system currently says about itself — read this before trading anything

Tonight's run produces **25 cards and zero BUYs.** That is not a failure, it is the honest
answer, and it is worth understanding before you override it.

The barrier estimate is the arm-invariant BASE rate. The EDGE term that would differentiate a
specific candidate contributes exactly 0.0 until the system has resolved ≥ 30 of its own trades.
A base rate against a 2.0 ATR target and a 1.5 ATR stop is ≈ zero expectancy before costs and
negative after them — the best candidate tonight scores **+0.04 ATR against a +0.05 bar.**

So every card is published with a full trade plan and labelled `no demonstrated edge`, with its
arithmetic printed. **The system has not yet shown it can pick stocks.** It will not be able to
say otherwise until the four-arm comparison has resolved trades behind it — which is exactly the
sequence PLAN.md's governing principle 5 describes, and exactly why `frozen_control` exists.

## What exists

| Component | State |
| --- | --- |
| uv 0.13.6 + CPython 3.13.18 | installed, workspace syncs |
| ClickHouse 26.10.2.103 | running, **5.00 GiB cap verified in effect** |
| MongoDB 9.0.4 | running, replica set `rs0` primary, **WT cache 2.00 GB verified** |
| Redis | running (pre-existing brew install) |
| `ta_core.enums` | closed enums: ReasonCode, FailureMode, Horizon, Sleeve, Arm, Regime… |
| `ta_core.costs` | full Indian cost model, doctested |
| `ta_core.timefmt` | 7-format date normaliser |
| `ta_core.calendar` | NSE sessions, handles Muhurat-on-a-Sunday |
| `ta_core.asof` | **the PIT guard** + 18-test leakage suite — **hardened 2026-10-31, see below** |

Still to write in `core`: `ports.py`, `dto.py`, `ids.py`, `settings.py`.

### The PIT guard had a live look-ahead hole (fixed 2026-10-31)

`assert_pit_bounded` proved only that a `knowledge_time` bound *existed*, never what it was bound
to. So this passed the build-failing leakage test:

```sql
SELECT * FROM md.bars_1d WHERE knowledge_time <= now()
```

`now()` moves with the wall clock, so replaying 2026-10-15 today reads everything learned since —
straight through the one guard CLAUDE.md calls the thing to get right if only one thing is right.
The guard now refuses any bound whose right-hand side is not a literal or a driver-bound
parameter, and `assert_pit_bounded(sql, as_of)` additionally requires the bound to **equal** the
run's as-of (a stale constant copied from another query was the third hole).

Still true and worth knowing: this is a **build-time, syntactic** check over query text, not a
parse, and not proof that the bound held at execution. The runtime proof is the `traceparent`
span described in PLAN.md §Observability, scheduled at M4.

## Verified numbers (regenerate, don't trust prose)

- ₹1,00,000 **delivery** round trip = **₹237.59 (23.76 bps)**
- ₹1,00,000 **intraday** round trip = **₹82.45 (8.25 bps)**
- DP charge drag: 15.34 bps on ₹10k → 3.07 on ₹50k → 1.53 on ₹1L → 0.31 on ₹5L
- DB footprint at rest: ClickHouse 0.42 GB + watchdog 0.10, mongod 0.14, redis ~0

## ⚠️ Four silent failure modes already hit — do not re-introduce

Each of these **looked fine, started cleanly, and was wrong**. Three were self-inflicted
config errors. This is why every phase needs a gate.

1. **Gatekeeper kills quarantined binaries with no output.** The Homebrew *cask* ClickHouse
   died with exit 137 and zero log output. Fix used: the official GitHub release binary
   fetched with curl into `infra/bin/clickhouse` (curl-fetched files get no quarantine
   attribute). **Do not reinstall the cask.** Do not strip quarantine — that was correctly
   blocked as a security action.
2. **`max_server_memory_usage_to_ram_ratio` = 0 silently cancels the absolute cap.**
   ClickHouse computes `48 GB × 0 = 0` and treats 0 as *unlimited*. The server started
   happily with **no cap at all** and the config looked correct. Leave the ratio at its
   0.9 default; set only the absolute. Always verify via
   `SELECT value FROM system.server_settings WHERE name='max_server_memory_usage'`.
3. **`--` inside an XML comment makes the config malformed** — and ClickHouse's failure
   mode is to log nothing useful and hang in its *shutdown* path, which reads like a slow
   startup. `infra/services.sh` now runs `infra/validate_config.sh` before every start.
4. **`background_pool_size` too small throws `Code 36`.**
   `number_of_free_entries_in_pool_to_execute_mutation` (default 20) must be **less than**
   `background_pool_size × background_merges_mutations_concurrency_ratio`. Current config
   uses pool 8 × ratio 2 = 16, with the merge_tree thresholds lowered to 4.

Also: **ClickHouse holds port 9000 briefly after SIGTERM.** Restarting too fast fails with
"Address already in use" and the new process exits silently. `services.sh down` now waits
for the port to actually free.

## Diagnostic that cracked it

When ClickHouse hangs with no useful log, sample the process — it showed the main thread in
`ContextSharedPart::shutdown()`, proving it was *shutting down*, not starting:

```bash
sample $(pgrep -f "infra/bin/clickhouse server" | head -1) 3 -mayDie
```

For the real error, run with a debug/console config:
```bash
sed 's|<level>warning</level>|<level>debug</level>\n<console>1</console>|' \
    infra/clickhouse/config.xml > /tmp/ch_debug.xml
infra/bin/clickhouse server --config-file=/tmp/ch_debug.xml 2>&1 | grep -v "@ 0x"
```

## Deliberately NOT done

- ~~**launchd agents are written but not installed.**~~ **Superseded 2026-10-31:** all four
  agents (`clickhouse`, `mongod`, `api`, `nightly`) are now installed and loaded — the owner
  asked for unattended operation explicitly. `./infra/install_launchd.sh status` confirms.
  Scheduled WAKE is still NOT set and needs sudo from you:
  `sudo pmset repeat wakeorpoweron MTWRF 18:40:00`. Without it, a run whose 18:45 window passes
  while the Mac sleeps defers to the next wake. Original note follows for context:
  `cp infra/launchd/*.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.tradingagent.*.plist`
  Until then use `./infra/services.sh up` per session.
- No LLM, no model downloads yet (that is M4).
- Fundamentals research pass (`wf_431cc86b-d0b`) never finished — it was interrupted twice
  by session restarts. Its 4 research agents completed and are cached; only the verify +
  synthesis stages remain.

## Plan corrections made 2026-10-31 — read these before building M4+

Four claims in PLAN.md were wrong or unflagged. All are now fixed in the document; the two
irreversible ones have ADRs.

1. **[ADR-0001](ADR/0001-plain-python-orchestration.md)** — orchestration stays plain Python, but
   the old rationale was a strawman ("frameworks exist to let an LLM choose control flow" is
   false). The argument that actually survives is that the generation record is mandatory anyway,
   so a framework's checkpointer is a duplicate store. Measured: resuming under LangGraph after
   fixing a persona serves the *stale* value into a green run with no warning.
2. **[ADR-0002](ADR/0002-replay-from-record.md)** — "byte-identical replay" was impossible as
   written. Prefix caching (needed for the 20:45–22:05 window) and bit-exact regeneration are
   incompatible, and llama.cpp will not give bit-exactness on Metal. Replay now means
   **replay-from-record**, and the ledger must store prompt/grammar/seed/token-stream/model hashes.
3. **`--models-max 1` is not a structural invariant.** Router mode is experimental (#18035) and a
   TOCTOU race in `unload_lru()` was reported at exactly that setting (#20137). Assert
   `GET /models` ≤ 1 **before** every load, not only empty after.
4. **The no-telemetry constraint is already violated** — by Prefect, not by any rejected
   framework. Set `PREFECT_SERVER_ANALYTICS_ENABLED=false` and `DO_NOT_TRACK=1` before M1.

## Next

**M2 remainder** — the hash-chained ledger and PaperBroker are specified but not written
(`packages/ledger` has `risk.py` only). Until they exist the system recommends but does not
record fills, so nothing resolves, so calibration and the four-arm comparison stay at
`INSUFFICIENT`. That is the next thing that unblocks everything downstream.

**Then M3**, whose gate is the anti-drift invariant: replaying history through the paper engine
must reproduce the backtest curve to rounding. And per ADR-0002, `ta daily --date X --replay`
must **re-derive, never recall**.

**Not started:** news/NLP (M7), the LLM layer and committee (M4–M5), the learning loop (M8).

## Superseded: the original M1 note

Deliverable: `NSEArchiveSource` + `BSEArchiveSource` sharing one UDiFF parser; raw bytes to
`data/raw/` before parsing; on-disk cache keyed by `(url, date)` — a past bhavcopy never
changes, so fetch it once ever; 2 years of `md.bars_1d`; `md.index_members` as **dated
snapshots** (not current membership); `md.sector_daily`; FastAPI `/api/heatmap`,
`/api/screener`; Vite+React treemap.

**Gate:** `/api/heatmap` p95 < 30 ms (must be one SELECT from `md.sector_daily`, not a
500-stock computation per request). Spot-check 5 symbols' close against NSE. Then measure
the real ClickHouse compression ratio and **replace every storage estimate in PLAN.md with
it** — the 10–15× figure everyone quotes is unsourced; official figures are 2.7–3.5×.

---

## ⚠️ The most under-advertised gap: no corporate-action adjustment

`md.corporate_actions` is empty and nothing adjusts prices. That makes
**leakage vector #3 from `ta_core.asof`'s own module docstring live right now**,
against two years × 3,197 symbols of unadjusted closes:

> *"Unadjusted splits -- a 1:2 split reads as an instant -50% return, which a
> momentum factor happily trades on."*

Every trailing return, every moving average, every ATR and every barrier path
crossing a split or a bonus issue is wrong, and wrong in a direction that looks
like a tradeable signal. The barrier library resamples those same paths, so the
base rates inherit it too.

This is not a rounding problem and it is not deferred by a milestone — it is a
correctness hole in data the system is already using. Fix before trusting any
backtest number.

### Other gaps worth knowing, found in the 2026-10-31 documentation audit

- **`RISK-08`'s `pair_corr` is never computed.** The residual-correlation gate
  is the rule PLAN.md calls "not optional", because it is the counterweight to
  the large-cap bias the ₹50k floor creates. It currently receives an empty
  dict and therefore never fires.
- **`md.surveillance` is empty**, so ASM/GSM names are not actually excluded;
  `RISK-12` only catches the series check.
- **No golden-vector test for factors or trend.** A silent arithmetic
  regression in Wilder smoothing or the HAC variance would flow through every
  card with the whole suite still green. This is the single biggest hole in an
  otherwise strong test suite.
- **Four tests PLAN.md names do not exist**: `test_backtest_matches_paper`,
  `test_ledger_rebuild`, `test_committee_shape`, `test_gpu_mutex`. Nor does the
  `--replay` CLI flag that ADR-0002 specifies.
- **No `weights.v1.json` frozen control.** Until it exists, "is the learning
  real?" is unanswerable by construction, not merely unanswered.
