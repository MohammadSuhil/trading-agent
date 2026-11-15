# Setup & Sync Guide for Other Machine

This document details how to sync and run all schema changes, data ingestion, table seeds, backtests, and nightly recommendation screens introduced in the `v2` Sortino-Momentum update.

---

## 1. Code & Environment Setup

```bash
# 1. Fetch latest branch and checkout
git fetch origin
git checkout fix/backtest-pit-baseline-equity-chart
git pull

# 2. Sync pinned Python 3.13 dependencies
uv sync --frozen

# 3. Install UI dependencies and build production assets
cd apps/ui
npm install
npm run build
cd ../..
```

---

## 2. Start Infra Services

```bash
# Start local ClickHouse + MongoDB daemons
./infra/services.sh up

# Verify config well-formedness
./infra/validate_config.sh
```

---

## 3. Apply Latest ClickHouse Schema (`schema_v2.sql`)

This creates and verifies all new and updated tables (`ev.backtest_runs`, `md.index_1d`, `ev.positions`, `ev.recommendations`, `ev.ledger_events`, `md.regime_daily`):

```bash
./infra/apply_schema.sh infra/clickhouse/schema_v2.sql
```

---

## 4. Ingest Reference Data & Market Bars

Run these commands to populate index constituents, symbol master, index history, and daily bars:

```bash
# 1. Listed security master
uv run --frozen ta symbols

# 2. Index constituents snapshot (Nifty 50, 100, 200, 500)
uv run --frozen ta constituents

# 3. Nifty 500 Daily Index close history (for market regime & ADR-0005 benchmark)
uv run --frozen ta index-history --start 2019-01-01

# 4. Ingest EOD Bhavcopy bars (1,250 sessions for full 5Y history)
uv run --frozen ta ingest --days 1250
```

---

## 5. Seed Historical Backtests (Control v1 vs Live v2 vs Nifty 500)

Pre-calculate and store the multi-window backtests in ClickHouse `ev.backtest_runs` so the Calibration / Honesty dashboard loads immediately:

```bash
# Seed 1,250-session (~5Y) backtest (Control v1 + Challenger v2)
uv run --frozen python -c "import uuid; from ta_quant.nightly import execute_backtest; rid=f'BT-{uuid.uuid4().hex[:12]}'; execute_backtest(sessions=1250, persist_results=True, run_id=rid); execute_backtest(sessions=1250, regime_filter=True, run_id=rid); print('Seeded 1250-session backtest')"

# (Optional) Seed 126-session (~6M) and 252-session (~1Y) backtests
uv run --frozen python -c "import uuid; from ta_quant.nightly import execute_backtest; rid=f'BT-{uuid.uuid4().hex[:12]}'; execute_backtest(sessions=126, persist_results=True, run_id=rid); execute_backtest(sessions=126, regime_filter=True, run_id=rid); print('Seeded 126-session backtest')"
uv run --frozen python -c "import uuid; from ta_quant.nightly import execute_backtest; rid=f'BT-{uuid.uuid4().hex[:12]}'; execute_backtest(sessions=252, persist_results=True, run_id=rid); execute_backtest(sessions=252, regime_filter=True, run_id=rid); print('Seeded 252-session backtest')"
```

---

## 6. Run Recommendations (Tonight's Screen)

To execute tonight's live screen using the `v2` Sortino-Momentum Rank-Buffer strategy:

```bash
uv run --frozen ta screen
```

Or run the full end-to-end nightly workflow (ingest today's bhavcopy $\to$ symbols master $\to$ constituents $\to$ screen $\to$ advance paper trading book):

```bash
uv run --frozen ta daily
```

---

## 7. Start the API and Dashboard

### Option A: Install background launchd services (Recommended on macOS)

```bash
./infra/install_launchd.sh install
./infra/install_launchd.sh status
```

### Option B: Run in terminal sessions

```bash
# Terminal 1: FastAPI Backend (http://127.0.0.1:8000)
uv run --frozen python -m ta_api.serve

# Terminal 2: Vite UI Dev Server (http://localhost:5173)
cd apps/ui && npm run dev
```

---

## 8. Verification Checks

Run the verification suite to ensure all parity and corporate-action invariants hold:

```bash
# Ensure codemap matches code
uv run --frozen python scripts/gen_codemap.py

# Run test suite
uv run --frozen pytest -q
```
