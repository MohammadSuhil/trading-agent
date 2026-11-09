# C4 level 1 — system context

Who uses the system and what it reaches outside itself. Everything inside the dashed box runs on
one laptop; nothing listens on a non-loopback interface.

```mermaid
graph TB
    user["<b>Operator</b><br/>single user, single machine<br/>reads the dashboard, runs the CLI"]

    subgraph local["Trading Agent — localhost only"]
        ta["<b>Trading Agent</b><br/>Ingests NSE end-of-day data, computes factors<br/>and trend states, screens the Nifty 500,<br/>sizes against capital-protection rules,<br/>records every decision and every rejection.<br/><br/>Python 3.13 · ClickHouse · FastAPI · React"]
    end

    nse["<b>nsearchives.nseindia.com</b><br/>UDiFF bhavcopy, one zip per session<br/>~2,700 EQ rows: OHLC, volume, turnover,<br/>trade count, ISIN<br/><i>plain GET, no auth, 1 req/s</i>"]
    nifty["<b>niftyindices.com</b><br/>Index constituent CSVs<br/>carries NSE's official Industry column<br/><i>plain GET, no auth, 1 req/2s</i>"]
    master["<b>nsearchives EQUITY_L.csv</b><br/>listed-security master<br/><i>headers ship with leading spaces</i>"]

    broker["<b>Broker API</b><br/>Angel One / Upstox<br/>minute candles, order placement"]
    feeds["<b>RSS / filings</b><br/>ET, Moneycontrol, NSE announcements"]
    llm["<b>Local LLM</b><br/>llama-server, 4-bit GGUF"]

    user -->|"HTTP :8000<br/>reads only"| ta
    user -->|"uv run --frozen ta ..."| ta

    ta -->|"HTTPS GET, archive files only"| nse
    ta -->|"HTTPS GET"| nifty
    ta -->|"HTTPS GET"| master

    ta -.->|"M10 — not built"| broker
    ta -.->|"M7 — not built"| feeds
    ta -.->|"M4/M5 — not built"| llm

    classDef built fill:#1f4e5f,stroke:#2d7a94,color:#fff
    classDef ext fill:#2c3e50,stroke:#4a6572,color:#fff
    classDef notbuilt fill:#2c3e50,stroke:#7a7a7a,color:#aaa,stroke-dasharray:6 4
    classDef person fill:#3d2b56,stroke:#6b4e8f,color:#fff
    class ta built
    class nse,nifty,master ext
    class broker,feeds,llm notbuilt
    class user person
```

## What is deliberate here

**Three HTTP sources, all unauthenticated, all published archive files.** `www.nseindia.com`
returns 403 to non-browser clients and `query1.finance.yahoo.com` returned 429 from this machine.
The design routes **around** both rather than through them: no cookie-warming, no session
emulation, no bot-protection defeat. NSE's Terms of Use prohibit systematic automated collection,
and fetching a published CSV is a materially different act from emulating a browser to reach an
internal JSON API.

**No egress except those three hosts.** No telemetry (`DO_NOT_TRACK=1` and
`PREFECT_SERVER_ANALYTICS_ENABLED=false` are set in the launchd plists), no cloud services, no
model APIs, no account anywhere. The entire M1 deliverable runs on **two unauthenticated GETs per
day**.

**One user, no auth, loopback only.** ClickHouse (8123/9000), mongod (27017), redis (6379) and
uvicorn (8000) all bind `127.0.0.1`. There is no authentication on any of them. This is a
single-user personal system and is not designed for scale — see
[../OPERATIONS.md §9](../OPERATIONS.md#9-security-posture).

**The dashed arrows are the honest part.** A broker adapter, a news pipeline and an LLM layer are
all specified in `docs/PLAN.md` and none exists. The system today is a deterministic screener with
an audit trail.
