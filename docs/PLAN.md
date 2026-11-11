# Trading Agent — local multi-agent NSE/BSE recommendation & paper-trading system

> **Status:** drafting. Environment facts below are verified on this machine. Architecture sections
> are being filled in from two research + fact-check passes:
> - `wf_c2ca01ea-c5e` — market data, news/NLP, local LLM, storage, orchestration, quant engine, dashboard
> - `wf_431cc86b-d0b` — **fundamentals** (statements/XBRL, filings & concalls, ownership/governance/calendar/macro,
>   factor design & point-in-time correctness). Added 2026-10-31 after the user correctly pointed out that the
>   positional/investing style needs financial statements, which the first pass did not cover.

## Context

`/Users/msuhi00/Projects/python` is currently a scratch folder (`test.py`, `load_test_tuning.py`, a
`ca-bundle.pem` and `results.json` from unrelated Albertsons load-testing work). It is **not a git
repo**. The user wants it turned into **Trading Agent**: a fully local, self-improving, multi-agent
system that researches Indian equities and issues reasoned BUY/SELL/HOLD calls.

**The outcome wanted:** a system that behaves like a small fund-management team — a news/catalyst
scout, a technician, a quant screener, a portfolio manager and a post-mortem critic — which every
day produces a ranked set of trades, each with an explicit, machine-readable *reason*, a price
range and a stop-loss; records them in an auditable ledger; paper-trades them; scores the outcome;
and uses that score to get measurably better over time. A modern React dashboard surfaces a daily
industry heat map, per-industry trending stocks ranked by liquidity/volume and other fund-manager
metrics, the ledger, and performance KPIs.

**Hard constraints set by the user**
- Everything runs **locally and free** where possible; paid services must be swappable in by config.
- Agents/models run **one at a time**, started and shut down by an orchestrator, so the machine
  never thrashes. Quantization expected.
- **Paper trades only** at first, stored in the local DB — but a broker adapter must already exist
  so live broker APIs can be plugged in later.
- Single user, personal use. **Do not design for scale.**
- MongoDB for documents, ClickHouse for numeric/analytics, Docker/Redis where they earn their keep.

**Decisions the user made (2026-10-31)**

| Decision | Answer |
| --- | --- |
| Broker account | **Angel One / Dhan** — both offer free API access with historical candles + websocket |
| Paper capital | **₹5,00,000** |
| Universe | *"You decide — not the smallest, we need data too; we'll scale when it performs"* |
| Styles | **All three**: intraday, swing (2–20d), positional/investing (1–12m) |

---

## Verified environment (probed, not assumed)

| Fact | Value |
| --- | --- |
| Machine | Apple **M5 Pro**, 18 cores, **48 GB** unified memory, arm64, macOS 26.5.2 |
| Free disk | 765 GB |
| **RAM already in use** | **30.8 GB** (Outlook 8.2, VS Code 4.8, Chrome 2.4, Podman 0.65, Java 0.8…) |
| **Realistic LLM headroom** | **~15–17 GB** while the user is working; ~35 GB if apps are closed |
| Swap in use | 0 MB (healthy) |
| `iogpu.wired_limit_mb` | 0 (macOS default ≈ 75 % of RAM available to Metal) |
| Docker | CLI 29.6.1 + compose + **colima installed but no VM created**; Podman Desktop app present |
| Node / npm | v22.16.0 / 10.9.2 |
| Python | default `python3` = **3.15.2** (python.org); brew has 3.12/3.13/3.14 available; **no `uv`** |
| Already installed | redis (brew), postgresql@15, openjdk@21, Xcode CLT |
| Not installed | ollama, llama.cpp, MLX, LM Studio, MongoDB, ClickHouse, uv |
| Corporate proxy | **none** — clean egress, npm registry default |

### Network reachability (probed from this machine)

| Endpoint | Result | Consequence for the design |
| --- | --- | --- |
| `nsearchives.nseindia.com/.../EQUITY_L.csv` | **200**, 185 KB | ✅ Official NSE archive files work with a plain request — **this is the primary EOD source** |
| `niftyindices.com/.../ind_nifty100list.csv` | **200**, 6.6 KB | ✅ Index constituents fetchable directly |
| `www.nseindia.com` (web app) | **403** | ❌ The browser-facing site rejects non-browser clients. **Design around it — use archives + broker API, not NSE's internal JSON endpoints.** |
| `api.bseindia.com` (no Referer) | **403** | Same posture: prefer BSE's published archive files |
| `query1.finance.yahoo.com` | **429** | ⚠️ Yahoo is rate-limited/unreliable here — **demote yfinance to a fallback, never the primary** |
| ET Markets RSS / Moneycontrol RSS | **200** | ✅ News ingestion via RSS is viable today |
| huggingface.co, ollama.com | **200** | ✅ Model downloads fine |
| Dhan docs, Angel SmartAPI | **200** | ✅ Broker path is open |

**Design consequences already locked in by these probes:**
1. **Market data comes from official archive files + the user's broker API**, not from scraping
   NSE's logged-in web endpoints. This is more robust *and* ToS-clean.
2. **yfinance is a fallback only.** The primary EOD path is the NSE bhavcopy archive.
3. **Model sizing must assume ~15 GB free, not 48 GB.** Heavy reasoning runs are scheduled for
   post-market, and the orchestrator must check real memory pressure before loading a model.

### Verified: the EOD data source actually works

Downloaded three real trading days of the NSE **UDiFF bhavcopy** with a plain request:

```
https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip
→ HTTP 200, ~203 KB zipped / 636 KB CSV, 3,698 rows (2,712 of them series=EQ)
```

Columns include `TckrSymb, ISIN, SctySrs, OpnPric, HghPric, LwPric, ClsPric, PrvsClsgPric,
TtlTradgVol, TtlTrfVal (turnover), TtlNbOfTxsExctd, OpnIntrst, ChngInOpnIntrst`.
Spot-checked RELIANCE 2026-10-29: O=1159.00 H=1182.10 L=1155.60 C=1167.40, vol 2.09 crore,
turnover ₹2,450 crore, 293,758 trades. **Turnover and trade-count come free in this file — those
are exactly the liquidity metrics the screener needs.** Ten years ≈ **489 MB zipped**: trivial.

*(This file is cash-market only — F&O open interest needs the separate FO bhavcopy.)*

### Verified: index constituents carry the heat-map taxonomy

All of `ind_nifty50list / nifty100 / nifty200 / nifty500 / niftybank / niftyit / niftypharma .csv`
return HTTP 200 from `niftyindices.com/IndexConstituent/`. The schema is
`Company Name, Industry, Symbol, Series, ISIN Code` — **the official `Industry` column is the
heat-map grouping, free and first-party.** Nifty 100 yields 17 industries (Financial Services 23,
Capital Goods 11, Auto 9, Metals & Mining 8, Healthcare 8, FMCG 7, Power 6, Oil & Gas 6, IT 6, …).

### Verified: Python version is forced to 3.13

Queried PyPI for real wheel/metadata support. The binding constraints:

| Package | Latest | Constraint |
| --- | --- | --- |
| **vectorbt** | 1.2.0 | `requires_python <3.15,>=3.11` → **rules out this machine's default 3.15.2** |
| **nautilus_trader** | 1.231.0 | `<3.15,>=3.12`, no cp315 wheel |
| pandas-ta | 0.4.71b0 | `>=3.12`, still maintained |
| ta-lib | 0.8.3 | **ships prebuilt macosx-arm64 wheels for cp312–cp315** — no source build needed any more |
| numba / llvmlite | 0.68.1 / 0.50.0 | arm64 wheels through cp315 (not the blocker) |
| numpy 2.5.4, pandas 3.1.0, scipy, scikit-learn, pyarrow 26, torch 2.15, mlx 0.32.4, clickhouse-connect 1.11, pymongo 4.19 | | all have cp313 arm64 wheels |
| **dhanhq** 2.3.0, **smartapi-python** 1.5.5 | | both on PyPI → the user's broker is directly supported |

> Also: the machine's default `python3` (3.15.2, python.org build) **has no CA bundle** — every
> HTTPS call fails with `CERTIFICATE_VERIFY_FAILED`. A second, independent reason not to use it.
> **Pin Python 3.13 via `uv`**, which installs its own managed interpreter and sidesteps both issues.

### Verified: real model weight sizes (from the Ollama registry) vs. real headroom

Queried `registry.ollama.ai` manifests for exact weight-layer bytes. Measured against the
**~15–17 GB actually free** (not the nominal 48 GB):

| Model | Weights | Verdict against ~15 GB free |
| --- | --- | --- |
| `gpt-oss:120b` | **60.88 GB** | ❌ Impossible on this machine at any time |
| `qwen3:32b` | 18.81 GB | ⚠️ Only with apps closed |
| `deepseek-r1:32b` | 18.49 GB | ⚠️ Only with apps closed |
| `qwen3:30b-a3b` (MoE) | 17.28 GB | ⚠️ Tight, but only ~3B active params → fast |
| `gemma3:27b` | 15.42 GB | ⚠️ Borderline |
| **`gpt-oss:20b`** | **12.85 GB** | ✅ **Fits today, with room for KV cache** |
| `magistral:24b` | 13.35 GB | ✅ Fits |
| `qwen3:14b` / `deepseek-r1:14b` | 8.64 / 8.37 GB | ✅ Comfortable |
| `qwen3:8b` / `qwen3:4b` | 4.87 / 2.33 GB | ✅ Cheap swappable workers |
| `bge-m3` (embed) / `qwen2.5vl:7b` (vision) | 1.08 / 5.56 GB | ✅ |

**This forces an adaptive model ladder** rather than one fixed model: the orchestrator measures
free RAM before each stage and loads the heaviest tier that fits, degrading gracefully instead of
swapping the machine to a halt. Written up under *Architecture* below.

---

## Three corrections to the inputs you gave me

Research contradicted three of our working assumptions. Flagging before the design, because each changes something.

**1. Dhan is not the free option.** Dhan's `/v2/charts/*` historical endpoints are gated behind a ₹499+GST/month Data API (it returns error code `806` without it). Angel One SmartAPI *is* free. **Upstox** is also free (Analytics Token, 1-year validity, read-only, no static-IP whitelist) and is the cleanest of the three — its only limit is that minute data starts Jan-2022. *Recommendation: use Angel One (you have it) or open Upstox; both are a one-line config swap behind the same adapter. Milestones 1–3 need no broker account at all, so this can wait.*

**2. ₹5L capital sets a hard floor on position count.** The DP charge is a **fixed ₹15.34 per scrip per delivery-sell day** — it does not scale with size. At ₹50,000/position that is 3.1 bps; at ₹25,000 it is 6.1 bps, on top of ~23.8 bps round-trip delivery cost. So **₹5L realistically means a maximum of ~10 concurrent positions**, not 20. That is workable, and the design assumes it. If you would actually deploy more, say so — it changes sizing, the concentration cap, and every cost conclusion the learning loop draws.

**3. NSE's Terms of Use are stricter than expected.** They explicitly prohibit *"systematic or automated data collection (including scraping, data mining, data extraction and data harvesting)"*, prohibit redistribution, and ban use for *"gaming, virtual trading or simulation"* — which arguably touches a paper-trading ledger. The design's posture is **downloading published archive files only** (the `nsearchives` bhavcopy and `niftyindices` CSVs I verified), with **no cookie-warming and no bot-protection defeat** — warming a session to reach `www.nseindia.com`'s JSON API is a materially different act from fetching a published CSV. Everything cookie-gated is optional and degrades to `news_coverage='PARTIAL'` rather than being load-bearing. I'm not a lawyer and found no Indian case law; exposure is contractual, not criminal, and a private single-user non-redistributing system is the lowest-risk posture. **Read [NSE's terms](https://www.nseindia.com/static/nse-terms-of-use) yourself before building on this.**

---

## Governing principles

These resolve every ambiguous case downstream:

1. **The LLM never computes a number.** Every price, indicator, stop, target, size, cost and probability comes from Python/ClickHouse and is pre-printed into a fact sheet. The model selects among pre-validated candidates, explains, assigns a closed-enum reason code, states confidence, and may refuse.
2. **`knowledge_time` ≠ event date ≠ crawl time.** Every point-in-time table carries `knowledge_time`; all reads go through one gated accessor; a CI test fails the build on any unbounded read.
3. **Raw bytes are immutable.** `data/raw/YYYY/MM/DD/` is written *before* anything parses it. Everything else is reconstructible.
4. **One model resident at a time**, enforced at three independent levels that fail differently.
5. **The evaluation harness exists before the strategy; the strategy exists before the model.** Milestones 1–3 contain zero LLM calls.

> Principle 5 is the one that serves your core objective. "It learns and grows with each recommendation" is an *unfalsifiable* claim unless there is a frozen baseline to beat and every decision is reconstructible. Retrofitting an audit trail is impossible — the data you needed was never stored.

---

## The stack

| Layer | Choice | Why |
| --- | --- | --- |
| Interpreter | **CPython 3.13 via `uv`** | `vectorbt<3.15`, `nautilus_trader<3.15`, `readability-lxml<3.15`, `faust-cchardet` has no cp315 wheel. 3.13 clears every constraint *and* keeps `outlines` + `clickhouse-driver` available (3.14 drops `outlines`). Your default 3.15.2 also has a broken CA store. |
| Packaging | **uv** (`brew install uv`) | Manages interpreter + deps + lockfile. `uv run --frozen` is reproducible under launchd, which execs without a shell. |
| Orchestrator | **Prefect 3.9** on SQLite | Wall-clock scheduling, retries and a DLQ in one process, not Airflow's five. ⚠️ *Re-justified 2026-10-31.* The original reason — "the only orchestrator with cross-subprocess global concurrency limits without a broker" — **is no longer true**: DBOS's `Queue(global_concurrency=N)` is database-backed and broker-free in 12 packages. Prefect stays for scheduling maturity and DLQ; it is **not** load-bearing for the committee's internal control flow. |
| Numeric store | **ClickHouse OSS**, native arm64 | Columnar scans; `ASOF JOIN` kills a class of look-ahead bugs; HNSW vector index means no 4th datastore. |
| Document store | **MongoDB 9 Community**, single-node replica set `rs0` | Replica set is required for change streams and `mongodump --oplog`. |
| Cache/bus | **Redis** (already installed) | Three jobs only: GPU-mutex backstop, token buckets, pub/sub → SSE. |
| LLM runtime | **llama.cpp `llama-server`, router mode** | GBNF gives token-level schema guarantees. SIGTERM = guaranteed unload. ⚠️ *Corrected 2026-10-31:* `--models-max 1` is **a policy you must verify, not a structural invariant**. Router mode is self-labelled experimental (#18035); a TOCTOU race in `unload_lru()` was reported at exactly `--models-max 1` (#20137, closed "not planned" by a stale-bot with only an unlinked partial fix); and `-cram` is applied per-child, so the memory budget is per-model, not global. Assert `GET /models` returns **≤1 before every load**, not merely empty after the stage. Calling something structural that isn't means an OOM has a cause you have architecturally excluded from consideration. |
| Reasoner | **Qwen3.6-35B-A3B**, 4-bit GGUF | 36B total / **3B active**. M5 Pro is bandwidth-bound: a dense 27B reads ~15 GB per token-step; a 3B-active MoE reads ~1.9 GB. Verified on HF (4.7M downloads). |
| Red team | **gpt-oss:20b** (14 GB) | *Different model family* — a critique from the same weights that wrote the thesis is theatre. |
| Tabular ML / Meta-Labeler | **LightGBM (CPU, purged walk-forward)** | Captures non-linear regime × sector × factor × event interactions over 1.4M+ bar rows in <2s on CPU with zero GPU/VRAM footprint. Sits between the linear factor baseline and the LLM Committee. |
| Frontend | **Vite + React + Tailwind + shadcn**, lightweight-charts + d3 treemap | SSR is pointless for localhost. |

**No Docker, no colima.** Apple's `vz` hypervisor does not return freed guest memory to macOS ([lima#2789](https://github.com/lima-vm/lima/issues/2789), open), so `--memory 10` is a one-way high-water mark. All databases run natively.

### ⚠️ The memory defaults that would kill this on day one

| Component | Default on this box | Must be set to |
| --- | --- | --- |
| MongoDB WiredTiger cache | `max(0.5×(RAM−1GB), 256MB)` = **23.5 GB** | `cacheSizeGB: 2` |
| ClickHouse `max_server_memory_usage` | `0` = all available ≈ **43 GB** | `5368709120` **absolute** (the ratio-based worker does nothing on macOS) |
| `llama-server` | `--models-max 4`, margin `0` (accounting **off**) | `--models-max 1 --models-memory-margin 8192` |

This is precisely the "machine gets overwhelmed" failure you asked me to prevent, and all three are silent.

### Memory arithmetic — and the one thing you must do

```
your apps today (Outlook 8.2 + VS Code 4.8 + Chrome 2.4 + …)   30.8 GB   ← measured
+ Qwen3.6-35B-A3B @ 32k ctx                                    22.4 GB
  ------------------------------------------------------------------
                                                               53.2 GB  of 48 → SWAP DEATH
```

```
apps closed (macOS + services only)                            15.3 GB
+ Qwen3.6-35B-A3B @ 32k ctx                                    22.4 GB
  ------------------------------------------------------------------
                                                               37.7 GB  of 48 → 10.3 GB spare ✅
```

The heavy reasoning stage runs **20:45–22:05 IST**, when you are unlikely to be working. But this must be explicit, so the orchestrator uses an **adaptive model ladder**: it reads free memory immediately before loading and picks the heaviest tier that fits — Qwen3.6-35B-A3B (22.4 GB) → gpt-oss:20b (12.9 GB) → qwen3:14b (8.6 GB). If it has to step down it stamps `degraded_mode=true` on every recommendation that night and badges it on the dashboard, **because a Q3-reasoned trade must be distinguishable from a Q4-reasoned one in the ledger or the learning loop fits noise.**

**A badge is not a detector.** Stamping the flag and painting it on the dashboard does not catch
the expensive case: a *week* of quietly degraded recommendations poisoning the learning loop
while nobody reads the badge. The 22:35 `maintain` stage therefore runs a **streak detector** —
N consecutive degraded nights, or degraded share above a threshold over a rolling window, raises
an alert and marks the affected window as suspect in the four-arm comparison. Roughly ten lines;
without them the flag is decoration.

Also: context is capped at **32k**. The same model measures 19.3 GB @1k, 22.4 GB @32k, 25.0 GB @64k — 64k costs 2.6 GB and buys nothing a 1,500-token dossier needs.

---

## Architecture

### Services (launchd-supervised, long-lived)
`clickhouse` · `mongod` · `redis` · `llama-server` · `prefect server` + 1 worker · `ta-api` (FastAPI, serves the built SPA as StaticFiles)

### Deterministic agents — run while the GPU is empty

| | Agent | Job |
| --- | --- | --- |
| D1 | **Archivist** | 10 NSE archive files + BSE UDiFF → immutable Parquet → ClickHouse. Zero auth. *The critical path.* |
| D2 | **Broker Leg** | Upstox/Angel minute candles for the watchlist |
| D3 | **NSE-API Sidecar** | Cookie-gated JSON (announcements, corp actions, FII/DII). **Separate venv, subprocess only — the `nse` package is GPLv3.** Every call optional. |
| D4 | **Feed Poller** | 20 feeds, conditional GET, per-host token bucket, **liveness assertion on every poll** |
| D5 | **Extractor** | HTML → markdown + tables + images (trafilatura; Chromium batch only on failure) |
| D6 | **Doc Parser** | Filing PDFs/XBRL → markdown tables (docling), gated to high-value filings × watchlist |
| D7 | **Entity Linker** | Text → symbol + ISIN. Deterministic exact-join first (54% hit rate, free), NuNER on the residual |
| D8 | **Quant Engine** | ~70 factors, hard gates, composite, PM Conviction Score (`_pm_conviction`: composite + conservative lower-bound expectancy at `ci_lo` + multi-horizon stock/industry trend alignment - marginal pair correlation), Paced Top-N Auto-Buy (`regime_caps["new"]`: 3/day `TREND_UP`, 2/day `CHOP`, 1/day `TREND_DOWN`/`HIGH_VOL`), Slot & Industry Competition (`ROTATE_WEAKEST`), 5-Trigger Daily Exit Engine (`STOP_DISCIPLINE`, `TARGET_REACHED`, `TIME_STOP`, `TREND_REVERSAL`, `ROTATE_WEAKEST` + `STOP_TRAILED`), costs, sizing, triple-barrier labels |
| D8b | **GBDT Meta-Labeler** | LightGBM CPU purged walk-forward classifier on resolved triple-barrier paths (`exit_date < decision_d`, 10-session embargo, `uniqueness_w` sample weights, shallow `max_depth=4, num_leaves=15, min_child_samples=200`), calibrated via PIT isotonic regression, gated by out-of-sample Brier Skill Score (`BSS > 0`), and outputting per-candidate SHAP feature attributions into the `D10` Dossier |
| D9 | **Regime** | 3-state Gaussian HMM, hand-rolled (`hmmlearn` is abandoned — last commit 2024-10-31). **Filtered, never smoothed** — smoothed leaks the future. Risk gate only, never a signal flipper. *Note 2026-11-11 ([ADR-0004](ADR/0004-index-regime-filter-rejected.md)):* a rule-based index regime (SMA50/200 death cross + vol) was tested OOS as an entry block and **lost to the control** — it fires after the fall. Any HMM must beat the control OOS before it gates a trade. |
| D10 | **Dossier Builder** | The ~1,500-token fact sheet, with mandatory **CONTRADICTIONS** and **KNOWN GAPS** sections |
| D11 | **Paper Broker** | Fill simulation + full Indian cost model + ledger events |
| D12 | **Evaluator** | Triple-barrier resolution, `resolve_past_recommendations` (`SHADOW_BUY` / `SHADOW_WATCH` evaluating every recommended card against subsequent daily bars so calibration at `n_eff >= 30` and selection-regret tracking converge ~10× faster), attribution, posteriors, calibration |

### The committee — your "team of fund managers"

This is how the multi-agent structure you asked for is delivered **without ever having two models in memory**: multiple structured passes over the *same resident weights*, each with a different system prompt, a different Pydantic schema, and an explicit debate protocol.

```
Bull → Bear → one Rebuttal → deterministic Risk gates → Risk narrative
     → Portfolio Manager (two-stage) → V1–V9 validators
     → Position Reviewer over every open position → PM desk note
```

**Nine validators** run after generation. A GBNF grammar guarantees *syntax*, never *semantics* — a stop-loss above the entry on a long is perfectly valid JSON. V2 rejects any numeric literal not present in the dossier within 0.5%.

### Why orchestration is plain Python — the argued version

Orchestration between personas is **plain Python, no agent framework.** This was re-decided from
scratch on 2026-10-31 after the question *"isn't a framework easier to fix and audit?"* — which is
a good question that the first draft answered badly. Four options were steelmanned, fact-checked
and scored by three independent judges; the conclusion held, the original reasoning did not.

**What the first draft got wrong, and should not be repeated.** It claimed "every agent
framework's core value is letting an LLM choose control flow." That is false. LangGraph's
`add_edge` is static, and this committee's exact Bull→Bear→Rebuttal→Risk→PM shape compiles and
runs under it with zero routing logic; Apache Burr and Atomic Agents target fixed, auditable
pipelines explicitly. It also claimed a framework would "remove determinism" — backwards.
Frameworks do not remove determinism, they fail to add it, and plain Python has none to start
with. Attacking the strawman left the real argument — **durable execution** — unanswered.

**The argument that survives.** No framework supplies byte-identical replay (see *Replay means
replay-from-record* below), so the generation record is mandatory whichever option you pick. Once
it exists it already **is** a step journal — `if (run_id, symbol, step) in ledger: return
ledger[...]`, roughly 60–150 lines. A framework therefore adds a *second* durable store of the
same facts, without a hash chain, with its own schema, retention, migration story and CVE feed,
reconciled against the first by a test that would not otherwise need to exist.

**And resume actively damages the thing it promises.** Measured on this machine, 2026-10-31: fix
the persona that emitted a bad number, then resume. Under LangGraph the fix never runs — the
upstream node already committed `1187.40` to checkpoint state, the repaired validator stops
raising, and the Portfolio Manager receives the exact literal V2 exists to reject, **in a green
run with no warning.** Under DBOS the resume replays the recorded *error*: the fix is invisible
and you spend the evening debugging something that was never broken. Plain Python re-runs current
code every iteration and gets it right. Having no resume is a safety property, not a gap — and
the failures above are the same shape as all four entries in HANDOFF.md: *looked fine, started
cleanly, and was wrong.* Note also that resume buys little here: the expensive resource is the
22.4 GB resident model, not the node, so any llama-server-shaped crash pays the reload anyway.

**Costs, stated honestly.** Plain Python does not give determinism for free: `PYTHONHASHSEED`
randomises set iteration, which reorders candidates, which changes prompt bytes; a `datetime.now()`
anywhere inside the committee breaks replay. That is ~200 lines of discipline you must write and
test. It also gives you no trace viewer — see **Observability** below, scheduled at M4.

**Two things to steal from the frameworks**, both obtainable without the dependency:

1. **Machine-check "control flow must never vary."** Declare `COMMITTEE_STEPS` as a module-level
   tuple of `(name, callable)`, drive it with a `for` loop containing no `if`, and add a CI test
   asserting the stage-name sequence equals a golden constant. Hash that tuple into the
   `RunManifest`. This is LangGraph's `assert len(branches) == 0` with zero packages, and it is
   strictly better because the hash lands in the tamper-evident ledger. Render the committee
   diagram *from the tuple*, never from a comment, so shape-vs-doc drift is impossible.
2. **The hard rule on replay.** Any resume or replay path must re-execute everything downstream
   of an edit, **or refuse loudly.** Stamp code identity (git sha + persona prompt sha + GBNF sha
   + model sha) on every recorded step and fail when the stamps differ from current. LangGraph
   served a stale *value* silently; DBOS served a stale *error* silently. Both got this wrong, in
   opposite directions. Your own ~60 lines can get it right precisely because you control the
   comparison.

Dependency surface is a weak argument in both directions and is recorded only so nobody
re-litigates it. Measured on this machine against Python 3.13: current lockfile **45** third-party packages (52 `uv.lock` entries);
OTel api+sdk **4**; Temporal **5**; DBOS **12**; LangGraph + SQLite checkpointer **41**; Prefect
**103**; CrewAI **137**. The old parenthetical that CrewAI "caps at `<3.14`" was true and
irrelevant — we pin 3.13, which is *inside* its range. The real disqualifiers are
telemetry-on-by-default and 137 packages.

### Replay means replay-from-record — and the ledger spec that follows

**An unflagged contradiction in this document, found 2026-10-31.** The daily schedule below makes
prefix-cache reuse the load-bearing optimisation of the committee stage — prefill the shared
market-context block once at 2.7 s instead of 25 times at 67 s. *Verification* requires
`ta daily --date 2026-10-15` to reproduce that day **byte-identically**. Those cannot both be true
by re-execution, and the document never said so.

llama.cpp is not bit-reproducible: it guarantees seed-reproducibility only for an identical decode
schedule, and continuous batching varies that schedule. The batch-invariant PR (#16016) has been
an open draft since 2025-09-15, is **CUDA-only** — so irrelevant to this Metal machine at any
future date — and the maintainer is on record: *"I don't want to maintain guarantees for
bit-for-bit identical results as the batch size is varied."* His workaround is a single server
slot with **prompt caching disabled**: surrendering exactly the optimisation the schedule depends
on.

**Resolution — "byte-identical" means replay-from-record, never regenerate.** `ta daily --date X
--replay` re-runs every *deterministic* stage (dossier render, risk gates, sizing, cost model,
V1–V9) against the **recorded** LLM outputs and asserts byte-equality, raising on divergence. It
must **re-derive, never recall**: a replay that simply returns the stored answer passes the gate
while proving nothing, and a regression in the cost model or a validator would sail straight
through. This belongs alongside `tests/pit/`, `test_ledger_rebuild.py` and
`test_backtest_matches_paper.py` as the fourth member of that family.

**Consequence: the ledger spec under *Repo layout* is wrong by omission.** It describes business
events. For byte-identity and for V2 it must also record, per generation:

| Field | Why it is load-bearing |
| --- | --- |
| prompt bytes + sha256 | the input V2 checks numeric literals against |
| GBNF grammar sha256 | hashing the committee source does **not** catch prompt or grammar drift |
| sampling params + seed | insufficient alone, but without it nothing is reproducible |
| sampled token stream | the only artifact that makes replay possible at all |
| GGUF sha256 + llama.cpp build tag | **no lockfile covers the model binary**, and reproducibility holds only within one build tag |
| `uv.lock` sha256 + git sha + stage-tuple hash | library skew moves float arithmetic under V2's 0.5% tolerance; the stage hash proves the pipeline shape |
| `model_tier`, `degraded_mode` | **recorded inputs, never re-measured.** The adaptive ladder probes free RAM at load time; a replay in 2027 probes a different machine, picks a different tier and diverges. Same rule for `datetime.now()`, `PYTHONHASHSEED`-dependent set iteration, and the `random` arm's draw. |

Write these as the **`RunManifest`, the first ledger event of every run.** Canonical JSON, never
pickle — rename a Pydantic model and last year's record stops loading.

### Observability — the part the first draft left silent

Rejecting a framework does not hand you a trace viewer; it means you own that surface. The first
draft said nothing about observability, so the silence read as *"you get nothing"* — which is what
provoked the framework question in the first place. Scheduled at **M4/M5**, deliberately not
earlier: `packages/llm` is empty, and span attributes cannot be designed before the personas exist.

- **OpenTelemetry spans** (api + sdk — 4 packages, pure Python, vendor-neutral) around every stage
  and persona. Use `SimpleSpanProcessor`, **not** `BatchSpanProcessor`: the latter's 5000 ms
  schedule delay drops exactly the spans a `kill -9` was supposed to leave behind, and at well
  under 1,000 spans a night batching buys nothing. Keep a JSONL fallback under `data/raw/` —
  ClickHouse holds port 9000 after SIGTERM (see HANDOFF), so assume it is unreachable at least once.
- **Propagate `traceparent` into `clickhouse-driver`** via the `opentelemetry_traceparent` session
  setting. ClickHouse accepts W3C traceparent and `system.opentelemetry_span_log` is enabled by
  default. Join on `trace_id` and *"why was this number in this recommendation?"* collapses to one
  query returning the exact SQL, its `knowledge_time` predicate and its row count, under the span
  of the persona that cited it. **Nothing in any framework offers this, and it is the only
  _runtime_ evidence of point-in-time correctness available** — `ta_core.asof` is a build-time
  syntactic guard over query text, not proof that the bound matched the run's `as_of`.
- **Viewer:** `uvx arize-phoenix serve` from day one — no Docker (which this stack bans outright),
  no account, SQLite backend. Set `PHOENIX_ALLOW_EXTERNAL_RESOURCES=false` and
  `PHOENIX_TELEMETRY_ENABLED=false`: it is Elastic-2.0 (source-available, *not* OSI open source)
  and has enabled web analytics by default since Dec 2025. The instrumentation is the durable
  asset; the backend is a one-line exporter swap.

### ⚠️ Telemetry — the no-egress constraint is already violated today

"Everything local and free, no vendor telemetry, no data egress" is threatened by the *incumbent*
stack first, not by the frameworks this document rejects. Fix before M1:

```bash
export PREFECT_SERVER_ANALYTICS_ENABLED=false   # else egresses to sens-o-matic.prefect.io
export DO_NOT_TRACK=1                           # and to api2.amplitude.com
```

Prefect additionally hard-depends on `docker` — a package banned at the stack level above — and on
`amplitude-analytics`. And **Prefect 3 auto-caches tasks called twice with the same inputs**, while
the committee invokes the same task shape per persona, per candidate, per validator: set
`cache_policy=NO_CACHE` explicitly on every committee-adjacent task. The hazard is latent only
because caching requires result persistence, which is off by default — it arms itself the moment
you enable persistence for the durability reasons discussed above.

---

## Capital protection — the rules that decide the size

**The arithmetic first, because it corrects this document.** ₹5,00,000 equity; the flat ₹15.34
DP charge sets a ₹50,000 position floor; 0.8% risk per trade (₹4,000) against a 6.0% portfolio
heat cap. Those constraints are simultaneously satisfiable at **seven concurrent positions, not
ten.** The "~10 positions" stated earlier in this document satisfies the DP floor but breaches
the heat cap at any realistic stop distance. Seven is the book; nine is an absolute backstop.

**The honest cost, stated once so nobody rediscovers it as a surprise:** the ₹50,000 floor plus
the 0.8% risk budget caps the workable stop distance at **12.5%**. That structurally excludes
high-ATR names and biases the book toward correlated large caps. The diversification rules below
push back against that bias — which is why the residual-correlation gate is not optional.

Three enforcement points, deliberately distinct: a **GATE** runs before sizing and a failure
means the candidate never reaches the dossier; **SIZING** clamps change the number but keep the
trade; a **BREAKER** is post-hoc and can halt new entries. Implemented in
`packages/ledger/risk.py`; every rule has a test that constructs a breaching portfolio and
asserts the rule fires (`tests/test_risk_gates.py`). *A risk rule without a failing-case test is
a comment* — a predicate that silently evaluates to NULL never rejects and renders green forever.

| ID | Rule | Where | On breach |
| --- | --- | --- | --- |
| RISK-01 | ₹50,000 ≤ notional ≤ ₹1,00,000 | SIZING | resize down; **reject** below the floor — rounding up breaches RISK-02 |
| RISK-02 | risk/trade 0.8% (₹4,000), hard ceiling 1.0% | SIZING | resize to ceiling |
| RISK-03 | stop distance ≤ 12.5% | GATE | reject (never resize — see the arithmetic above) |
| RISK-04 | portfolio heat ≤ 6.0%, from the **current resting stop** | GATE + BREAKER | reject new entry; heat is released only by stops trailing up or positions closing |
| RISK-05 | ≤ 7 positions (9 backstop), cash ≥ 8%, gross ≤ 92% | GATE | reject |
| RISK-06 | ≤ 25% and ≤ 2 names per Industry; Financial Services ≤ 30% / 3 names | GATE | resize to headroom, reject below the floor |
| RISK-07 | distinct industries ≥ ⌈positions/2⌉ once ≥ 5 open | assertion | implied by RISK-06; a failure means the industry join is broken |
| RISK-08 | pairwise residual correlation < 0.65 (release < 0.55) | GATE | reject, or resize under a 15% combined cap |
| RISK-11 | ADV ≥ ₹5 cr, size ≤ 0.5% ADV, ≥ 3,000 trades/day, price ≥ ₹30, ≥ 150 sessions listed | GATE | reject — **never resize into illiquidity** |
| RISK-12 | no ASM/GSM/trade-to-trade/non-EQ series | GATE | reject; an open position newly flagged tightens its time stop, it is not force-sold |
| RISK-13 | long only, no leverage, gross ≡ net | GATE + broker | reject and raise — this is a programming error, not a risk event |
| RISK-14 | daily realised loss ≤ 2.0% | BREAKER | halt new entries **and skip tonight's publish** |
| RISK-16/17 | rolling 5-session ≤ 4.0%, 21-session ≤ 7.0% | BREAKER | halt 3 sessions / until recovery; heat cap halved after |
| RISK-18 | drawdown ladder 8 / 12 / 15 / 18%, halt at 25%, watermark never reset | BREAKER | size ×0.5, then halt; re-arm requires an ADR |
| RISK-20 | consecutive losses 6 warn / 8 half / 10 halt | BREAKER | halt until the weekly critic has labelled the streak |
| RISK-25 | regime gates exactly five quantities: gross, cash, heat, single-name, new entries/night | SIZING | caps tighten on the first session but widen only after three — **asymmetric on purpose** |
| RISK-10 / RISK-22 | factor exposure; expected portfolio loss | **DISPLAY-ONLY** | specified now, enforced once the statistics exist. Rendering these as OK would imply an assurance nobody has earned |

**Correctness notes that are easy to get wrong.** Heat uses the *current* stop, not the entry
stop: a stop trailed above entry has locked in a gain and contributes zero heat, and using the
original would keep the book permanently hot. The industry label comes from `md.index_members`
as a **dated snapshot**, never today's membership — that is leakage vector #2 from
`ta_core.asof`'s own docstring applied to risk rather than returns, and it would be invisible.
And candidates are gated **sequentially against a growing book**: sizing each against the
starting portfolio is how a screener emits 25 cards that jointly breach every cap it claims.

---

## Barrier probabilities — P(target), P(stop), P(time-stop)

The most dangerous numbers in the product, because a percentage looks authoritative whether or
not it is earned. `packages/quant/barriers.py` is built to **refuse more readily than it
answers**.

- **Empirical, not analytic.** A Brownian kernel understates P(stop) on Indian equities: it has
  no gaps, no circuit limits and no fat tails, and all three hit the stop rather than the target.
  Real forward paths are resampled, ATR-standardised at t0.
- **One Dirichlet, three outcomes.** Competing risks over the horizon, so the triad sums to 1 by
  construction. Three independently-fitted Betas would not, and the error would be invisible.
- **Pessimistic same-bar resolution.** When one bar spans both barriers, daily data cannot say
  which came first. Record DOWN.
- **`n_eff`, never a row count.** Overlapping forward windows are not independent; Kish's
  effective sample size on uniqueness weights gates display. Below `MIN_N_EFF` the estimate is
  `UNAVAILABLE` and the UI renders an em-dash plus the reason. A `73%` with four observations
  behind it is a different kind of object and must not share a visual language with a real one.
- **The barrier geometry must equal the live geometry.** An early version measured P(touch −1.0
  ATR) while placing the stop at −1.5 ATR, which overstated P(stop) and made every candidate look
  negative-expectancy. This is the same class of error the plan already warns about for the time
  stop, applied to the price barriers.

**What the system currently says about itself, and it is worth reading twice.** With the EDGE
term at zero — it contributes nothing until `n_eff ≥ 30` of the system's *own* resolved trades —
the barrier estimate is the arm-invariant BASE rate. A base rate against symmetric-ish barriers
is ≈ zero expectancy before costs and necessarily negative after them. So a hard
"reject below +0.05 ATR" gate on BASE alone would reject 100% of candidates every night forever:
that is not a risk control, it is a shutdown with extra steps. The gate is therefore a
**downgrade**: the card is published with a full trade plan and labelled
`no demonstrated edge`, with its arithmetic printed. An honest card is informative; an empty
screen that silently swallowed 367 candidates is not.

---

## Dashboard surface

Served from FastAPI at `127.0.0.1:8000`, Vite dev at `:5173`. **Every endpoint is one SELECT
from a pre-aggregated table**; a panel needing a per-request computation over 500 stocks must
first add a nightly pre-aggregate. There is no exception path. The API computes nothing — every
number was decided and recorded by the nightly run.

| View | Shows | Source |
| --- | --- | --- |
| **Today** | market context (regime chip, breadth, benchmark), ranked trade-plan cards, rejection drawer | `ev.recommendations`, `ev.rejections`, `md.breadth_daily` |
| **Heat map** | d3 treemap by Industry, sized by turnover, coloured by return | `md.sector_daily` |
| **Screener** | ranked candidates with trend state and reason chips. **No sort-by-probability control exists** | `ev.recommendations` ⋈ `md.trend_1d` |
| **Portfolio** | positions, equity/cash/heat tiles, drawdown vs watermark | `ev.positions` |
| **Risk** | every rule with state chip and headroom bar; industry diversification | `ta_ledger.risk.rule_states` |
| **Analysis** | per-symbol candles, three-timeframe trend states, RS | `md.bars_1d`, `md.trend_1d` |
| **Calibration & Simulation** | the refusal panels | `ev.outcomes`, `ev.equity_daily` |

**Honesty rules the UI enforces, which are the point of the product.** A barrier block renders
its *interval* ("41–57%"), never a bare point, and suppresses the point entirely when the 90%
interval exceeds 15 pp. `UNAVAILABLE` renders an em-dash and the reason. Calibration with fewer
than 30 resolved recommendations renders the shortfall, not an empty chart implying calibration
is known. The forward Monte Carlo refuses below 750 engine-days and 200 closed trades — a
distribution fitted to less is a picture of noise. Money is formatted in lakh grouping.

**Trend** is a closed 8-member `TrendState` enum at three lookbacks (20/63/252), from a
**Newey-West-corrected** drift t-stat, the Kaufman efficiency ratio and ADX's 10-session delta.
The HAC correction is load-bearing: the obvious alternative — OLS of log(close) on t — has
strongly autocorrelated residuals, far too small a standard error, and scores almost every random
walk "significant". `*_DECAYING` blocks new trend-following entries; it never forces an exit and
never flips a side. `UNDEFINED` is not `RANGE` — too little history means the trend was never
measured, and recording that as range-bound feeds the learning loop an observation that never
happened. A name with ≥ 6 state flips in 63 sessions is excluded from trend-following sleeves.

**Sentiment** is deferred to M7 and ships demoted. Its promotion bar is explicit: incremental
out-of-sample rank-IC ≥ 0.020 residualised against existing price factors, or it stays
`DISPLAY_ONLY` — rendered achromatically, never appearing in any response that also carries a
size, stop, target or rank, and never the sole reason code on a recommendation.

---

## Daily schedule (IST)

| Time | Stage | Model resident | Total RAM |
| --- | --- | --- | --- |
| 15:45 | `eod_mark` — mark positions, **gap rule** (gap through stop ⇒ fill at open) | none | 15.6 |
| **18:45** | `ingest_archive` ⭐ — raw bytes to disk **before** parsing | none | 15.8 |
| 19:00 | `validate_eod` ⭐ — freshness gate. **Fail ⇒ NO recommendations. Never degraded ones.** | none | 15.3 |
| 19:25 | `build_features` — ~70 factors → winsorize → rank-to-normal → sector-neutralise | none | 17.8 |
| **19:40** | `evaluate_and_learn` ⭐ — resolve outcomes, update posteriors **before** tonight's picks | none | 15.8 |
| 19:55 | `nlp_enrich` — **one load for the whole day's backlog**, then unload | encoders | 17.3 |
| 20:30 | `screen_and_dossier` — hard gates → composite → top 20–25 → render dossiers | none | 16.3 |
| **20:45** | **`committee`** ⭐⭐ — ONE load, all personas, all candidates. Unload; assert `GET /models` empty. | **reasoner** | **37.7** |
| 22:05 | `red_team` — cross-family attack on the top 8 | gpt-oss:20b | 29.3 |
| 22:25 | `publish` — validators, ledger events, staged orders for tomorrow's open | none | 15.3 |
| 22:35 | `maintain` — incremental backup, `rebuild_from_ledger()` equality test, **`degraded_mode` streak detector** | none | 16.3 |

**Model loads per night: 3.** A naive per-stock loop over 25 candidates would cost ~21 minutes of pure loading and grow linearly — hence the pipeline is **phased by model, not by symbol**. Inside the committee load, the shared market-context block goes at the *start* of every prompt so the prefix cache prefills once (2.7 s) instead of 25 times (67 s).

**Morning T+1 is a veto gate, never a generator:** 06:45 preflight → 07:00–08:40 feed polls → 08:50 *conditional* re-reason (only if a candidate is flagged; on a quiet night zero models load) → 09:00 pre-open gate → 09:15 fill at the **actual open** → 09:20–15:30 **deterministic intraday management only**.

**Health gates:** abort on >512 MB swap *growth* during a phase; abort if decode drops below ~10 tok/s (llama.cpp page-faults from SSD rather than failing cleanly — only a floor alarm catches it); on memory failure **never retry and never escalate** — step down the ladder and stamp `degraded_mode`.

---

## The learning loop

### Five arms, every night, scored identically

| Arm | What it is | What it proves |
| --- | --- | --- |
| `random` | 2% of risk to random candidates passing hard gates | the true zero line, *including real costs* (must lose after costs) |
| `frozen_control` | the day-1 rule screener, **`weights.v1.json` frozen forever** + ATR-bucket `p_base` | **If the "learned" system can't beat this in 6 months, the learning is cosmetic.** The single most valuable number in the project. |
| `deterministic` | current screener, linear composite + PM Conviction Score + Thompson-bandit sleeve weights | baseline quantitative execution |
| `gbdt_meta` | Purged walk-forward LightGBM + isotonic calibration over price + trend + regime + news/event features | non-linear tabular ML meta-labeler predicting barrier win probability |
| `committee` | the full persona pipeline debating over `D10` dossiers containing `GBDT_META` probabilities & SHAP drivers | the LLM reasoning product |

Cost: ~50 extra ledger rows/day. **Demotion and promotion rules are pre-committed on day one, not retrofitted when you're emotionally invested:** `committee` (LLM) must beat `gbdt_meta` (not merely static linear weights) over 200 resolved trades to earn live capital promotion; otherwise `gbdt_meta` runs the book and the LLM remains an explanatory/veto shadow layer. If the Red Team shows no discriminative power after 100 challenges it is cut and its 14 GB reclaimed.

**Market benchmark (added 2026-11-11, [ADR-0005](ADR/0005-nifty500-benchmark.md)):** beating the other arms is necessary, not sufficient. No arm or model drives the book unless, out of sample and after full Indian costs, it beats **both** `frozen_control` **and the Nifty 500 index** (price index — the free archive has no TRI, so a narrow win is a tie). As of 2026-11-11 the frozen control trails the Nifty 500 in every window tested (5 years: −7.6% vs +53.6%).

### The Five Architectural Surprises & Guardrails

1. **Surprise 1 — The Missing Middle & Promotion Bar:** A purely linear factor model leaves substantial non-linear alpha (e.g. momentum only works when vol is low and market regime is `TREND_UP`) on the table, while LLMs cannot compute or aggregate 1.4M rows of price interactions. `GBDT_META` fills this missing middle. Consequently, `COMMITTEE` (LLM) must beat `GBDT_META` (not merely static linear weights) over 200 resolved trades to earn live capital promotion; otherwise `GBDT_META` runs the book and the LLM remains an explanatory/veto shadow layer.
2. **Surprise 2 — Overlapping Label Leakage in Time-Series GBDTs:** A 10-session swing barrier started at $t-5$ is still open at $t$. Training a tree at $t$ on any path whose `exit_date >= d` leaks future index moves across all 500 stocks simultaneously. Enforce strict `exit_date < d` purging, a 10-session embargo, and uniqueness weighting (`uniqueness_w`).
3. **Surprise 3 — Shadow Recommendation Bias (`SHADOW_BUY` / `SHADOW_WATCH`):** Shadow outcomes accelerate `n_eff` by 10× for barrier calibration (`p_up`), but live portfolio sizing/bandit updates must weight live fills (`DETERMINISTIC`) separately from `SHADOW_WATCH` so unbought tail names don't skew portfolio-level Sharpe.
4. **Surprise 4 — Replay Determinism (`ADR-0002`) for ML Models:** Just as LLM token streams and `weights.v1.json` are SHA256-pinned in `RunManifest`, every nightly LightGBM model binary (`lgbm_meta_<run_id>.txt`) and isotonic calibrator must be hashed in `RunManifest` so `ta daily --replay` reproduces exact numbers without refitting.
5. **Surprise 5 — News & Event Ablation (`M7`):** Event/news features (`nx.news_features`) are tested both standalone (Rank-IC `>= 0.020`) and via a `GBDT_WITH_NEWS` vs `GBDT_PRICE_ONLY` out-of-sample Brier Skill Score (`ΔBSS > 0`) gate before entering live models.

### Labelling, attribution, calibration

- **Triple-barrier, ATR-scaled** (fixed-% barriers make high-vol stocks look predictable). `H` must equal the live engine's time-stop exactly, or labels and execution disagree.
- Pessimistic same-bar resolution, circuit-block handling, gap-through-stop → `stop_slippage_bps` as a KPI. `uniqueness_weight = 1/(concurrent overlapping labels)` because overlapping windows aren't IID.
- Benchmark against **NIFTY 50 TRI**, not the price index — otherwise you flatter yourself ~1.2%/yr of dividends.
- Each resolved rec explodes into one attribution row **per reason code** × sector × regime × sleeve × horizon.
- Beta-Binomial posteriors with exponential forgetting (60-trade half-life). **`n_eff ≥ 30` before a posterior touches sizing** — below that it's display-only.
- **Calibration is the primary KPI.** The system's job is not to be right, it's to know how right it is. Isotonic map from stated → realised confidence; **sizing uses `calibrated_p` only, never raw LLM confidence.** Brier/ECE reported *per slice*, never only in aggregate — aggregate calibration can look perfect while every sub-population is wrong in offsetting directions.
- Thompson sampling across six strategy sleeves, floor 5% / ceiling 40%. **The 5% floor is the exploration term** — a sleeve starved to zero stops generating data and its posterior freezes forever.

### Guardrails against the loop overfitting itself

Weights move **weekly, not daily**; capped ±20% per update and ±50% cumulative; **frozen for 10 sessions after a regime change**; a global trial counter feeds a Deflated Sharpe (*if you can't state N, the Sharpe is uninterpretable*); the critic writes labels but may **never** mutate a weight.

---

## Repo layout

```
/Users/msuhi00/Projects/trading-agent/
├── .python-version              # 3.13  (NOT the default 3.15.2)
├── uv.lock                      # COMMITTED — every scheduled job runs `uv run --frozen`
├── justfile                     # up / down / api / ui / daily / backfill / test / pit
├── docs/ADR/                    # one file per irreversible decision
├── docs/{DATA_SOURCES,COST_MODEL,RUNBOOK}.md
├── packages/
│   ├── core/      # ZERO I/O. ports, DTOs, closed enums, calendar, costs, read_pit, timefmt
│   ├── datafeed/  # sources/, registry (priority+failover), http (token bucket), parse/, adjust/
│   ├── news/      # feeds, liveness, extract/, link, nlp/, cluster/, features
│   ├── quant/     # factors/, xsec, screener/, labels, cv, regime, ml/, stats, backtest/
│   ├── llm/       # session (triple mutex), backends/, dossier, committee, personas/, validators
│   └── ledger/    # broker/{base,paper,upstox}, events (hash-chained), manifest (RunManifest
│                  # + per-generation record — see "Replay means replay-from-record"),
│                  # rebuild, replay, outcomes, attribute, posteriors, bandit,
│                  # calibration, kpis, risk (the capital-protection gates)
├── apps/{api,ui}/
└── infra/{models,launchd,clickhouse,mongo}/
```

Legacy files (`test.py`, `load_test_tuning.py`, `ca-bundle.pem`, `results.json`) move to `legacy/` — they're unrelated Albertsons work, not deleted.

---

## Build milestones

**Do not advance on a failing gate.** Milestones 1–3 contain **zero LLM calls**.

| # | Deliverable | Gate |
| --- | --- | --- |
| **M0** | uv workspace, `core` with zero I/O, DBs running with caps, test skeleton | `ta cost --delivery --notional 100000` prints **₹238**; Mongo cache = 2 GiB; swap 0.00M; the PIT leakage test **fails against a deliberately-leaky stub** |
| **M1** ⭐ | **Real NSE data on screen.** Archive sources, 2y backfill, heat map + 500-row screener | Open `127.0.0.1:5173` → live sector heat map + liquidity screener **from two unauthenticated GETs/day. No account, no API key, no LLM.** `/api/heatmap` p95 <30 ms |
| **M2** | Hash-chained ledger, PaperBroker, ~70 factors, hard gates, sizing, drawdown breakers | Dated BUY/SELL/HOLD cards with reason-code chips + equity curve vs NIFTY TRI — **zero LLM. Already a complete, honest, useful system.** Then **freeze `weights.v1.json` and git-tag it — it is the control arm forever.** |
| **M3** | Backtester sharing `simulate_fill()`/`apply_costs()` *literally* with the paper broker | Replaying history through the paper engine reproduces the backtest curve to rounding. **This invariant kills live/backtest drift.** Frozen control must show positive deflated expectancy *after* full Indian costs — or fix the screener before adding anything |
| **M4** | LLM plumbing, triple mutex, validators, **the generation record + OTel spans + Phoenix viewer** | **Measure 5 numbers on this machine before freezing the schedule** (cold load, tok/s + peak RSS at 1k/8k/32k, schema-compliance over 100 objects, Prefect idle RSS, real ClickHouse compression). Every published figure in the research is crowd-sourced or unverifiable |
| **M5** | The committee | One symbol, hand-pasted data, full Bull→Bear→Rebuttal→Risk→PM debate, every number traceable. Schema-rejection <5%, peak RSS ≤23 GB, swap flat. **Committee starts in shadow** |
| **M6** | Unattended operation (Prefect + launchd + DLQ) | Close the laptop at 18:00, open at 09:00 — the run completed. `kill -9` mid-committee must not deadlock the GPU. One full week unattended |
| **M7** | News + filings depth | Zero `news_features` rows carry a crawl timestamp. Parser survives real data (**21% of NSE announcement items have no `<link>` at all**). News must improve out-of-sample rank-IC **or become display-only** |
| **M8** ⭐ | **The learning loop closes** — attribution, posteriors, calibration, four-arm comparison | A chart that answers *"is this learning or fooling itself?"* at a glance. **`random` must be clearly worst after costs — if it isn't, the cost model is wrong.** Then the committee promotion gate |
| **M9** | Red team, weekly critic, lesson cards | Red-team precision must show discriminative power or C8 is cut |
| **M10** | Intraday leg, ML ranker, regime | Each must beat the frozen control in walk-forward before touching live sizing |
| **M11** | Broker provision (not execution) | Read SEBI/HO/MIRSD/MIRSD-PoD/P/**CIR**/2025/0000013 directly before any live-order code |

**M1 is usable in under a week. M2 is a complete honest trading system in under three. To M9: ~45–50 working days.**

---

## Verification

- `uv run --frozen pytest` — unit tests, no services needed.
- `tests/pit/` — the leakage suite. Monkeypatches the ClickHouse client and **fails the build** on any unbounded read of a PIT table. *If only one thing here is implemented properly, make it this.* **Hardened 2026-10-31:** the guard previously proved only that a `knowledge_time` bound *existed*, so `WHERE knowledge_time <= now()` passed it — a live look-ahead path through the most important guard in the system. It now rejects any bound whose right-hand side is not a literal or a driver-bound parameter, and `assert_pit_bounded(sql, as_of)` additionally requires the bound to *equal* the run's as-of. Note this remains a **build-time, syntactic** check over query text; the runtime proof is the `traceparent` span (see *Observability*).
- `tests/test_backtest_matches_paper.py` — the anti-drift invariant.
- `tests/test_ledger_rebuild.py` — `rebuild_from_ledger() == stored`.
- `tests/test_committee_shape.py` — the stage-name sequence of `COMMITTEE_STEPS` equals a golden constant, and an AST walk asserts no conditional branching in the persona sequence. This is what makes *"control flow is fixed and must never vary"* a machine-checked claim rather than a comment.
- `tests/test_gpu_mutex.py` — two concurrent `ModelSession.__enter__` calls must serialise.
- `tests/test_risk_gates.py` — every capital-protection rule has a test that constructs a breaching portfolio and asserts the gate rejects it. A risk rule without a failing-case test is a comment.
- End-to-end: `ta daily --date 2026-10-15 --replay` re-derives that day's recommendations from the recorded generations and reproduces them byte-identically. **Re-derives, never recalls** — see *Replay means replay-from-record*.
- Visual: `just up && just ui` → `http://127.0.0.1:5173`.

---

## Honest limits

- **Realistic target: Sharpe 0.8–1.2, max DD 12–18%, hit rate 45–55%, profit factor 1.3–1.6.** Any backtest showing Sharpe >2 on daily Indian equities is a bug — look-ahead, survivorship, or an unadjusted split.
- **~60% of the engineering here produces zero alpha** (point-in-time correctness, the cost model, the ledger, the four arms, calibration). That ratio is the point: look-ahead bias, underestimated costs, and a loop that optimises its own metric are what kill systems like this. No model sophistication survives any of them.
- **The LLM layer may add nothing, and the design is built to say so.** Quant-Arena (9 frontier models, 38 configurations, sealed test year, realistic costs) found only 13/38 positive against an equal-weight benchmark; top-decile picks often *underperformed* middle deciles. A 2026 audit found only 1 of 19 closed-loop LLM-portfolio studies used an explicit transaction-cost model. A local 4-bit model will be worse than any of those. Hence the permanent `deterministic` shadow arm and the pre-committed demotion rule.
- **On day trading specifically:** the design gives you intraday *management* plus one conditional breaking-news escalation, not a resident intraday reasoner. SEBI's own data: 71% of individual intraday cash traders lost money in FY2022-23, rising to 80% among the most frequent. A Python orchestrator on a laptop has no structural edge against colocated participants. **This is where I expect to disappoint you, and I'd rather say it now than in month four.** If you want a true intraday reasoning loop, say so — it changes the memory budget, the data layer and the orchestrator shape, and it is far cheaper to design in than to bolt on.
- **What you genuinely have that funds don't:** patience (no redemptions, you can sit in cash for weeks), size (₹5L trades names a fund can't touch, with near-zero impact), breadth (500 names scanned nightly with consistent rules), and process. **What you don't have:** speed, data advantage, colocation, or capital to diversify across uncorrelated strategies.
