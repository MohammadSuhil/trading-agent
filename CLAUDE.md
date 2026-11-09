# Trading Agent

Local, single-user, multi-agent stock recommendation + paper-trading system for Indian
equities (NSE/BSE). Day, swing and positional. Everything runs locally and free.

> **Continuity note.** This project was designed and bootstrapped in a session rooted at
> `/Users/msuhi00/Projects/python` (before the repo existed here). The full research,
> architecture and rationale are in [docs/PLAN.md](docs/PLAN.md). Current build state and
> hard-won gotchas are in [docs/HANDOFF.md](docs/HANDOFF.md). **Read HANDOFF.md before
> changing infra config** — four separate silent-failure modes are documented there.

## Core objective

The system must *measurably* improve from its own results. That is a schema and
evaluation-harness requirement, not a model requirement — which is why milestones 1–3
contain zero LLM calls. Retrofitting an audit trail is impossible: the data you needed
was never stored.

## Governing principles — these settle every ambiguous case

1. **The LLM never computes a number.** Every price, indicator, stop, target, size, cost
   and probability comes from Python/ClickHouse and is pre-printed into a fact sheet. The
   model selects among pre-validated candidates, explains, assigns a closed-enum reason
   code, states confidence, and may refuse.
2. **`knowledge_time` ≠ event date ≠ crawl time.** Every point-in-time table carries
   `knowledge_time`; all reads go through `ta_core.asof.build_pit_query`; a CI test fails
   the build on any unbounded read. **If only one thing stays correct, make it this.**
3. **Raw bytes are immutable.** `data/raw/YYYY/MM/DD/` is written *before* anything parses.
4. **One model resident at a time**, enforced at three levels that fail differently.
5. **The evaluation harness exists before the strategy; the strategy before the model.**

## User's decisions (do not re-litigate)

| | |
| --- | --- |
| Broker | Angel One / Dhan account held. **Dhan historical is NOT free** (₹499+GST/mo, error 806). Angel One SmartAPI is free; Upstox is free and cleaner. Not needed until M10. |
| Paper capital | ₹5,00,000 → **7** concurrent positions (9 absolute backstop). The flat ₹15.34 DP charge sets a ₹50k position floor; that floor plus the 6.0% portfolio-heat cap is what yields 7, not the 10 this line used to claim. Enforced in `ta_ledger.risk`. |
| Universe | Ingest full market EOD (~2,712 symbols, costs nothing — one file). Score and trade Nifty 500; wider coverage needs no backfill. |
| Styles | All three. Design is swing + positional primary, with *deterministic* intraday management. |

## Commands

```bash
./infra/services.sh up|down|status   # ClickHouse + MongoDB (+ redis check)
./infra/smoke_test.sh                # 10 infra gates; exits non-zero on failure
./infra/validate_config.sh           # XML well-formedness (see HANDOFF gotcha #3)
uv run --frozen pytest               # all tests, no services needed
```

Always `uv run --frozen`. Python is pinned to **3.13** (`.python-version`): `vectorbt` and
`nautilus_trader` both cap at `<3.15`, and the system `python3` (3.15.2) additionally has
a broken CA store — every HTTPS call fails with `CERTIFICATE_VERIFY_FAILED`.

## Layout

- `packages/core` — **zero I/O**, stdlib + pydantic only. enums, costs, calendar, timefmt, asof.
- `packages/{datafeed,news,quant,llm,ledger}` — to be built
- `apps/{api,ui}` — FastAPI + Vite/React
- `infra/` — configs, launchd plists (written, **not installed**), service scripts
- `legacy/` — unrelated Albertsons scratch files, preserved

## Memory reality — tighter than it looks

48 GB total, but **~31 GB is typically already resident** (Outlook, VS Code, Chrome). Real
headroom is ~15–17 GB, not 48. The heavy reasoning stage runs 20:45–22:05 IST and expects
apps closed. The orchestrator must measure free memory before loading and step down the
ladder (Qwen3.6-35B-A3B 22.4 GB → gpt-oss:20b 12.9 GB → qwen3:14b 8.6 GB), stamping
`degraded_mode=true` when it does — a Q3-reasoned trade must be distinguishable from a
Q4-reasoned one in the ledger or the learning loop fits noise.

## Data sources — verified working

- **EOD**: `https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip`
  — plain GET, 200, 2,712 EQ rows/day with OHLC, volume, **turnover**, trade count, ISIN.
- **Index + industry**: `https://niftyindices.com/IndexConstituent/ind_nifty<N>list.csv`
  — carries the official `Industry` column (the heat-map grouping).
- `www.nseindia.com` returns **403** to non-browser clients; `query1.finance.yahoo.com`
  returned **429**. Use published archive files, never cookie-warmed scraping.

**NSE Terms of Use prohibit automated collection, redistribution, and use for "virtual
trading or simulation."** Posture: download published archive files only; never defeat bot
protection. Everything cookie-gated is optional and degrades to `news_coverage='PARTIAL'`.

## Style

Match the existing code: dense docstrings that explain *why* a guard exists and what
failure it prevents, doctests on anything with arithmetic, closed enums over free text.
