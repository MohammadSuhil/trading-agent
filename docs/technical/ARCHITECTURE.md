# Architecture — high-level design

**Audience:** an engineer picking this repo up. Read [CLAUDE.md](../../CLAUDE.md) for the
governing principles and [PLAN.md](../PLAN.md) for the full reasoning; this document is the
shape of the system and the handful of decisions that determine everything else.

Companion documents: [LLD.md](LLD.md) · [DATA_MODEL.md](DATA_MODEL.md) · [API.md](API.md) ·
[TECH_STACK.md](TECH_STACK.md) · [OPERATIONS.md](OPERATIONS.md) · [TESTING.md](TESTING.md) ·
[diagrams/](diagrams/)

---

## 1. What this is

A local, single-user system that screens Indian equities (NSE/BSE) nightly, produces ranked
trade plans with explicit stops, targets and sizes, paper-trades them, scores the outcomes, and
is built so that it can prove whether it is actually any good.

Everything runs on one laptop. No cloud, no vendor telemetry, no data egress.

**What it is not.** It is not a trading bot — nothing places real orders, and the broker adapter
is a seam, not an implementation. It is not fast — the nightly run takes about ten seconds and
is scheduled once a day. And it is not, yet, a system with demonstrated edge; see §7.

---

## 2. The five decisions everything else follows from

These are the load-bearing ones. If you understand only these, you can predict most of the rest
of the design.

### 2.1 The LLM never computes a number

Every price, indicator, stop, target, size, cost and probability is produced by Python or
ClickHouse and recorded. When the LLM layer arrives (milestone M4+), it will select among
pre-validated candidates, explain them, assign a closed-enum reason code, state a confidence and
be allowed to refuse. It will never widen a stop or invent a probability.

**Consequence:** the API computes nothing either. It selects and serialises what the nightly run
decided. A number that appears on the dashboard can always be traced to the run that produced
it, which is what makes the audit trail possible at all.

### 2.2 `knowledge_time` ≠ event date ≠ crawl time

Every point-in-time table carries a `knowledge_time`: the instant a row *became knowable*. All
reads go through [`ta_core.asof`](../../packages/core/src/ta_core/asof.py), which refuses an
unbounded read, refuses a bound that is evaluated at query time (`knowledge_time <= now()` moves
with the wall clock), and — when given the run's as-of — refuses a bound to the *wrong* instant.

This is the single most important module in the repo. The failure it prevents does not raise, does
not look wrong, and produces a beautiful backtest; you discover it months later when live results
diverge.

**Consequence:** the decision instant and the bar date are separate axes. A live run on Monday
evening legitimately sees an index-membership snapshot taken Monday morning while using Friday's
closing bars. Conflating the two was the first bug this separation caught.

### 2.3 Raw bytes are immutable

`data/raw/YYYY/MM/DD/` is written *before* anything parses. NSE changes CSV headers without
notice; when the parser breaks, the fix is a re-parse, not a re-download. Everything downstream
is reconstructible from the raw archive.

Note the deliberate split in [`ta_datafeed.http`](../../packages/datafeed/src/ta_datafeed/http.py)
between `reuse` (read from the disk cache) and `archive` (write to it). A past bhavcopy never
changes, so it is both. An index constituent list *does* change, so it must never be reused — but
it must still be archived, or the dated membership snapshot is gone the moment NSE updates the
file. Principle 3 is "raw bytes are immutable", not "raw bytes are cached".

### 2.4 The evaluation harness exists before the strategy; the strategy before the model

Milestones 1–3 contain zero LLM calls. The frozen control arm, the cost model, the hash-chained
ledger and point-in-time correctness all come first.

This is the principle that serves the stated objective. "It learns and gets better" is an
*unfalsifiable* claim unless there is a frozen baseline to beat and every decision is
reconstructible — and retrofitting an audit trail is impossible, because the data you needed was
never stored.

### 2.5 Orchestration is plain Python

No agent framework. The reasoning is argued at length in
[ADR-0001](../ADR/0001-plain-python-orchestration.md) and is *not* the usual "frameworks are
bloat": the real argument is that the generation record is mandatory regardless, and once it
exists it already is a step journal, so a framework's checkpointer is a second durable store of
the same facts without a hash chain.

The decisive measurement: after fixing a persona that emitted a bad number, resuming under
LangGraph served the **stale** value into a green run with no warning, and resuming under DBOS
replayed the recorded **error** so the fix was invisible. Plain Python re-runs current code every
time. Having no resume is a safety property here.

---

## 3. Shape

```
                    NSE published archives            niftyindices.com
                 (bhavcopy .csv.zip, EQUITY_L)        (index constituents)
                              │                              │
                              ▼                              ▼
                      ┌───────────────────────────────────────────┐
   data/raw/ ◀────────│  ta_datafeed  — polite HTTP, parse, store │
   (immutable)        └───────────────────────────────────────────┘
                              │
                              ▼
                      ┌─────────────────┐        ┌──────────────┐
                      │   ClickHouse    │        │   MongoDB    │  documents (M7+)
                      │ md.* nx.* fn.*  │        │   Redis      │  mutex, buckets
                      │ ev.* ref.*      │        └──────────────┘
                      └─────────────────┘
                              │   reads go through ta_core.asof (PIT-guarded)
                              ▼
     ┌──────────────────────────────────────────────────────────────┐
     │ ta_quant — factors → trend → sector → screen → barriers      │
     │ ta_ledger — capital-protection gates → sizing                │
     │            (sequential against a GROWING book)               │
     └──────────────────────────────────────────────────────────────┘
                              │  writes ev.recommendations, ev.rejections
                              ▼
                      ┌─────────────────┐
                      │  ta_api         │  one SELECT per endpoint, no computation
                      │  + apps/ui dist │  served from the same process
                      └─────────────────┘
                              │
                              ▼
                   http://127.0.0.1:8000
```

See [diagrams/](diagrams/) for C4 context/container views and the nightly sequence.

### Package responsibilities

| Package | Responsibility | I/O |
| --- | --- | --- |
| `packages/core` (`ta_core`) | Closed enums, the Indian cost model, NSE calendar, date normalisation, **the point-in-time guard**, the plain-English glossary | **none** — stdlib + pydantic only |
| `packages/datafeed` (`ta_datafeed`) | Polite rate-limited HTTP, raw archiving, UDiFF/constituent parsing, the guarded ClickHouse client | network, disk, ClickHouse |
| `packages/quant` (`ta_quant`) | Price factors, trend states, sector rollups, cross-sectional scoring, barrier probabilities, the nightly pipeline | pure frames in/out (`pipeline`), ClickHouse (`nightly`) |
| `packages/ledger` (`ta_ledger`) | Capital-protection rules and sizing. *Hash-chained events and the paper broker are not written yet* | pure (currently) |
| `packages/cli` (`ta_cli`) | Operator commands (`ta daily`, `ta status`, `ta ingest`, …) | orchestration only |
| `apps/api` (`ta_api`) | Read model; serves the SPA | ClickHouse reads |
| `apps/ui` | React dashboard | HTTP only |

`packages/core` having **zero I/O** is enforced by convention and is worth preserving: it is what
lets the cost model and the PIT guard be doctested without a database.

---

## 4. The nightly run

One command, `ta daily`, ~10 s incremental. Stages are independent: a failed constituents refresh
must not cost you the day's bars.

1. **Ingest** — fetch bhavcopies for the window; a 404 means a holiday, which is an *answer*, not
   an error, and is in fact the most reliable trading-calendar source available (it is the
   exchange's own output). Raw bytes land before parsing.
2. **Symbol master + dated index membership** — membership is snapshotted with a date, never
   applied retroactively. Using today's constituents for a past date is the survivorship bias
   that makes every backtest look brilliant.
3. **Factors and trend** — the whole ~2,900-symbol × 420-session panel, vectorised in polars,
   well under a second. Trend uses a Newey-West-corrected drift t-stat, not an OLS slope on
   log price: the residuals of the latter are badly autocorrelated and almost every random walk
   scores "significant".
4. **Sector rollup and breadth** — pre-aggregated so the heat map is one SELECT.
5. **Screen** — hard gates (liquidity, surveillance, geometry) run *before* scoring so an
   untradeable name never consumes ranking budget; then a sector-neutralised composite.
6. **Barrier probabilities** — empirical, resampled from real ATR-standardised forward paths.
7. **Risk gates and sizing** — sequential, against a growing book (§5).
8. **Record** — plans *and* rejections, because a gate nobody can see is a gate nobody trusts.

### Live vs replay

`execute_nightly(replay=False)` (default) pins the decision instant to **now**.
`replay=True` pins it to that session's 18:30 IST, reconstructing only what was knowable then.
Per [ADR-0002](../ADR/0002-replay-from-record.md), replay must **re-derive, never recall**: a
replay that returns the stored answer passes the gate while proving nothing.

---

## 5. Capital protection

28 rules across three enforcement points — a **GATE** before sizing (a failure means the
candidate never reaches the dossier), **SIZING** clamps that change the number but keep the
trade, and post-hoc **BREAKERS** that can halt new entries. Full table in
[PLAN.md](../PLAN.md#capital-protection--the-rules-that-decide-the-size); implementation in
[`ta_ledger/risk.py`](../../packages/ledger/src/ta_ledger/risk.py).

**The arithmetic that sets the shape of the book:**

```
₹5,00,000 equity
flat ₹15.34 DP charge per scrip per delivery-sell  →  ₹50,000 position floor
0.8% risk per trade (₹4,000) under a 6.0% heat cap
──────────────────────────────────────────────────────
⇒ SEVEN concurrent positions, not ten
```

The plan's original "~10 positions" satisfies the DP floor but breaches the heat cap at any
realistic stop distance.

**The honest cost, stated so nobody rediscovers it as a surprise:** the ₹50,000 floor plus the
0.8% budget caps the workable stop distance at 12.5%, which structurally excludes high-volatility
names and biases the book toward correlated large caps. The residual-correlation gate (RISK-08)
exists to push back against that bias, which is why it is not optional.

**Three correctness traps worth knowing:**

- Heat uses the **current resting stop**, not the entry stop. A stop trailed above entry has
  locked in a gain and contributes zero heat; using the original keeps the book permanently hot.
- The industry label comes from a **dated** membership snapshot. This is leakage vector #2 from
  `asof.py`'s own docstring applied to risk rather than returns, and it would be invisible.
- Candidates are gated **sequentially against a growing book**. Sizing each against the starting
  portfolio is how a screener emits 25 cards that jointly breach every cap it claims to enforce —
  a bug this system actually had.

---

## 6. The dashboard

FastAPI serves both the JSON API and the built SPA from one process at `127.0.0.1:8000`.

**Every endpoint is one SELECT from a pre-aggregated table.** A panel that needs a per-request
computation over 500 stocks must first add a nightly pre-aggregate; there is no exception path.
The M1 gate is `/api/heatmap` p95 < 30 ms.

**The honesty rules are the product**, not decoration:

- A probability renders as an **interval**, never a bare point; the midpoint is suppressed
  entirely when the 90% interval exceeds 15 percentage points; `UNAVAILABLE` renders an em-dash
  plus the server's reason. A `73%` backed by four observations is not a weak estimate — it is a
  different kind of object, and the two must not share a visual language.
- Calibration below 30 resolved recommendations renders the shortfall, not an empty chart
  implying calibration is known and perfect.
- Rules that are specified but not yet enforceable render `DISPLAY_ONLY`, not a green tick.
- `WATCH` ≠ `HOLD`. `HOLD` means *you own this, keep it* and is only valid for a portfolio
  position. A screened candidate you have never bought is `WATCH`, with `qty: 0` and the
  hypothetical size in `would_qty`. Labelling an unowned candidate `HOLD` reads as advice about
  an existing position and hides that the system is not telling you to do anything.
- Every coded term is explained in plain English from `GET /api/glossary` — served from the
  backend so a tooltip cannot drift from the rule that actually fires.

---

## 7. What the system currently says about itself

**Tonight's run produces 25 cards and zero BUYs.** That is the honest output, not a failure, and
it is the most important thing to understand before overriding it.

The barrier estimate is the arm-invariant BASE rate. The EDGE term that would differentiate a
specific candidate contributes exactly 0.0 until the system has resolved ≥30 of its own trades.
A base rate against a 2.0 ATR target and a 1.5 ATR stop is ≈ zero expectancy before costs and
negative after them. The best candidate scores about **+0.04 ATR against a +0.05 bar**.

A hard reject on that gate would refuse 100% of candidates every night forever — a shutdown with
extra steps, not a risk control. So each card is published with a full plan and labelled
`no demonstrated edge`, with its arithmetic printed.

**The system has not yet shown it can pick stocks, and it says so on every card.** It cannot say
otherwise until the four-arm comparison has resolved trades behind it.

---

## 8. Deliberately not built yet

| Area | Milestone | Consequence today |
| --- | --- | --- |
| Hash-chained ledger, PaperBroker | M2 remainder | Nothing resolves → calibration and the four-arm comparison stay `INSUFFICIENT`. **This is the next thing that unblocks everything.** |
| Backtester sharing `simulate_fill`/`apply_costs` | M3 | No anti-drift invariant yet |
| LLM plumbing, committee, validators | M4–M5 | No narrative layer; the deterministic arm is the whole product |
| News, filings, sentiment | M7 | `nx.*` tables exist and are empty |
| Learning loop, calibration, four arms | M8 | The system cannot yet measure itself |
| Broker adapter, minute bars, intraday | M10–M11 | Paper only, daily only |

Anything built on top of this should assume the ledger is coming and write through it rather than
around it.

---

## 9. Known weaknesses

- **ReplacingMergeTree is used for most tables and dedupes only on an unpredictable background
  merge.** Re-ingesting a session leaves duplicate rows until then, and a duplicated bar shifts
  every trailing window — SMA20 silently becomes a 20-row average of 19 distinct sessions. Reads
  now dedupe defensively in `nightly.load_panel_inputs`, but the pattern is a latent trap
  elsewhere. `ev.recommendations`/`ev.rejections` were deliberately moved to plain MergeTree with
  `run_id` in the sort key after ReplacingMergeTree was found to be destroying run history.
- **The PIT guard is a regex over SQL text, not a parse.** It is a build-time, syntactic check.
  The runtime proof — propagating `traceparent` into the ClickHouse driver so a number can be
  joined to the exact SQL and `knowledge_time` predicate that produced it — is scheduled at M4
  and is the only real evidence of point-in-time correctness.
- **The regime classifier is four thresholds**, not the 3-state HMM the plan specifies. It is
  labelled a heuristic on the dashboard rather than dressed up as a model, and it only ever
  tightens risk limits.
- **`md.benchmark_1d` is empty.** Relative-strength numbers against the NIFTY 50 TRI are not yet
  computed; the TRI must be reconstructed from the published dividend-points series.
- **No authentication.** Binding is `127.0.0.1` only. Do not expose this host.
- **NSE Terms of Use prohibit automated collection and redistribution.** The posture is published
  archive files only, no cookie-warming, no bot-protection defeat. Keep it that way, and read the
  terms yourself before extending the fetch layer.
