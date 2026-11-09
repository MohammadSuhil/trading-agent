# Trading Agent

A local research and paper-trading dashboard for Indian equities. It ingests end-of-day market data, screens NIFTY 500 stocks, records risk-checked trade plans, and serves them through a local web app.

> **Early-stage project:** recommendations are experimental and have not demonstrated a trading edge. This is not financial advice or a live-trading system. Broker order execution and the planned LLM agents are not implemented.

## What works today

- Downloads NSE's published end-of-day bhavcopy archive and stores daily bars locally.
- Computes price, volume, trend, and market-breadth features; scores and screens the NIFTY 500 by default.
- Applies per-stock liquidity, position-sizing, industry, and portfolio-risk gates before publishing recommendations.
- Records recommendations and market context in ClickHouse and serves a local FastAPI + React dashboard.
- Provides a CLI for ingestion, daily runs, status, and cost calculations.

The system uses deterministic Python calculations for prices, indicators, stops, targets, costs, and probabilities. The current probability estimate is historical and should not be treated as calibrated until enough recommendations have resolved. See the [calibration endpoint](docs/technical/API.md#api-calibration) and [simulation endpoint](docs/technical/API.md#api-simulation) for their evidence thresholds.

## Important limitations

- No broker connection or live order placement is implemented. Recommendations are plans, not executions.
- LLM-based analysis, autonomous agents, and a validated learning loop are planned, not current features.
- There is no bundled market database. Runtime data, logs, local overrides, and downloaded NSE files are excluded from Git.
- Foreground infrastructure scripts resolve paths from the checkout. The launchd templates are still tailored to the original macOS machine and are not installed by setup. A fresh clone needs the macOS setup script before the data-backed app can run.
- The dashboard/API is intended for loopback use and has no authentication. Do not expose it to a network or the public internet.

## NSE data-use notice

The project's research notes state that NSE terms restrict automated collection, redistribution, and use for virtual trading or simulation. This project includes automated archive ingestion and paper-trading features. Review the current official terms and obtain any required permission or data rights before using those features. Do not bypass access controls or redistribute downloaded exchange files.

## Requirements

- macOS is the currently documented deployment environment; the infrastructure scripts use `launchd` and macOS utilities.
- [Homebrew](https://brew.sh/) is required by `scripts/setup_macos.sh` to install local system dependencies.
- Python 3.13 (`>=3.13,<3.15`) and [`uv`](https://docs.astral.sh/uv/).
- Node.js and npm for the dashboard (`apps/ui`); use the checked-in npm lockfile.
- For a data-backed run: local ClickHouse, MongoDB, and Redis services, plus the project-specific configuration under `infra/`.

## Developer checks

These checks do not require the databases:

```bash
uv sync --frozen
uv run --frozen pytest
uv run --frozen ruff check
cd apps/ui
npm ci
npm run build
```

## Setup on macOS

The setup script installs missing Homebrew prerequisites, the pinned Python version, Python/UI dependencies, a checksum-verified ClickHouse binary, local schemas, and the dashboard build. It starts the local databases but does not install unattended launchd jobs.

```bash
./scripts/setup_macos.sh
```

Use `--upgrade` to upgrade installed Homebrew formulas as well as install missing ones. Review the package changes before running it; database upgrades can affect existing local data.

## Start and run workflows

```bash
./scripts/run.sh start
./scripts/run.sh workflow
./scripts/run.sh workflow --end 2026-11-05 --days 7
./scripts/run.sh status
./scripts/run.sh build
```

`start` serves the dashboard and API at `http://127.0.0.1:8000` in the foreground; stop it with Ctrl-C. `workflow` runs the daily ingest and recommendation pipeline for the latest available session. The optional end date is useful for a historical run. See [Operations](docs/technical/OPERATIONS.md) for lower-level commands and launchd details.

For frontend development, run the API in one terminal and the Vite server in another:

```bash
./scripts/run.sh start
./scripts/run.sh ui
```

The Vite development server is at `http://127.0.0.1:5173` and proxies `/api` to the local API on port 8000.

Useful commands:

```bash
uv run --frozen ta status
uv run --frozen ta daily --end 2026-11-05 --days 7
./infra/smoke_test.sh
```

The daily command ingests a date range and screens the latest session that is available in the local database. A successful process exit does not necessarily mean every optional refresh stage succeeded; inspect the printed stage results and `data/logs/nightly.log`.

## Repository map

| Path | Purpose |
| --- | --- |
| `packages/core` | Shared enums, costs, calendar, time handling, and point-in-time query guards |
| `packages/datafeed` | NSE archive ingestion and ClickHouse access |
| `packages/quant` | Feature calculations, ranking, barrier probabilities, and nightly screening |
| `packages/ledger` | Paper portfolio, sizing, and risk gates |
| `apps/api` | FastAPI read API and static UI hosting |
| `apps/ui` | React dashboard |
| `infra` | Local database configuration, schema, and macOS service scripts |
| `docs` | Architecture, API, data model, operations, and project plan |

## Documentation

- [Operations and setup details](docs/technical/OPERATIONS.md)
- [Architecture](docs/technical/ARCHITECTURE.md)
- [HTTP API](docs/technical/API.md)
- [Data model](docs/technical/DATA_MODEL.md)
- [Project plan and known gaps](docs/PLAN.md)
- [Dashboard-specific guide](apps/ui/README.md)

## License

No license is currently included. Public visibility on GitHub does not by itself grant permission to reuse or redistribute this project.