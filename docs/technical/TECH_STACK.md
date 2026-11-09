# Tech stack

Every runtime dependency with its **actually installed** version, what it is for, and why it was
chosen. Versions below were read from `uv.lock`, `uv pip list`, `apps/ui/package.json`,
`npm ls --depth=0` and the running binaries on **2026-10-31**. Nothing here is quoted from
documentation.

Companion documents: [OPERATIONS.md](OPERATIONS.md) (running it), [LLD.md](LLD.md) (how it is used),
[DATA_MODEL.md](DATA_MODEL.md).

---

## 1. The hard constraints

These four facts determine most of the stack. They are not preferences.

### 1.1 Python is pinned to 3.13

`.python-version` contains `3.13`. Every `pyproject.toml` in the workspace declares
`requires-python = ">=3.13,<3.15"`. Installed interpreter, verified:

```
Python 3.13.18 (main, Oct 26 2026, 13:59:02) [Clang 23.1.3]
```

Two independent reasons, either of which alone would be sufficient:

**(a) The ceiling.** `vectorbt` 1.2.0 declares `requires_python <3.15,>=3.11`; `nautilus_trader`
1.231.0 declares `<3.15,>=3.12` with no cp315 wheel; `readability-lxml` and `faust-cchardet` also
cap below 3.15. Neither backtesting library is installed yet (M3), but pinning *after* writing code
against a newer interpreter is a rewrite, so the pin is taken up front. 3.14 was rejected because it
drops `outlines`, which the M4 GBNF/structured-output layer wants.

**(b) The floor is unusable.** The machine's default `python3` is a **3.15.2 python.org build with
no CA bundle**. Every HTTPS call fails with `CERTIFICATE_VERIFY_FAILED`. For a project whose entire
data layer is HTTPS GETs to `nsearchives.nseindia.com` and `niftyindices.com`, that interpreter is
not a candidate at any version.

`uv` sidesteps both by installing and managing its own interpreter. **Always invoke through
`uv run --frozen`**; a bare `python3` in this repo is the broken 3.15.2.

### 1.2 No Docker, no Colima, no Podman

All three datastores run **natively**. From PLAN.md:

> Apple's `vz` hypervisor does not return freed guest memory to macOS
> ([lima#2789](https://github.com/lima-vm/lima/issues/2789), open), so `--memory 10` is a one-way
> high-water mark.

On a machine whose binding constraint is 15–17 GB of real headroom for a 22 GB model, a VM that
ratchets memory upward and never gives it back is disqualifying. The ban is at the stack level, and
it has a downstream consequence worth knowing: **Prefect hard-depends on the `docker` package**,
which is one of the reasons it is not installed (§4).

The Docker CLI 29.6.1, Colima (no VM created) and Podman Desktop are present on the machine from
unrelated work. Do not use them for this project.

### 1.3 Memory caps — the defaults would have killed this on day one

Three components default to "take whatever is available" on a 48 GB machine. All three failures are
**silent**: the service starts cleanly and simply grows.

| Component | Default on this box | Set to | Verified |
| --- | --- | --- | --- |
| ClickHouse `max_server_memory_usage` | `0` = unlimited ≈ **43 GB** | `5368709120` (5 GiB), **absolute** | `SELECT value FROM system.server_settings` → `5368709120` (default column reads `0`) |
| MongoDB WiredTiger cache | `max(0.5×(RAM−1GB), 256MB)` = **23.5 GB** | `cacheSizeGB: 2` | `db.serverStatus().wiredTiger.cache["maximum bytes configured"]` → `2147483648` |
| `llama-server` | `--models-max 4`, margin `0` (accounting **off**) | `--models-max 1 --models-memory-margin 8192` | **unverified: llama.cpp is not installed (M4)** |

The arithmetic that makes these non-negotiable, from PLAN.md:

```
apps resident today (Outlook 8.2 + VS Code 4.8 + Chrome 2.4 + …)   30.8 GB
+ Qwen3.6-35B-A3B @ 32k ctx                                        22.4 GB
  ------------------------------------------------------------------------
                                                                   53.2 GB of 48 -> swap death
```

A 23.5 GB Mongo cache plus a 43 GB ClickHouse budget leaves nothing for either the user's apps or
the model.

**The ClickHouse cap has a specific trap, documented in HANDOFF.md as silent failure #2.** Setting
`max_server_memory_usage_to_ram_ratio` to `0` in an attempt to "disable the ratio" computes
`48 GB × 0 = 0`, and ClickHouse treats `0` as **unlimited** — silently cancelling the absolute cap
you just set. The config looked correct and the server started happily with no cap at all. Leave
the ratio at its `0.9` default and set only the absolute; the effective limit is
`min(5 GiB, 0.9 × 48 GB) = 5 GiB`. Verified live: ratio is `0.9`, absolute is `5368709120`.

Other ClickHouse tuning in `infra/clickhouse/config.xml`, all verified in effect:

| Setting | Value | Default | Why |
| --- | --- | --- | --- |
| `background_pool_size` | 8 | 16 | a laptop does not need 16 merge threads fighting the LLM |
| `background_merges_mutations_concurrency_ratio` | 2 | 2 | — |
| `mark_cache_size` | 536870912 (512 MiB) | 5368709120 (5 GiB) | the default alone equals the whole server budget |
| `uncompressed_cache_size` | 0 | 0 | the panel is 35 MiB; caching decompressed blocks buys nothing |
| `max_concurrent_queries` | 8 | 0 (unlimited) | single user |
| `max_thread_pool_size` | 2000 | 10000 | |
| `max_memory_usage` (per query, `users.xml`) | 3 GiB | — | distinct from the server-wide cap |
| `max_bytes_before_external_group_by` | 1.5 GiB | — | spill rather than OOM |

`background_pool_size` has its own trap — HANDOFF silent failure #4. ClickHouse requires
`number_of_free_entries_in_pool_to_execute_mutation` (default 20) to be **less than**
`background_pool_size × background_merges_mutations_concurrency_ratio`. With pool 8 × ratio 2 = 16,
the default 20 exceeds it and mutations throw **Code 36**. `config.xml` lowers the three
`merge_tree` thresholds to 4.

### 1.4 No vendor telemetry, no egress

PLAN.md's no-egress constraint was already violated by the incumbent stack rather than by anything
it rejected. The two switches are set as `EnvironmentVariables` in the launchd plists:

```xml
<key>PREFECT_SERVER_ANALYTICS_ENABLED</key><string>false</string>   <!-- else sens-o-matic.prefect.io -->
<key>DO_NOT_TRACK</key><string>1</string>                           <!-- else api2.amplitude.com -->
```

`com.tradingagent.nightly.plist` sets both; `com.tradingagent.api.plist` sets `DO_NOT_TRACK` only.
Neither is set by `infra/services.sh`, so a manually-started session does not carry them — harmless
today because nothing that phones home is installed.

---

## 2. Python runtime dependencies — actually installed

`uv.lock` contains **52 `[[package]]` entries**: 45 third-party, 6 workspace members
(`ta-core`, `ta-datafeed`, `ta-quant`, `ta-ledger`, `ta-api`, `ta-cli`) and the root
`trading-agent`. Two entries (`colorama`, `tzdata`) are platform-conditional and not installed on
macOS; `uv pip list` reports 44 third-party packages + 6 editable workspace members.

> **Correction to PLAN.md.** ADR-0001's dependency-surface table states *"current lockfile 19
> packages"* and compares it against DBOS-12 / LangGraph-41 / Prefect-103. The real number is
> **45 third-party**. The argument it supports (a framework's checkpointer duplicates the mandatory
> generation record) does not depend on the count, but the count should be re-struck before anyone
> cites it.

### Direct dependencies, by package

| Workspace package | Declares |
| --- | --- |
| `ta-core` | `pydantic>=2.9`, `pydantic-settings>=2.6`, `pyyaml>=6.0` |
| `ta-datafeed` | `ta-core`, `httpx>=0.28`, `polars>=1.17`, `pyarrow>=18.0`, `clickhouse-connect>=0.8`, `pymongo>=4.10` |
| `ta-quant` | `ta-core`, `ta-datafeed`, `polars>=1.17`, `numpy>=2.0` |
| `ta-ledger` | `ta-core`, `ta-datafeed`, `ta-quant`, `polars>=1.17`, `clickhouse-connect>=0.8` |
| `ta-api` | `ta-core`, `ta-datafeed`, `ta-quant`, `ta-ledger`, `fastapi>=0.115`, `uvicorn[standard]>=0.34` |
| `ta-cli` | `ta-core`, `ta-datafeed`, `ta-quant` — entry point `ta = ta_cli.__main__:main` |
| dev group | `pytest>=8.3`, `pytest-cov>=6.0`, `pytest-asyncio>=0.25`, `ruff>=0.8`, `httpx>=0.28` |

### Installed versions

**Core runtime**

| Package | Version | For | Chosen over |
| --- | --- | --- | --- |
| **polars** | **2.1.0** | the entire dataframe layer: parsing, factors, trend, barriers, API serialisation | pandas. The factor stack is ~2,700 symbols × 500 sessions of `over("symbol")` window functions; polars' expression engine runs the whole panel in well under a second where a per-symbol pandas loop takes minutes. The `over()` + strictly-trailing-window idiom is also what makes "no factor may see the future" a readable property rather than an audit. |
| polars-runtime-32 | 2.1.0 | polars' compiled runtime (32-bit index build) | — |
| **numpy** | **2.5.4** | transitive numeric support | — |
| **pyarrow** | **26.0.0** | Arrow interchange | **explicit, not transitive.** `clickhouse-connect`'s fast path is `insert_arrow`, and `polars.to_arrow()` needs pyarrow at runtime even though polars itself does not. The `pyproject.toml` comment says so. |
| **clickhouse-connect** | **1.11.0** | the only ClickHouse driver used | `clickhouse-driver` (native protocol). `clickhouse-connect` speaks HTTP on 8123, has first-class Arrow support, and keeps the wire format debuggable with `curl --data-binary`. PLAN.md §Observability notes `clickhouse-driver` would be wanted later for `opentelemetry_traceparent` session propagation — that is an M4 concern and does not bind today. |
| **httpx** | **0.28.1** | `PoliteClient`'s transport | `requests`. HTTP/2, a real timeout model, and a `Client` object whose lifetime maps cleanly onto the token-bucket context manager. |
| **pydantic** | **2.14.2** | DTO/validation substrate | — currently used only as a declared dependency; `ta_core` uses stdlib `dataclass(frozen=True, slots=True)` throughout. Pydantic is positioned for the LLM layer's structured outputs (M4). |
| pydantic-core | 2.50.2 | pydantic's Rust core | — |
| pydantic-settings | 2.16.2 | config loading | `ta_core.settings` is not written yet |
| **pymongo** | **4.19.0** | MongoDB client | the only client. Used in exactly one place: `/api/health`'s ping. |
| **pyyaml** | **6.0.3** | holiday/cost YAML | no consumer yet; `config/` is empty |

**Web**

| Package | Version | For |
| --- | --- | --- |
| **fastapi** | **0.162.0** | the read-model API; also serves the built SPA via `StaticFiles` |
| **starlette** | **1.10.0** | FastAPI's ASGI core |
| **uvicorn** | **0.54.0** | ASGI server, `[standard]` extra |
| uvloop | 0.23.0 | uvicorn's event loop |
| httptools | 0.9.0 | uvicorn's HTTP parser |
| websockets | 17.4 | uvicorn `[standard]` |
| watchfiles | 1.3.0 | uvicorn `--reload` |
| h11 / httpcore | 0.16.0 / 1.0.9 | httpx transport |
| anyio | 4.15.1 | async compat |
| click | 8.5.0 | uvicorn's CLI |
| python-dotenv | 1.2.4 | uvicorn `[standard]` |

**Transitive support**

`annotated-doc` 0.0.5 · `annotated-types` 0.8.0 · `backports-zstd` 1.8.0 · `certifi` 2026.10.23 ·
`dnspython` 2.9.1 (pymongo SRV) · `idna` 3.20 · `lz4` 4.4.5 (ClickHouse compression) ·
`packaging` 26.3 · `pygments` 2.21.0 · `typing-extensions` 4.16.0 · `typing-inspection` 0.4.4 ·
`urllib3` 2.8.0

**`opentelemetry-api` 1.45.2 is in the lock file and nothing imports it.** It is staged for the M4
observability layer described in PLAN.md §Observability. `opentelemetry-sdk` is **not** present, so
there is no exporter and no span processor — the package is inert.

**Dev / test**

| Package | Version | For |
| --- | --- | --- |
| **pytest** | **9.2.0** | the suite; `addopts = "--doctest-modules -q"` |
| pytest-cov | 7.1.0 | coverage (not wired into CI) |
| pytest-asyncio | 1.4.1 | `asyncio_default_fixture_loop_scope = "function"` |
| coverage | 7.17.0 | pytest-cov's engine |
| iniconfig / pluggy | 2.3.1 / 1.7.0 | pytest internals |
| **ruff** | **0.17.3** | lint + format. `line-length = 110`, `target-version = "py313"` |
| httpx | 0.28.1 | `fastapi.testclient` transport |

### Notably absent

| Not installed | PLAN.md says | Reality |
| --- | --- | --- |
| **prefect** | "Prefect 3.9 on SQLite" is the orchestrator | **not in `uv.lock`.** Scheduling is a single launchd `StartCalendarInterval`. Prefect additionally hard-depends on the banned `docker` package and on `amplitude-analytics`, and auto-caches tasks called twice with the same inputs. |
| vectorbt, nautilus_trader | backtesting (M3) | not installed; they are nonetheless the reason Python is pinned <3.15 |
| ta-lib, pandas-ta | indicators | not installed. RSI/ADX/ATR are hand-rolled in `ta_quant.factors.price` with Wilder smoothing — deliberate, since matching the charts the user compares against matters more than library provenance |
| scikit-learn, torch, mlx | ML ranker (M10), LLM | not installed |
| redis (python client) | GPU mutex, token buckets, SSE | not installed. The token bucket is in-process (`threading.Lock`) |
| hmmlearn | regime HMM (M10) | not installed — and PLAN.md rules it out anyway (abandoned, last commit 2024-10-31); the HMM is to be hand-rolled |
| dhanhq, smartapi-python | broker (M11) | not installed |
| trafilatura, docling, feedparser | news/filings (M7) | not installed |

---

## 3. Services

All three run **natively**, started either by `infra/services.sh` or by launchd. See
[OPERATIONS.md](OPERATIONS.md).

### ClickHouse 26.10.2.103

```
ClickHouse local version 26.10.2.103 (official build).
```

Binary at `infra/bin/clickhouse`, **fetched with `curl` from the official GitHub release**, not
installed from Homebrew.

> **This is HANDOFF silent failure #1 and it must not be undone.** The Homebrew *cask* ClickHouse
> died with **exit 137 and zero log output** — macOS Gatekeeper killing a quarantined binary.
> Files fetched with `curl` carry no `com.apple.quarantine` attribute, which is why the curl path
> works. **Do not reinstall the cask. Do not strip the quarantine attribute** — that was correctly
> refused as a security action.

Config `infra/clickhouse/config.xml` + `users.xml`. Listens `127.0.0.1` only: HTTP **8123**, native
**9000**. Server timezone `Asia/Kolkata`. Data at `data/clickhouse/`.

**Chosen for** columnar scans over the daily panel, `ASOF JOIN` (which kills a class of look-ahead
bugs structurally), and an HNSW vector index — meaning no fourth datastore is needed when
embeddings arrive. Measured compression on the real panel: **3.59×**
([DATA_MODEL.md §3](DATA_MODEL.md#3-storage-footprint-measured)).

### MongoDB 9.0.4 Community

```
db version v9.0.4
```

Homebrew binary `/opt/homebrew/bin/mongod`, config `infra/mongo/mongod.conf`, data `data/mongo/`.
`127.0.0.1:27017`. **Single-node replica set `rs0`, PRIMARY** — verified.

The replica set is **required, not optional**: change streams, transactions and `mongodump --oplog`
all need one, even single-node. Retrofitting a replica set onto a standalone deployment is an
operation; starting as one costs nothing.

**Currently holds zero application data** — only `admin`, `config`, `local`. See
[DATA_MODEL.md §6](DATA_MODEL.md#6-mongodb).

### Redis 8.8.0

```
redis_version:8.8.0
```

Pre-existing Homebrew install, `127.0.0.1:6379`. **Not managed by this project**: `services.sh up`
and `down` do not touch it, there is no launchd agent for it, and there is no Python client in the
lock file. `services.sh status` and `/api/health` merely ping it.

Three jobs are planned (GPU-mutex backstop, token buckets, pub/sub → SSE); none are implemented.

### Not installed

`llama.cpp` / `llama-server`, Ollama, LM Studio, MLX, and all model weights. The entire LLM layer is
M4+. `GET /models` assertions, the triple mutex and the adaptive model ladder exist only as prose in
PLAN.md.

---

## 4. Frontend

Node **v22.16.0**, npm **10.9.2**. `apps/ui/`, `@trading-agent/ui@0.1.0`, ESM.

> `apps/ui/` is being edited concurrently by another agent. Versions below are from
> `package.json` and `npm ls --depth=0` as of 2026-10-31.

**Runtime dependencies** (4 — deliberately small)

| Package | Declared | Installed | For | Chosen over |
| --- | --- | --- | --- | --- |
| **react** | ^18.3.1 | **18.3.1** | UI | — |
| **react-dom** | ^18.3.1 | **18.3.1** | DOM renderer | — |
| **d3** | ^7.9.0 | **7.9.0** | the industry **treemap** on the heat map | a charting library. `d3.treemap` is the layout; nothing else in d3 is used for charts |
| **lightweight-charts** | ^5.2.1 | **5.2.1** | candlestick/volume panes on the Analysis view | Recharts/Chart.js. TradingView's library is ~45 KB gzipped, handles 260-bar OHLC panes natively, and looks like what a trader expects |

**Dev dependencies**

| Package | Declared | Installed | For |
| --- | --- | --- | --- |
| **vite** | ^7.3.7 | **7.3.7** | dev server (`:5173`) + production bundler |
| @vitejs/plugin-react | ^5.2.0 | 5.2.0 | React fast refresh |
| **typescript** | ~5.9.3 | **5.9.3** | `npm run build` is `tsc --noEmit && vite build` — types gate the bundle |
| **tailwindcss** | ^3.4.19 | **3.4.19** | styling |
| postcss | ^8.5.6 | 8.5.32 | Tailwind's processor |
| autoprefixer | ^10.4.21 | 10.6.2 | |
| @types/react | ^18.3.12 | 18.3.31 | |
| @types/react-dom | ^18.3.1 | 18.3.7 | |
| @types/d3 | ^7.4.3 | 7.4.3 | |

**No SSR, no Next.js.** PLAN.md: *"SSR is pointless for localhost."* The build output
`apps/ui/dist` is served by FastAPI as `StaticFiles(html=True)`, mounted last at `/` so it never
shadows `/api/*`, with SPA fallback to `index.html`.

**shadcn/ui is named in PLAN.md and is not installed** — there is no `components.json`, no
`class-variance-authority`, no Radix. Styling is plain Tailwind.

---

## 5. Build and run tooling

### uv 0.13.6

```
uv 0.13.6 (Homebrew 2026-10-26 aarch64-apple-darwin)
```

Manages the interpreter, the workspace, the lockfile and the virtualenv. `uv.lock` is **committed**.

**Always `uv run --frozen`.** `--frozen` refuses to re-resolve, so a scheduled job can never
silently upgrade a dependency mid-run. The launchd plists invoke `/opt/homebrew/bin/uv` by
**absolute path** with `--directory`, and the nightly plist says why:

> `uv run --frozen` is used rather than a bare python because launchd execs without a shell: there
> is no PATH, no venv activation and no profile. The absolute uv path is deliberate.

Workspace members are declared in the root `pyproject.toml` under `[tool.uv.workspace]` and wired
with `[tool.uv.sources] … { workspace = true }`, so all six install editable.

### hatchling

Build backend for all six workspace packages: `[tool.hatch.build.targets.wheel] packages =
["src/ta_<name>"]`. Chosen because it is PEP 517 with no configuration beyond that line.

### pytest configuration

```toml
testpaths = ["tests", "packages", "apps"]
addopts = "--doctest-modules -q"
pythonpath = ["packages/*/src", "apps/api/src"]
norecursedirs = ["apps/ui", ".venv", "data", "infra", "legacy", "node_modules"]
```

`--doctest-modules` makes the arithmetic docstrings in `costs`, `asof`, `calendar`, `timefmt`,
`risk` and `barriers` **executable tests** — 21 of the 107. `norecursedirs` excludes `apps/ui`
because the UI build output is not Python and must never be imported by collection.

### ruff

`line-length = 110`, `target-version = "py313"`. `ruff check` is run by hand; there is no CI and no
pre-commit hook.

---

## 6. Version-skew hazards

Things that will break on a fresh machine or a future upgrade.

| Hazard | Detail |
| --- | --- |
| **`python3` is 3.15.2 with no CA store** | Any command not routed through `uv run` will fail HTTPS with `CERTIFICATE_VERIFY_FAILED`. `infra/apply_schema.sh` and `infra/validate_config.sh` call bare `python3`, but only for local XML/SQL parsing — no network — so they are safe. |
| **polars 2.x is a major version** | The code uses `pl.when/then/otherwise`, `rolling_*`, `ewm_mean(min_samples=)`, `int_range(pl.len()).over()`, `group_by().last()`, `unique(subset=, keep=)`. `min_samples` was `min_periods` in polars 0.x. Do not loosen `polars>=1.17` without re-running the suite. |
| **ClickHouse 26.10 is recent** | `system.server_settings` exposing a `default` column, and `DESCRIBE` codec reporting, are version-dependent. The smoke test asserts the literal string `5368709120`. |
| **Starlette 1.10 deprecation** | `uv run --frozen pytest` emits `StarletteDeprecationWarning: Using 'httpx' with 'starlette.testclient' is deprecated; install 'httpx2' instead`. The suite passes; it will not forever. |
| **npm installs float above `package.json`** | `autoprefixer` resolves to 10.6.2 against `^10.4.21`, `postcss` to 8.5.32 against `^8.5.6`, `@types/react` to 18.3.31 against `^18.3.12`. `package-lock.json` is committed; use `npm ci`, not `npm install`, for reproducibility. |
| **No lockfile covers the ClickHouse binary** | `infra/bin/clickhouse` is a curl-fetched release artifact with no checksum recorded anywhere in the repo. ADR-0002 makes exactly this point about the GGUF model binary; it applies here too. |
| **Hand-rolled indicators** | RSI/ADX/ATR are implemented locally. If `ta-lib` is ever added, the two will disagree at the edges and the factor stack must pick one. |
