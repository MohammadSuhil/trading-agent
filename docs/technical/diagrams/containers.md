# C4 level 2 — containers

Processes, Python packages, datastores and the edges between them. Dashed = specified but not
built.

```mermaid
graph TB
    browser["<b>Browser</b><br/>127.0.0.1:8000 (or :5173 dev)"]

    subgraph procs["Processes — launchd-supervised"]
        api["<b>ta-api</b> · uvicorn<br/><code>com.tradingagent.api</code><br/>FastAPI, 13 GET endpoints<br/>serves apps/ui/dist as StaticFiles<br/>:8000"]
        cli["<b>ta</b> · CLI<br/><code>com.tradingagent.nightly</code><br/>StartCalendarInterval 18:45<br/><code>ta daily --days 7</code>"]
        ch["<b>ClickHouse 26.10.2.103</b><br/><code>com.tradingagent.clickhouse</code><br/>:8123 HTTP · :9000 native<br/>cap 5 GiB absolute"]
        mg["<b>mongod 9.0.4</b><br/><code>com.tradingagent.mongod</code><br/>:27017 · replica set rs0<br/>WT cache 2 GB"]
        rd["<b>redis 8.8.0</b><br/>:6379 · pre-existing brew install<br/><i>no launchd agent</i>"]
    end

    subgraph pkgs["Python workspace — uv, 6 members"]
        core["<b>ta_core</b> · ZERO I/O<br/>asof (PIT guard) · enums · costs<br/>calendar · timefmt · glossary"]
        feed["<b>ta_datafeed</b><br/>PoliteClient · udiff/constituents parsers<br/>NSEArchiveSource · <b>GuardedClient</b>"]
        quant["<b>ta_quant</b><br/>factors.price · trend · barriers<br/>pipeline (pure) · nightly (I/O)"]
        ledger["<b>ta_ledger</b><br/>risk.py — gates, sizing, rule_states<br/><i>broker/ is empty</i>"]
    end

    subgraph disk["Filesystem"]
        raw["<b>data/raw/</b> · 86 MB, 497 files<br/>immutable source bytes<br/>written BEFORE any parsing"]
        logs["data/logs/ · 7.5 MB"]
        dist["apps/ui/dist<br/>Vite + React 18 + Tailwind<br/>d3 treemap · lightweight-charts"]
    end

    llm["<b>llama-server</b><br/>4-bit GGUF, one model resident<br/><i>M4/M5 — not installed</i>"]

    browser -->|"GET /api/*"| api
    browser -->|"GET /"| api
    api --> dist

    api --> core
    api --> feed
    api --> ledger
    cli --> feed
    cli --> quant

    quant --> core
    quant --> feed
    quant --> ledger
    ledger --> core
    feed --> core

    feed -->|"httpx GET<br/>token bucket per host"| raw
    feed -->|"insert_arrow / query_pit<br/><b>every PIT read guarded</b>"| ch
    api -->|"query_pit — new client per request (~38 ms)"| ch
    api -->|"ping only"| mg
    api -->|"TCP connect only"| rd

    procs --> logs

    quant -.-> llm
    ledger -.->|"ev.ledger_events — no writer"| ch

    classDef built fill:#1f4e5f,stroke:#2d7a94,color:#fff
    classDef store fill:#4a3c1f,stroke:#8a7230,color:#fff
    classDef fs fill:#2f3b2f,stroke:#5a7a5a,color:#fff
    classDef notbuilt fill:#2c3e50,stroke:#7a7a7a,color:#aaa,stroke-dasharray:6 4
    class api,cli,core,feed,quant,ledger built
    class ch,mg,rd store
    class raw,logs,dist fs
    class llm notbuilt
```

## Reading this

**`ta_core` is at the bottom of every arrow and sends none.** It has zero I/O — stdlib plus
pydantic, nothing that opens a socket, reads a file or touches a database. That is what lets the
whole domain layer be tested with no services running: `uv run --frozen pytest` is green on a
machine with ClickHouse down.

**`GuardedClient` is the only door into ClickHouse**, and the point-in-time guard is installed
there rather than left to each caller. Both `ta_quant.nightly` and `apps/api` go through it; a
`query_admin` escape hatch exists for operational questions and a CI test greps to ensure it never
appears in decision code.

**`packages/llm` and `packages/news` do not exist.** `packages/ledger` contains `risk.py` only —
no hash-chained event ledger, no paper broker, so `ev.ledger_events`, `ev.positions`, `ev.outcomes`
and `ev.equity_daily` all have zero rows and no writer.

**Redis and MongoDB are load-bearing only on the health panel.** Mongo holds zero application
documents (only `admin`, `config`, `local`); Redis has no Python client in the lock file. The token
bucket that does exist is in-process (`threading.Lock` in `ta_datafeed.http`). Both are running,
configured and capped — ready, not used.

**There is no orchestrator process.** PLAN.md names Prefect 3.9; it is not in `uv.lock`. Scheduling
is one launchd `StartCalendarInterval`.

## Process lifecycle

| Container | Managed by | Restart policy |
| --- | --- | --- |
| ClickHouse | `com.tradingagent.clickhouse` | `RunAtLoad`, `KeepAlive=true` |
| mongod | `com.tradingagent.mongod` | `RunAtLoad`, `KeepAlive=true` |
| ta-api | `com.tradingagent.api` | `RunAtLoad`, `KeepAlive={SuccessfulExit:false}`, `ThrottleInterval=10` — restart on crash, **not** in a tight loop if it cannot bind |
| nightly | `com.tradingagent.nightly` | calendar only, `RunAtLoad=false`, `ExitTimeOut=1800`, `ProcessType=Background`, `LowPriorityIO` |
| redis | nothing | assumed running from `brew services` |

All four agents invoke absolute paths (`/opt/homebrew/bin/uv`, `infra/bin/clickhouse`) because
**launchd execs without a shell** — no PATH, no venv activation, no profile.
