# Data model

Every ClickHouse table, what one row means, and the `knowledge_time` contract. Also the MongoDB and
Redis roles, which are smaller than the plan implies.

Companion documents: [LLD.md](LLD.md) (module design), [API.md](API.md) (what gets served),
[OPERATIONS.md](OPERATIONS.md) (applying schema, backup), [diagrams/clickhouse-er.md](diagrams/clickhouse-er.md).

All counts, sizes and engines below were read from the running ClickHouse on 2026-10-31 via
`system.parts` and `system.tables`. ClickHouse version **26.10.2.103**, listening on `127.0.0.1`,
HTTP `8123` / native `9000`, server timezone `Asia/Kolkata`.

---

## 1. Databases

Five application databases, separated by subject rather than by lifecycle:

| DB | Subject | Tables | Carries `knowledge_time`? |
| --- | --- | --- | --- |
| `md` | market data | 9 | yes, all |
| `nx` | news / events | 1 | yes |
| `fn` | fundamentals | 2 | yes |
| `ev` | evaluation / learning | 8 | 5 of 8 |
| `ref` | reference data | 1 | **no, by design** |

`ref` is deliberately outside the point-in-time regime: reference data has no knowledge_time and
`build_pit_query` passes it through unbounded. See §2.

Created by `infra/clickhouse/schema.sql` (reference, bars, membership, corporate actions,
surveillance, features, sector rollup, news, fundamentals, outcomes/attribution/posteriors) and
`infra/clickhouse/schema_v2.sql` (trend, breadth, benchmark, recommendations, rejections, ledger,
positions, equity curve). Both are idempotent (`CREATE TABLE IF NOT EXISTS`) and applied by
`infra/apply_schema.sh` — see [OPERATIONS.md](OPERATIONS.md#4-schema).

---

## 2. The `knowledge_time` contract

### 2.1 What it means

`knowledge_time` is **the instant the row became knowable to this system**. It is not the event
date, and it is not the crawl time. `CLAUDE.md` states the rule as a governing principle:

> `knowledge_time` ≠ event date ≠ crawl time. Every point-in-time table carries `knowledge_time`;
> all reads go through `ta_core.asof.build_pit_query`; a CI test fails the build on any unbounded
> read. **If only one thing stays correct, make it this.**

Worked example, which is the clearest way to internalise it. NSE publishes the bhavcopy for session
`2026-10-29` at roughly 18:30 IST **on that same evening**. The row therefore has:

| Column | Value | Axis |
| --- | --- | --- |
| `d` | `2026-10-29` | the **event** — which session this bar describes |
| `knowledge_time` | `2026-10-29 13:00:00` UTC (= 18:30 IST) | the **knowledge** — when we could first have used it |

A decision made at 15:00 IST on 2026-10-29 has `as_of = 09:30 UTC`, which is **before**
`knowledge_time`, so that bar is correctly invisible. The two axes move independently and
conflating them is leakage.

`BHAVCOPY_PUBLISH_IST = time(18, 30)` lives in
`packages/datafeed/src/ta_datafeed/sources/nse_archive.py`.

### 2.2 The reads contract

Every read of a PIT table must carry `knowledge_time <= <pinned instant>` where the right-hand side
is a **quoted literal**, a DB-API placeholder `%(name)s`, or a ClickHouse native binding
`{name:Type}`. Anything evaluated at execution time — `now()`, `today()`, a column, a subquery — is
refused with `PITViolation`.

The guard is installed in `ta_datafeed.store.GuardedClient`, the only door into the database. It is
a **build-time, syntactic** check over query text. See [LLD.md §1.1](LLD.md#11-ta_coreasof--the-point-in-time-guard)
for the three failure modes it was hardened against, including the one that was live.

### 2.3 `PIT_TABLES`

Registered in `packages/core/src/ta_core/asof.py`. 23 entries:

```
md.bars_1d          md.bars_1m          md.features_1d      md.index_members
md.corporate_actions md.surveillance    md.sector_daily     md.trend_1d
md.breadth_daily    md.benchmark_1d     ev.recommendations
nx.news_features    nx.filings          nx.events
fn.statements       fn.ratios           fn.shareholding     fn.estimates
ev.outcomes         ev.attribution      ev.posteriors
```

Four of these (`nx.filings`, `nx.events`, `fn.ratios`, `fn.estimates`) are **registered but not yet
created**. That is the safe direction of mismatch: a table in the guard but not in the schema is
inert, whereas a table in the schema but not in the guard is read unbounded forever and nothing
complains.

That second failure is machine-checked.
`tests/pit/test_guard_hardening.py::test_every_pit_table_in_the_schema_is_registered_in_the_guard`
parses `infra/clickhouse/schema*.sql`, collects every `CREATE TABLE` whose body declares a
`knowledge_time` column, and asserts the set is a subset of `PIT_TABLES`. **If you add a table with
a `knowledge_time` column, this test fails until you register it.**

### 2.4 Tables deliberately NOT point-in-time

| Table | Why not |
| --- | --- |
| `ref.symbols` | reference data; the listed-security master is a lookup, not a fact about a date |
| `ev.rejections` | a rejection is a fact about one `run_id`, already scoped by `run_id` in the sort key; no `knowledge_time` column exists |
| `ev.ledger_events` | append-only and hash-chained; ordering is `seq`, and tamper-evidence replaces the PIT bound |
| `ev.positions` | current book state, `ReplacingMergeTree(as_of)`; "what do I hold now" is not a historical read |
| `ev.equity_daily` | a per-arm daily equity point; reconstructible from the ledger |

`ev.recommendations` **is** in `PIT_TABLES`, and the reason is worth repeating because it is
non-obvious. From `asof.py`:

> A past recommendation must be served exactly as it was recorded. Putting it behind the PIT guard
> is what stops an endpoint from "helpfully" recomputing a probability with data that arrived after
> the decision.

---

## 3. Storage footprint, measured

Read from `system.parts WHERE active`, 2026-10-31:

| Table | Rows | Parts | On disk | Uncompressed | Ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| `md.bars_1d` | 1,220,615 | 24 | **35.40 MiB** | 123.85 MiB | **3.59×** |
| `md.trend_1d` | 2,919 | 1 | 150.13 KiB | 169.57 KiB | 1.38× |
| `ref.symbols` | 2,620 | 1 | 92.64 KiB | 183.23 KiB | 2.19× |
| `md.index_members` | 851 | 1 | 26.98 KiB | 57.82 KiB | 2.77× |
| `ev.rejections` | 124 | 2 | 5.85 KiB | 9.35 KiB | 4.46× |
| `ev.recommendations` | 25 | 1 | 16.60 KiB | 13.43 KiB | 2.28× |
| `md.sector_daily` | 20 | 1 | 5.53 KiB | 1.25 KiB | 0.93× |
| `md.breadth_daily` | 1 | 1 | 2.71 KiB | 75 B | 0.23× |
| all others | 0 | 0 | — | — | — |

`data/clickhouse/` on disk, including metadata, system tables and logs: **37 MB**.

> **Every row count on a `ReplacingMergeTree` is merge-state-dependent.** Earlier in the same
> session these tables read `md.trend_1d` 8,757 / 3 parts, `ref.symbols` 7,860 / 3 parts,
> `md.sector_daily` 60 / 3 parts and `md.breadth_daily` 3 / 3 parts — the same logical data, three
> nightly runs deep, before a background merge collapsed each to one part. `SELECT count()` on a
> Replacing table answers *"how many rows are stored"*, not *"how many facts exist"*. Use
> `SELECT count() FROM (SELECT DISTINCT <sort key> …)` when you mean the latter, and see §5's note
> on dedupe-on-read.
>
> `ev.recommendations` and `ev.rejections` are **`MergeTree`**, so their counts are exact and
> permanent — 124 rejection rows really are 124 recorded facts across six runs.

Exact figures for `md.bars_1d`: 1,220,615 rows, `data_compressed_bytes` 36,199,087,
`data_uncompressed_bytes` 129,866,197, `bytes_on_disk` 37,119,437. **Compression ratio 3.59×.**

> **Correcting HANDOFF.md.** It reports 39.7 MiB and "~5.3× compression", derived from an assumed
> ~170 B/row raw size rather than from `data_uncompressed_bytes`. The measured ratio against
> ClickHouse's own uncompressed accounting is **3.59×** — comfortably inside the official 2.7–3.5×
> band, and nowhere near the 10–15× figure commonly quoted. Use 3.59× when sizing.
>
> The tiny tables with sub-1.0 ratios (`md.sector_daily` 0.93×, `md.breadth_daily` 0.23×) are not
> anomalies: a 3-row part carries more column header and index overhead than payload. They are
> noise at this scale.

**Extrapolation.** 2 years of full-market EOD costs ~35 MiB. Ten years is ~180 MiB. The whole panel
fits in RAM many times over; the 5 GiB ClickHouse cap
([TECH_STACK.md](TECH_STACK.md#13-memory-caps--the-defaults-would-have-killed-this-on-day-one)) is not a constraint on this data, it is a constraint to
protect the LLM's headroom.

---

## 4. `ev.recommendations` and `ev.rejections` — the two deliberate engine changes

Both were originally `ReplacingMergeTree`. Both were changed to **`MergeTree` with `run_id` in the
sort key**, and this is the most important schema decision in the project.

### The failure

From HANDOFF.md, bug #2:

> **`ev.recommendations` destroyed its own history.** `ReplacingMergeTree ORDER BY (d, arm,
> symbol)` meant re-running the job REPLACED the previous run's row for a symbol. The table whose
> purpose is "what did the system recommend on this date" was erased by recommending again.

ReplacingMergeTree collapses rows that share the full sort key, keeping the one with the highest
version column. With `ORDER BY (d, arm, symbol)` every re-run of the nightly job for the same
session produced rows with an identical sort key, so the previous run's recommendation for
`RELIANCE` on `2026-10-30` was destroyed by the act of recommending `RELIANCE` on `2026-10-30`
again. The audit property the table exists to provide was annihilated by normal operation, with no
error and no warning — the table simply always showed the latest run and looked correct.

This matters beyond tidiness. The learning loop scores what the system said *at the time*. A
corrected re-run silently overwriting the original means a bad call can be edited out of history
after the fact, which makes every downstream statistic unfalsifiable.

### The fix, as it sits in `schema_v2.sql`

```sql
-- MergeTree, NOT Replacing, and run_id is IN THE SORT KEY.
-- With ORDER BY (d, arm, symbol) under ReplacingMergeTree, re-running the
-- nightly job REPLACES the previous run's row for the same symbol instead of
-- appending a new one. The history of what the system recommended then is
-- destroyed by the act of recommending again -- which is precisely the
-- audit property this table exists to provide.
ENGINE = MergeTree()
PARTITION BY toYYYYMM(d)
ORDER BY (d, arm, run_id, symbol);
```

`ev.rejections` got the same treatment, for the same reason: *a rejection is a historical fact
about one run*.

```sql
ENGINE = MergeTree()
PARTITION BY toYYYYMM(d)
ORDER BY (d, run_id, symbol, rule_id);
```

Verified live: both are `MergeTree`, with those exact sorting keys.

### The cost you now carry

Append-only means **every reader must pin a run**, and unlike a Replacing table no background merge
will ever quietly clean up after a reader that forgets. `apps/api` does this with `_latest_run()`:

```sql
SELECT run_id FROM ev.recommendations
WHERE knowledge_time <= '<as_of>' AND d = '<session>'
GROUP BY run_id ORDER BY max(as_of) DESC LIMIT 1
```

`/api/screener` and `/api/recommendations` both call it. **`/api/rejections` does not**, and the
consequence is visible right now: `ev.rejections` holds **six** run_ids
(`c70802515a15`:26, `9e5e6029c1c8`:26, `65bd044013ec`:18, `563357aa749a`:18, `6b4a91345859`:18,
`42541a86b455`:18) and the endpoint returns all **124** rows merged — only **18 distinct
`(symbol, rule_id)` pairs**, some repeated **ten times**. See [API.md](API.md#apirejections).

Note that `ev.recommendations` holds only **one** run_id (`65bd044013ec`, 25 rows) while
`ev.rejections` holds six — consistent with the recommendations table having been dropped and
recreated when columns were added or the engine was changed, while `ev.rejections` kept its
history.

---

## 5. Table reference

Format: what one row means, then the columns, then notes. `kt` abbreviates
`knowledge_time DateTime64(3, 'UTC')`.

---

### `md.bars_1d` — daily OHLCV bars  **[PIT]**

**One row = one symbol's complete trading record for one NSE session**, as published in that
session's UDiFF bhavcopy.

```sql
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(d)
ORDER BY (symbol, d)
SETTINGS index_granularity = 8192
```

| Column | Type | Codec | Meaning |
| --- | --- | --- | --- |
| `d` | `Date` | | the trading session |
| `symbol` | `LowCardinality(String)` | | NSE ticker, e.g. `RELIANCE` |
| `isin` | `String` | | 12-char ISIN, e.g. `INE002A01018` |
| `series` | `LowCardinality(String)` | | `EQ` (rolling settlement) or `BE` (trade-to-trade) |
| `open`/`high`/`low`/`close` | `Float64` | `Delta, ZSTD(3)` | rupees, **unadjusted** |
| `prev_close` | `Float64` | `Delta, ZSTD(3)` | previous session's close as NSE states it |
| `last_price` | `Float64` | `Delta, ZSTD(3)` | last traded price; falls back to `close` if absent |
| `volume` | `UInt64` | `T64, ZSTD(3)` | shares traded |
| `turnover` | `Float64` | `Gorilla, ZSTD(3)` | **rupees** traded (`TtlTrfVal`). The liquidity metric that actually matters; free in the same file |
| `n_trades` | `UInt32` | `T64, ZSTD(3)` | executed trade count (`TtlNbOfTxsExctd`) |
| `deliv_qty` | `UInt64` DEFAULT 0 | `T64, ZSTD(3)` | **stubbed to 0** — the delivery file is not ingested |
| `deliv_pct` | `Float32` DEFAULT 0 | | **stubbed to 0** |
| `knowledge_time` | `DateTime64(3,'UTC')` | | 18:30 IST on `d`, in UTC |
| `source` | `LowCardinality(String)` DEFAULT `'nse_udiff'` | | provenance |
| `ingested_at` | `DateTime` DEFAULT `now()` | | ReplacingMergeTree **version column** |

**Live:** 1,220,615 rows · 3,197 symbols · 492 sessions · 2024-11-01 … 2026-10-30 · 24 parts.
Series split: `EQ` 1,115,047 / `BE` 105,568.

**Prices are raw and unadjusted, deliberately.** The schema comment: *adjustment factors live
separately so a late-arriving corporate action never silently rewrites history we already trained
on.* The adjustment layer does not exist yet
(see [LLD.md §9](LLD.md#no-corporate-action-adjustment)) — this is a live leakage vector.

**Codec choices.** `Delta` before `ZSTD` on prices because consecutive closes for a symbol differ
by a small amount, so delta-encoding turns a wide float into a narrow one. `T64` on integers
transposes the bit matrix so high-order zero bits compress. `Gorilla` on turnover is the
XOR-of-consecutive-floats scheme from Facebook's Gorilla TSDB, chosen because turnover is a
slowly-varying float where Delta would be a poor fit.

**`ORDER BY (symbol, d)` not `(d, symbol)`.** Every read in the system is "this symbol's trailing
window" or "the whole panel over a date range sorted by symbol". Symbol-major keeps one symbol's
history contiguous, which is what the factor stack scans.

**Dedupe on read is mandatory.** See [LLD.md §3.5](LLD.md#load_panel_inputs-and-the-dedupe-on-read--handoff-bug-1):
a duplicate `(symbol, d)` row shifts every trailing window by one and silently corrupts the entire
factor stack. `ReplacingMergeTree` only collapses on an unscheduled background merge.

---

### `md.index_members` — dated index membership snapshots  **[PIT]**

**One row = "on `snapshot_date`, `symbol` was a constituent of `index_name`, classified by NSE as
`industry`."**

```sql
ENGINE = ReplacingMergeTree()
PARTITION BY toYear(snapshot_date)
ORDER BY (index_name, snapshot_date, symbol)
```

| Column | Type | Meaning |
| --- | --- | --- |
| `snapshot_date` | `Date` | the day this membership list was observed |
| `index_name` | `LowCardinality(String)` | `NIFTY50` \| `NIFTY100` \| `NIFTY200` \| `NIFTY500` (10 known slugs) |
| `symbol`, `isin`, `company_name` | | from the niftyindices CSV |
| `industry` | `LowCardinality(String)` | **NSE's official taxonomy** — the heat map's grouping |
| `knowledge_time` | | `now()` at fetch |

**Live:** 851 rows, a single snapshot dated **2026-10-31**: NIFTY50 50, NIFTY100 100, NIFTY200 200,
NIFTY500 501.

**Dated snapshots, never "current membership."** This is leakage vector #2 from `asof.py`'s
docstring. NSE churns constituents constantly; applying today's Nifty 200 to a 2024 backtest means
the survivors look like the whole universe and returns are materially inflated. `snapshot_date` is
in the sort key precisely so two snapshots cannot collide.

The raw bytes are archived **day-granular** (`data/raw/nse_constituents/YYYY/MM/DD/`) with
`reuse=False`, because the live CSV is overwritten by NSE and a monthly directory would let two
snapshots in one month overwrite each other.

**Reading it correctly** means taking the latest snapshot at or before the target date per
`(index_name, symbol)` — `load_panel_inputs` does this with
`sort("snapshot_date").group_by(["index_name","symbol"]).last()`.

**Consequence of having exactly one snapshot:** a *replay* at an `as_of` earlier than
2026-10-31 sees **zero** members, which is correct PIT behaviour and makes replay currently
unusable. See [LLD.md §3.5](LLD.md#live-as-of-vs-replay-as-of--the-bug-the-separation-caught).

---

### `md.sector_daily` — pre-aggregated industry rollup  **[PIT]**

**One row = one industry's aggregate behaviour on one session**, over the scored universe.

```sql
ENGINE = ReplacingMergeTree()
PARTITION BY toYear(d)
ORDER BY (d, industry)
```

| Column | Type | Meaning / units |
| --- | --- | --- |
| `d` | `Date` | session |
| `industry` | `LowCardinality(String)` | NSE taxonomy; never `Unclassified` (filtered at write) |
| `n_symbols` | `UInt16` | constituents contributing |
| `turnover` | `Float64` | **rupees** summed across the industry |
| `cap_weighted_ret` | `Float32` | turnover-weighted mean `ret_1d`, **fraction** (−0.0096 = −0.96%) |
| `median_ret` | `Float32` | median `ret_1d`, fraction |
| `breadth_pct` | `Float32` | share of constituents with `ret_1d > 0`, **fraction** |
| `adv_count` / `dec_count` | `UInt16` | advancers / decliners |
| `move_z` | `Float32` | `cap_weighted_ret` z-scored **across industries that session** |
| `rs_21d` | `Float32` | median 21-day return, fraction |
| `knowledge_time` | | `= as_of` of the run |

**Live:** 20 rows, 20 distinct industries, 1 part, session 2026-10-30 only. (It read 60 rows across
**3 unmerged parts** — one per nightly run — earlier in the same session, before a background merge
collapsed them.)

**Exists so `/api/heatmap` is one SELECT.** The M1 gate is p95 < 30 ms, unachievable with a
per-request 500-stock computation. The ClickHouse side delivers: a 40-row scan measures **2.64 ms**
`elapsed`.

`cap_weighted_ret` is weighted by **turnover**, not market cap (there is no market-cap source), so
a thin stock's 15% print cannot move the sector tile the way an equal-weight mean would.
`median_ret` is stored alongside precisely so the two disagreeing is visible.

> Those 3 unmerged parts were why `/api/heatmap` returned duplicate tiles and silently dropped six
> industries. The merge has since hidden the symptom; the endpoint still does not dedupe on read,
> so it will recur on the next multi-run night. See [API.md](API.md#apiheatmap).

---

### `md.breadth_daily` — market breadth  **[PIT]**

**One row = the breadth of one named universe on one session.**

```sql
ENGINE = ReplacingMergeTree()
ORDER BY (d, universe)
```

| Column | Type | Meaning |
| --- | --- | --- |
| `d` | `Date` | session |
| `universe` | `LowCardinality(String)` | `NIFTY500` market/tradeable breadth by default; other configured universes are separate rows |
| `n_symbols` | `UInt16` | members measured |
| `advancers` / `decliners` | `UInt16` | counts; `ret_1d > 0` vs `<= 0` (flat counts as a decline) |
| `pct_above_sma50` | `Float32` | **fraction** of members above their 50-day SMA |
| `pct_above_sma200` | `Float32` | fraction above 200-day SMA |
| `ret_dispersion_xs` | `Float32` | cross-sectional **standard deviation** of `ret_1d`, fraction |
| `median_ret` | `Float32` | fraction |
| `knowledge_time` | | `= as_of` |

Each run stores one row per distinct market-context or tradeable universe. By default, both use
NIFTY500 and share one row; a narrower configured tradeable universe gets its own row.

**`universe` is a KEY column, not an attribute**, and the schema comment says why: *breadth over
the Nifty 200 and breadth over the whole market are different facts and must never be mixed.*
Putting it in the sort key makes mixing them structurally impossible.

The NIFTY500 row is the input to the regime heuristic (`infer_regime` / `_regime_from_breadth`),
which gates five risk quantities. When a narrower tradeable universe is configured, its separate
breadth row describes that subset but does not determine the overall market regime.

---

### `md.trend_1d` — multi-timeframe trend states  **[PIT]**

**One row = one symbol's trend classification and its supporting statistics on one session.**

```sql
ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(d)
ORDER BY (symbol, d)
```

| Column | Type | Meaning |
| --- | --- | --- |
| `d`, `symbol` | | |
| `trend_20` / `trend_63` / `trend_252` | `LowCardinality(String)` | `TrendState` member at each lookback, **after hysteresis** |
| `t_drift_20/63/252` | `Float32` | **Newey-West HAC t-stat** of mean daily log return. Not an OLS slope t-stat — see [LLD.md §3.2](LLD.md#why-a-newey-west-hac-variance-instead-of-an-ols-t-stat) |
| `er_20/63/252` | `Float32` | Kaufman efficiency ratio, [0,1]; 1.0 = straight line |
| `adx14` | `Float32` | Wilder-smoothed ADX, 0–100 |
| `adx_delta_10` | `Float32` | `adx14 − adx14[t−10]`; negative = trend decaying |
| `flips_63d` | `UInt16` | `trend_63` state changes in a rolling 63 sessions |
| `rs_vs_bench_63` | `Float32` | **always 0.0** — written as a literal by `persist`; no benchmark series exists |
| `trend_enum_version` | `UInt16` | `TREND_ENUM_VERSION`, currently 1. Rows at an older version must be excluded from posterior updates |
| `knowledge_time` | | `= as_of` |

**Live:** 2,919 rows, 1 part, session 2026-10-30 only. (8,757 across 3 parts before the merge.)

The panel is ~2,919 scored symbols × 3 runs. Only the latest session is persisted per run —
`persist` writes `panel.filter(d == res.d)`, so this table grows one session at a time going
forward and **has no history before the first run**.

---

### `md.features_1d` — the factor panel  **[PIT]** · EMPTY

**Intended: one row = one symbol's full factor vector on one session.** 30 columns covering
returns at 6 horizons, `mom_12_1`, three SMAs, `ma_stack`, `atr14`, `realised_vol_21d`, `rsi14`,
`adx14`, `bb_z`, 52-week distances, `rvol_20`, `adtv_20`, `turnover`, `deliv_pct`, two relative-
strength columns and `liquidity_ok`.

```sql
ENGINE = ReplacingMergeTree() PARTITION BY toYYYYMM(d) ORDER BY (symbol, d)
```

**Live: 0 rows.** Factors are computed in memory by `add_price_factors` on every run and never
persisted. `ta features`, the CLI command that would fill it, raises `ImportError`.

Consequence: **re-deriving a past date's factors means re-running the whole pipeline**, and there is
no stored record of what a factor value was when a decision used it. For the deterministic arm that
is recoverable (the bars are immutable); for anything that wants to ask "what did `rvol_20` look
like the night we bought this?" it is not.

---

### `md.corporate_actions` — splits, bonuses, dividends  **[PIT]** · EMPTY

**Intended: one row = one corporate action on one symbol.**

```sql
ENGINE = ReplacingMergeTree() ORDER BY (symbol, ex_date, action_type)
```

`symbol, ex_date, action_type (SPLIT|BONUS|DIVIDEND|RIGHTS), ratio_from, ratio_to, dividend,
adj_factor, announce_date, kt`.

**Live: 0 rows, no producer.** `announce_date` vs `ex_date` vs `knowledge_time` is a three-axis
problem and the schema is right to separate them.

Until this is populated and an adjustment layer consumes it, **leakage vector #3 is live**: a 1:2
split appears in `md.bars_1d` as an unexplained −50% single-day return, and `ret_1d`, `mom_12_1`,
`bb_z`, the HAC drift statistic and every resampled barrier path will treat it as a real move.

---

### `md.surveillance` — ASM / GSM / ESM / T2T / ban-period flags  **[PIT]** · EMPTY

**Intended: one row = "on session `d`, `symbol` was on surveillance list `list_type` at `stage`."**

```sql
ENGINE = ReplacingMergeTree() PARTITION BY toYYYYMM(d) ORDER BY (d, symbol, list_type)
```

`list_type` ∈ `ASM | GSM | ESM | T2T | BAN`.

- **ASM** (Additional Surveillance Measure) and **GSM** (Graded Surveillance Measure) are
  exchange/SEBI regimes imposing extra margin, narrower price bands or periodic call auctions on
  names showing abnormal price or volume behaviour. A name entering ASM is a liquidity cliff.
- **ESM** is the small-cap equivalent.
- **T2T** (trade-to-trade) forbids intraday netting — every trade must be delivered.
- **BAN** is the F&O market-wide position-limit ban period.

**Live: 0 rows, no producer.** `RISK-12` reads `Candidate.under_surveillance`, which the pipeline
never sets, so it is always `False`. **ASM/GSM names are not currently excluded** — the rule fires
only on its `series != "EQ"` branch (4 of the 124 recorded rejections, all `series=BE`).

---

### `md.bars_1m` — minute bars  **[PIT]** · EMPTY

```sql
ENGINE = ReplacingMergeTree() PARTITION BY toYYYYMM(ts) ORDER BY (symbol, ts)
```

`ts DateTime64(0, 'Asia/Kolkata')`, OHLCV, `kt`, `source` DEFAULT `'broker'`. Note the timestamp is
stored in **IST**, unlike every `knowledge_time` which is UTC — minute bars are read by humans
against an IST session clock. M10; needs a broker API.

---

### `md.benchmark_1d` — index series  **[PIT]** · EMPTY

```sql
ENGINE = ReplacingMergeTree() ORDER BY (index_id, series_kind, d)
```

`index_id, series_kind (TRI|PRICE), close, ret_1d, tri_source (PUBLISHED|RECONSTRUCTED), kt`.

**TRI** = Total Return Index, which reinvests dividends. Benchmarking against the price index
instead flatters you by roughly 1.2%/yr. `series_kind` is in the **sort key** so the two can coexist
without being confused, and `tri_source` records whether the series was published by NSE or
reconstructed locally.

**Live: 0 rows.** `/api/context` hard-codes `{"name": "NIFTY 50 TRI", "close": null, "ret_1d":
null, "ret_21d": null, "tri_source": "RECONSTRUCTED"}`. There is currently **nothing to benchmark
the equity curve against**, which blocks the M3 gate.

---

### `nx.news_features` — per-document news features  **[PIT]** · EMPTY

```sql
ENGINE = ReplacingMergeTree() PARTITION BY toYYYYMM(ts) ORDER BY (symbol, ts, doc_id)
```

| Column | Meaning |
| --- | --- |
| `ts` | **DISSEMINATION time, never crawl time** — the schema comment says so |
| `symbol`, `doc_id`, `source`, `source_tier` | |
| `event_type`, `sentiment`, `relevance`, `novelty`, `is_duplicate` | |
| `headline`, `url` | |
| `latency_sec` | `Int32` — gap between dissemination and our knowledge |
| `knowledge_time` | when *we* learned it |

`ts` vs `knowledge_time` is the three-axis problem made explicit: an exchange announcement
disseminated at 14:02 that our poller sees at 14:07 has `ts = 14:02`, `knowledge_time = 14:07`,
`latency_sec = 300`. A decision at 14:05 may not see it. Getting this backwards is leakage vector
#1.

M7. `nx.filings` and `nx.events` are registered in `PIT_TABLES` and do not exist.

---

### `fn.statements` — financial statements  **[PIT]** · EMPTY

```sql
ENGINE = ReplacingMergeTree() ORDER BY (symbol, period_end, consolidated)
```

`period_type` ∈ `Q | H | A` (quarterly, half-yearly, annual — Indian listed companies report
quarterly). `consolidated` `UInt8` distinguishes consolidated from standalone, and is **in the sort
key** because mixing them is a classic fundamentals error (a holding company's standalone revenue
is meaningless).

P&L (`revenue … pat, eps`), balance sheet (`equity, reserves, borrowings, total_assets`), cash flow
(`cfo, capex`).

**`filed_at` is "when it became PUBLIC"**, distinct from `period_end` (what period it describes) and
`knowledge_time` (when we learned it). Three axes again. A Q2 result for `period_end = 2026-09-30`
filed on `2026-11-12` must be invisible to any decision before 2026-11-12, no matter what the
period says.

### `fn.shareholding` — ownership  **[PIT]** · EMPTY

`promoter_pct, promoter_pledge, fii_pct, dii_pct, public_pct` + `filed_at` + `kt`.
`ORDER BY (symbol, period_end)`.

**Promoter** is the Indian term for a controlling founder/family/parent shareholder.
**Promoter pledge** — shares pledged as loan collateral — is a well-known distress signal.
**FII/DII** are Foreign and Domestic Institutional Investors; the split is published and widely
watched.

`fn.ratios` and `fn.estimates` are registered in `PIT_TABLES` and do not exist.

---

### `ev.recommendations` — the night's trade plans  **[PIT]**

**One row = one candidate's complete, immutable trade plan as produced by one run on one session.**

```sql
ENGINE = MergeTree()                        -- NOT Replacing; see §4
PARTITION BY toYYYYMM(d)
ORDER BY (d, arm, run_id, symbol)
```

| Column | Type | Meaning / units |
| --- | --- | --- |
| `as_of` | `DateTime64(3,'UTC')` | the decision instant, **recorded not re-derived** |
| `run_id` | `String` | 12 hex chars; `uuid4().hex[:12]` |
| `rec_id` | `String` | `{run_id}-{symbol}`, or `{run_id}-{symbol}-ALT` for a displaced alternate |
| `d` | `Date` | the session the decision was made from |
| `symbol`, `name`, `industry` | | |
| `arm` | `LowCardinality(String)` | always `DETERMINISTIC` today; the four-arm comparison is unbuilt |
| `action` | `LowCardinality(String)` | `Action` member — see [LLD.md §1.2](LLD.md#action-and-the-three-portfolio-only-members) |
| `sleeve`, `horizon` | | `Sleeve` / `Horizon` member |
| `entry_lo`, `entry_hi` | `Float64` | rupee entry band, `close × 0.995 / 1.005` |
| `stop`, `target` | `Float64` | rupees. `close − 1.5·ATR` / `close + 2.0·ATR` |
| `qty` | `UInt32` | **shares actually advised. 0 on a WATCH card.** Gated against the *running* book |
| `notional` | `Float64` | **rupees actually advised. 0.0 on a WATCH card** |
| `would_qty` | `UInt32` DEFAULT 0 | **affordability**: the most you could buy of this name against your *real* book. Independent of rank |
| `would_notional` | `Float64` DEFAULT 0 | ditto, rupees |
| `would_risk_rupees` | `Float64` DEFAULT 0 | ditto, rupees at risk to the stop |
| `afford_note` | `String` DEFAULT `''` | why that affordability figure, in words — e.g. `"risk 0.48% of equity, stop 3.2%"`, or `"{rule_id}: {reason}"` when the name is not affordable at all, or `"displaced by RISK-06"` on an alternate |
| `risk_rupees` | `Float64` | `(entry − stop) × qty`; 0 on WATCH |
| `risk_pct` | `Float32` | **fraction** of equity (0.0079 = 0.79%) |
| `rr` | `Float32` | reward:risk ratio, `(target−price)/(price−stop)`; ≈1.33 by construction |
| `atr14` | `Float64` | rupees |
| `cost_bps` | `Float32` | full round-trip cost in **basis points** of buy turnover |
| `expectancy_atr` | `Float32` | **ATR units, after costs**; may be negative and usually is |
| `composite` | `Float32` | sector-neutral composite score (z-units) |
| `reasons` | `Array(String)` | closed `ReasonCode` members, ≥1 |
| `barrier_state` | `LowCardinality(String)` | `OK` \| `UNAVAILABLE` |
| `p_up`/`p_dn`/`p_vert` | `Nullable(Float32)` | the Dirichlet triad, **fractions summing to 1**. NULL iff `UNAVAILABLE` |
| `ci_lo`/`ci_hi` | `Nullable(Float32)` | 90% credible interval **around `p_up` only** |
| `n_eff` | `Float32` | Kish effective sample size behind the triad |
| `barrier_reason` | `String` | geometry + the no-demonstrated-edge arithmetic |
| `knowledge_time` | | `= as_of` |

**Live:** 25 rows, 1 run (`65bd044013ec`), session 2026-10-30, all `action = WATCH`.

The comment on `qty` in the schema is the clearest statement of the WATCH contract:

> ACTUAL size being advised. Zero on a WATCH card: you are not being told to buy it, and a card
> that shows both "do not buy" and a share count is the exact ambiguity this split exists to
> remove.

and on `would_qty`:

> What the size WOULD have been had it been actionable. Kept so a WATCH card can still show the
> shape of the trade, in muted type, without implying an instruction.

**`qty` and `would_qty` come from two separate `evaluate_entry` calls, differing in one argument.**
`qty` is gated against the **running** book as cards are accepted, so a set of BUY cards is jointly
takeable. `would_qty` is gated against the **real, unchanged** portfolio, so it answers *"how much
of this could I buy?"* and does not shrink as higher-ranked cards consume a hypothetical book. The
pipeline comment records why collapsing them was wrong: *the same stock was "worth Rs 74k" at rank
3 and "Rs 0" at rank 20, for reasons invisible on the card.* See
[LLD.md §3.4](LLD.md#two-sizings-because-they-answer-two-different-questions).

**The barrier triad is stored as RECORDED INPUTS so a replay cannot recompute it.** That is the
`Nullable` on `p_up`/`p_dn`/`p_vert`/`ci_lo`/`ci_hi` doing real work: NULL is distinguishable from
0.0, and `UNAVAILABLE` cannot degrade into "we estimated zero".

> **Note on physical column order.** The live table has `would_qty`, `would_notional`,
> `would_risk_rupees` and `afford_note` as the **last four columns**, i.e. they were added by
> `ALTER TABLE ... ADD COLUMN`, whereas `schema_v2.sql` declares them mid-table. Harmless —
> `insert_arrow` matches by name — but a fresh `apply_schema.sh` on a new machine produces a
> different physical layout than the running one.

---

### `ev.rejections` — the rejection log  **[not PIT]**

**One row = "`run_id` rejected `symbol` on session `d` because `rule_id` fired."**

```sql
ENGINE = MergeTree()                        -- NOT Replacing; see §4
PARTITION BY toYYYYMM(d)
ORDER BY (d, run_id, symbol, rule_id)
```

`as_of, run_id, d, symbol, rule_id, reason, detail`. No `knowledge_time` — a rejection is scoped by
`run_id`, so it carries no separate knowledge axis.

**Live:** 124 rows across **6** run_ids, session 2026-10-30. Breakdown of what the endpoint
returns — note these are **summed over all six runs**, because `/api/rejections` does not pin a
run (see §4):

| `rule_id` | `reason` | Rows returned | Distinct symbols |
| --- | --- | ---: | ---: |
| `TRD-05` | `WHIPSAW` | 108 | 12 |
| `RISK-01` | `BELOW_MIN_NOTIONAL` | 12 | 2 |
| `RISK-12` | `SURVEILLANCE` | 4 | 4 |

Only **18 distinct `(symbol, rule_id)` pairs** underlie those 124 rows; some repeat ten times.
Per-run counts are 26/26/18/18/18/18, and the latest run (`65bd044013ec`) contributed 18.

**This table is the point.** From `schema_v2.sql`:

> Every rejected candidate with the rule that killed it. This exists so the gates are auditable
> rather than invisible: "the cap, not the signal, rejected this" is a thing the user must be able
> to see.

`detail` carries the arithmetic, e.g. `"Rs 49,880 < Rs 50,000 floor (DP charge would be 3.1 bps)"`
and `"series=BE surveillance=False"`.

`rule_id` values include non-RISK prefixes: `DATA` (no ATR), `TRD-03` (decaying trend blocks a
trend-following sleeve), `TRD-05` (whipsaw) as well as every `RISK-*` from `evaluate_entry`.

---

### `ev.ledger_events` — the hash-chained ledger  **[not PIT]** · EMPTY

```sql
ENGINE = ReplacingMergeTree() ORDER BY seq
```

`seq UInt64, ts, run_id, kind, payload String, prev_hash String, hash String`.

> Append-only, hash-chained. `prev_hash` is the previous row's `hash`, so any edit to history
> invalidates every row after it. **Canonical JSON, never pickle**: a record that must load in 2027
> cannot depend on today's classes.

`payload` is a `String` holding canonical JSON rather than a typed structure, and that is
deliberate: ADR-0002 requires the record to be readable after a Pydantic model is renamed or
re-typed.

**Live: 0 rows, no writer.** This is the gap that blocks everything downstream — see
[LLD.md §9](LLD.md#no-ledger-no-broker-nothing-resolves).

---

### `ev.positions` — the open book  **[not PIT]** · EMPTY

```sql
ENGINE = ReplacingMergeTree(as_of) ORDER BY (symbol, opened)
```

`as_of, symbol, industry, qty, entry, last, stop, target, opened, horizon, sleeve, status, pnl`.

`ReplacingMergeTree(as_of)` is correct here and *not* a repeat of the §4 mistake: this table holds
**current state**, so collapsing to the newest version per `(symbol, opened)` is the intended
semantics. The history lives in `ev.ledger_events`.

`status` carries `OPEN`; both `nightly.current_portfolio` and `api._portfolio` filter
`WHERE status = 'OPEN'` and both catch the exception if the table is missing, returning a flat book.

**Live: 0 rows.** Both readers therefore return `equity = cash = 500,000`, zero positions, zero
heat, zero drawdown.

---

### `ev.equity_daily` — per-arm equity curve  **[not PIT]** · EMPTY

```sql
ENGINE = ReplacingMergeTree() ORDER BY (arm, d)
```

`d, arm, equity, cash, deployed, heat_pct, drawdown, watermark`. `arm` is in the sort key so the
four arms' curves are stored side by side and compared directly.

`/api/simulation` counts this table and refuses below **750 engine-days**.

---

### `ev.outcomes` — resolved recommendations  **[PIT]** · EMPTY

```sql
ENGINE = ReplacingMergeTree() ORDER BY (rec_id)
```

| Column | Meaning |
| --- | --- |
| `rec_id` | joins back to `ev.recommendations` |
| `entry_date`/`entry_price`/`exit_date`/`exit_price` | |
| `barrier_hit` | `BarrierHit` member: `UP`\|`DOWN`\|`VERTICAL`\|`CANCELLED`\|`OPEN` |
| `r_multiple` | P&L in units of initial risk |
| `pnl_rupees` | |
| `mae_r` / `mfe_r` | Maximum Adverse / Favourable Excursion, in R |
| `holding_days` | `UInt16` |
| `benchmark_ret` | the benchmark's return over the same window |
| `stated_conf` | the model's raw stated confidence |
| `calibrated_p` | the isotonic-mapped probability. **Sizing uses this, never `stated_conf`** |
| `uniqueness_w` | `1/(concurrent overlapping labels)`; overlapping windows are not IID |
| `regime`, `degraded_mode` | recorded context |

`/api/calibration` counts this table and refuses below **30** resolved rows.
`/api/simulation` additionally requires **200**.

### `ev.attribution` — P&L exploded per reason code  **[PIT]** · EMPTY

```sql
ENGINE = ReplacingMergeTree() ORDER BY (reason_code, resolved_date, rec_id)
```

**One row per `(rec_id, reason_code)` pair**, so a recommendation carrying three reason codes
produces three rows. `weight` splits the P&L across them. `reason_code` leads the sort key because
every attribution query is "how has `MA_STACK_ALIGNED` performed".

This is why `ReasonCode` is a **closed** enum: you cannot build a posterior over a category that
never repeats.

### `ev.posteriors` — Beta-Binomial posteriors  **[PIT]** · EMPTY

```sql
ENGINE = ReplacingMergeTree() ORDER BY (as_of, reason_code, regime, horizon)
```

`alpha, beta, p_hat, n_eff, gated UInt8, expectancy_r`. `gated` records whether this posterior had
earned the right to touch sizing (`n_eff >= 30`); below that it is display-only. Storing the gate
decision rather than recomputing it is the same recorded-inputs discipline as the barrier triad.

---

## 6. MongoDB

**Running:** v9.0.4, single-node replica set `rs0` (PRIMARY), `127.0.0.1:27017`, WiredTiger cache
capped at **2.00 GB** (verified: `maximum bytes configured = 2147483648`). Config at
`infra/mongo/mongod.conf`, data at `data/mongo/` (**4.4 MB**).

**Role, as designed:** the document store for anything whose shape is not a fixed numeric row —
raw news articles, extracted markdown, filing text, dossiers, LLM generation records.

**Role, as built: none.** Verified by `listDatabases`: only `admin`, `config` and `local` exist. No
application database, no collections, **zero application documents**. `pymongo` is a dependency of
`ta-datafeed` and is imported in exactly one place: `apps/api/main.py`'s `/api/health` handler,
which pings it so the dashboard can show a service chip.

Two settings are load-bearing and both are explained in `mongod.conf`:

- **`cacheSizeGB: 2`.** WiredTiger's default is `max(0.5 × (RAM − 1 GB), 256 MB)` = **23.5 GB** on
  this 48 GB machine. That alone would make a 22 GB model unloadable. The failure is silent: Mongo
  starts happily and simply grows.
- **`replSetName: rs0`.** A replica set is **required**, not optional: change streams, transactions
  and `mongodump --oplog` all need one, even a single-node one. Retrofitting a replica set onto a
  standalone deployment is an operation; starting as one costs nothing.

---

## 7. Redis

**Running:** v8.8.0 (pre-existing Homebrew install), `127.0.0.1:6379`.

**Role, as designed** (PLAN.md): exactly three jobs — a GPU-mutex backstop, per-host token buckets,
and pub/sub feeding SSE to the dashboard.

**Role, as built: none.** There is no `redis` Python client in `uv.lock`. Nothing in the codebase
connects to it except `/api/health`, which opens a bare TCP socket to port 6379 with a 400 ms
timeout to light the service chip. The token bucket that *does* exist is in-process
(`ta_datafeed.http._Bucket`, a `threading.Lock`), which is correct for a single process and will
need Redis the moment there are two.

Redis is **not** managed by `infra/services.sh` (`up`/`down` handle ClickHouse and mongod only);
`status` merely reports `redis-cli ping`. There is no launchd agent for it. It is assumed to be
running from a pre-existing `brew services` install.

---

## 8. Filesystem stores

| Path | Size | Contents |
| --- | ---: | --- |
| `data/raw/` | **86 MB, 497 files** | immutable source bytes. `nse_bhavcopy/YYYY/MM/`, `nse_constituents/YYYY/MM/DD/`, `nse_master/`. Filenames are `{sha256(url)[:16]}_{basename}` |
| `data/clickhouse/` | **37 MB** | ClickHouse `path`, `tmp_path`, `user_files`, access control, preprocessed configs |
| `data/mongo/` | **4.4 MB** | WiredTiger files; application data is empty |
| `data/logs/` | **7.5 MB** | all service and job logs — see [OPERATIONS.md](OPERATIONS.md#5-logs) |

**`data/raw/` is the recovery substrate.** Governing principle 3: *raw bytes are immutable; written
before anything parses them. Everything else is reconstructible.* Concretely — if the UDiFF parser
has a bug, or NSE changes a column name, the fix is a re-parse from these 497 files, not 497 HTTP
requests to an exchange whose Terms of Use prohibit systematic collection. **This directory is the
one thing in the repo that cannot be regenerated.** Back it up accordingly; see
[OPERATIONS.md](OPERATIONS.md#7-backup-and-restore).
