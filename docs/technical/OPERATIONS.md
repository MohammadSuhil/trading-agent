# Operations

How to start, stop, schedule, inspect and repair the system. Everything below was executed or read
from the running machine on **2026-10-31**.

Companion documents: [TECH_STACK.md](TECH_STACK.md) (versions and caps),
[DATA_MODEL.md](DATA_MODEL.md) (tables), [API.md](API.md), [TESTING.md](TESTING.md),
[LLD.md](LLD.md).

Foreground scripts resolve the repo root from their own path, and the datastore config uses paths
relative to that root. The launchd installer and plist templates still contain the original
machine's absolute paths; **moving the repo requires adapting those before installing launchd jobs.**

---

## 1. Quick reference

```bash
cd /Users/msuhi00/Projects/trading-agent

./infra/services.sh up|down|status        # ClickHouse + mongod (+ redis check)
./infra/smoke_test.sh                     # 10 infra gates; exits non-zero on failure
./infra/validate_config.sh                # XML well-formedness — run before every start
./infra/apply_schema.sh [file.sql]        # idempotent CREATE TABLE IF NOT EXISTS
./infra/install_launchd.sh install|status|uninstall

uv run --frozen pytest                    # 107 tests, no services needed
uv run --frozen ruff check
uv run --frozen ta status                 # what is in the database
uv run --frozen ta daily                  # the whole nightly run (~10 s incremental)

open http://127.0.0.1:8000                # dashboard + API from one process
curl -s localhost:8000/api/health
curl -s --data-binary "SHOW TABLES FROM md" http://127.0.0.1:8123/
```

**Always `uv run --frozen`.** A bare `python3` in this repo is the system 3.15.2 with a broken CA
store ([TECH_STACK.md §1.1](TECH_STACK.md#11-python-is-pinned-to-313)).

---

## 2. Services

### 2.1 Per-session: `infra/services.sh`

```bash
./infra/services.sh up      # start ClickHouse and mongod if not already up
./infra/services.sh down    # stop both, then WAIT for port 9000 to free
./infra/services.sh status  # ping all three
```

Current output:

```
clickhouse : Ok.
mongod     : 1
redis      : PONG
```

Three things it does that are not obvious:

- **`up` runs `validate_config.sh` first and aborts on failure.** This is the guard against HANDOFF
  silent failure #3 (§6.3).
- **`up` is idempotent** — it probes `curl /ping` and `mongosh ping` before starting anything.
- **`down` waits up to 30 s for TCP 9000 to be released.** ClickHouse holds the native port briefly
  after SIGTERM; restarting too fast fails with *"Address already in use"* and the new process
  **exits silently**. See §6.5.

`services.sh` starts processes with `nohup … &` and `pkill -f`. It does **not** manage Redis —
Redis is assumed running from a pre-existing `brew services` install.

### 2.2 Persistent: `infra/install_launchd.sh`

```bash
./infra/install_launchd.sh install    # copy to ~/Library/LaunchAgents, bootstrap
./infra/install_launchd.sh status
./infra/install_launchd.sh uninstall  # bootout + rm
```

These are **user agents** (`~/Library/LaunchAgents`), so no `sudo`. Install runs `plutil -lint` on
each plist before copying, then `launchctl bootout` followed by `launchctl bootstrap gui/$UID`.

Current status, verified:

```
  loaded   com.tradingagent.clickhouse  (spawn)
  loaded   com.tradingagent.mongod  (spawn)
  loaded   com.tradingagent.api  (running)
  loaded   com.tradingagent.nightly  (not)
```

> **HANDOFF.md §"Deliberately NOT done" is stale on this point.** It says the agents are *"written
> to `infra/launchd/` but not installed"*. All four **are** installed and loaded. HANDOFF's own
> header already contradicts that line (`./infra/install_launchd.sh status -> 4 agents loaded`).
> Trust `launchctl`.

`(spawn)` means launchd is managing the process with `KeepAlive`; `(not)` on the nightly agent is
normal — it is a calendar job, not a daemon, and has not fired yet.

### 2.3 The four launchd agents

| Label | Type | What it does |
| --- | --- | --- |
| `com.tradingagent.clickhouse` | daemon | runs `infra/bin/clickhouse server --config-file=infra/clickhouse/config.xml`, `RunAtLoad`, `KeepAlive=true`, cwd `infra/clickhouse` |
| `com.tradingagent.mongod` | daemon | runs `/opt/homebrew/bin/mongod --config infra/mongo/mongod.conf`, `RunAtLoad`, `KeepAlive=true` |
| `com.tradingagent.api` | daemon | runs `uv run --frozen --directory <root> uvicorn ta_api.main:app --host 127.0.0.1 --port 8000 --log-level warning`, `RunAtLoad`, `ThrottleInterval=10` |
| `com.tradingagent.nightly` | calendar | runs `uv run --frozen --directory <root> ta daily --days 7` at **18:45 local**, `RunAtLoad=false`, `ExitTimeOut=1800`, `ProcessType=Background`, `LowPriorityIO=true` |

**There is no launchd agent for Redis.**

Two deliberate details:

- The API's `KeepAlive` is a dict `{SuccessfulExit: false}`, not `true` — *restart on crash, but
  not in a tight loop if it cannot bind*. `ThrottleInterval` 10 s backs that up.
- The nightly agent uses `uv run --frozen` with an **absolute** `/opt/homebrew/bin/uv` because
  *launchd execs without a shell: there is no PATH, no venv activation and no profile.*
  `EnvironmentVariables` therefore sets `PATH`, `HOME`, `PREFECT_SERVER_ANALYTICS_ENABLED=false`
  and `DO_NOT_TRACK=1` explicitly.

---

## 3. The nightly schedule

### 3.1 What actually runs

**One job, at 18:45 local time**, executing `ta daily --days 7`. That is the whole schedule.

NSE publishes the cash bhavcopy at roughly 18:30 IST; 15 minutes of slack covers a late publish
without pushing the run into the evening. `--days 7` re-probes the last week — cached files cost
nothing, so the window is cheap insurance against a missed night.

`ta daily` has four stages (`packages/cli/src/ta_cli/__main__.py::cmd_daily`):

| Stage | Does | Affects exit code |
| --- | --- | --- |
| `[1/4] ingest` | `ingest_sessions(start, end)` → `md.bars_1d` | **yes** |
| `[2/4] symbol master` | `refresh_symbol_master()` → `ref.symbols` | no |
| `[3/4] index membership snapshot` | `refresh_constituents()` → `md.index_members` | no |
| `[4/4] screen + size + record` | `execute_nightly()` → `md.sector_daily`, `md.breadth_daily`, `md.trend_1d`, `ev.recommendations`, `ev.rejections` | **yes** |

Each stage is independent and a failure is **reported rather than raised**: a missing constituents
refresh must not cost you the day's bars, and a screen that fails must not hide the fact that
ingestion succeeded. The exit code is non-zero if the *screen* produced no run, because that is the
stage whose silence would otherwise look like *"no trades today"*.

Typical incremental runtime: **~10 s**.

> **PLAN.md §Daily schedule describes eleven stages** (`eod_mark`, `ingest_archive`,
> `validate_eod`, `build_features`, `evaluate_and_learn`, `nlp_enrich`, `screen_and_dossier`,
> `committee`, `red_team`, `publish`, `maintain`) across 15:45–22:35 IST, orchestrated by Prefect.
> **None of that exists.** Prefect is not installed; there is no freshness gate, no feature
> persistence, no evaluation stage, no LLM stage and no maintenance stage. The four stages above
> are the system.

### 3.2 Sleep/wake — `pmset` is required and is NOT currently set

**launchd alone does not wake a sleeping Mac.** A `StartCalendarInterval` job whose window passes
while the machine is asleep runs at the **next wake**, which may be the next morning. That is safe
here — the run is idempotent and bhavcopies are immutable — but it is not "ran at 18:45".

To make the machine wake for it, schedule a power event. This needs `sudo`, so
`install_launchd.sh` prints it rather than running it:

```bash
sudo pmset repeat wakeorpoweron MTWRF 18:40:00
```

Verify with `pmset -g sched`; undo with `sudo pmset repeat cancel`. On a laptop this fires only on
AC power unless wake-on-battery is also allowed. The machine wakes, runs the job, and returns to
sleep on its own idle timer.

> **Verified not set.** `pmset -g sched` currently shows only two macOS-internal alarms
> (`com.apple.calaccessd.alarmEngine`, `com.apple.osanalytics.hardhighengagementtimer`) and **no
> `repeat` entry**. If the Mac sleeps before 18:45, tonight's run is deferred to the next wake.

### 3.3 Running the pipeline by hand

```bash
uv run --frozen ta daily                      # everything, ingesting the last 7 days
uv run --frozen ta daily --end 2026-10-30 --days 30
uv run --frozen ta ingest --start 2024-11-01 --end 2026-10-30   # backfill only
uv run --frozen ta constituents --indices NIFTY50 NIFTY200
uv run --frozen ta symbols
uv run --frozen ta status
uv run --frozen ta cost --delivery --notional 100000
uv run --frozen -v ta daily                   # -v raises logging to INFO
```

Running the screen alone, without re-ingesting:

```bash
uv run --frozen python -c "
from ta_quant.nightly import execute_nightly
r = execute_nightly()
print(r.run_id, r.d, r.regime.value, r.recommendations.height, r.rejections.height)"
```

`execute_nightly(d=None, equity=500_000.0, replay=False)` — `replay=True` pins the decision instant
to that session's 18:30 IST. **Replay currently produces nothing**, because only one
`md.index_members` snapshot exists (dated 2026-10-31) and it is correctly invisible to any earlier
as-of. That is right PIT behaviour, not a bug; see
[LLD.md §3.5](LLD.md#live-as-of-vs-replay-as-of--the-bug-the-separation-caught).

**Four CLI subcommands are broken** and raise `ImportError` on invocation: `ta features`,
`ta sectors`, `ta screen`, `ta calendar`. They import functions that do not exist
(`build_features`, `build_sector_daily`, `run_screen`, `observed_calendar`). See
[LLD.md §6](LLD.md#6-ta_cli--the-operator-cli).

### 3.4 Running the API by hand

```bash
uv run --frozen uvicorn ta_api.main:app --host 127.0.0.1 --port 8000
uv run --frozen uvicorn ta_api.main:app --reload            # dev
cd apps/ui && npm run dev                                   # Vite dev server on :5173
cd apps/ui && npm run build                                 # tsc --noEmit && vite build -> dist/
```

FastAPI serves `apps/ui/dist` at `/` if it exists, so after a build the single origin
`http://127.0.0.1:8000` serves both the SPA and the API. Without a build it logs
`UI bundle not found at …; run 'npm run build' in apps/ui` and the API still works.

CORS allows `:5173` for the Vite dev flow.

---

## 4. Schema

### 4.1 Applying it

```bash
./infra/apply_schema.sh                                      # defaults to schema.sql
./infra/apply_schema.sh infra/clickhouse/schema_v2.sql
```

The script strips `--` comments, splits on `;`, base64-encodes each statement to survive the shell,
and POSTs them one at a time so **a failure names the statement**. Output is `ok` / `FAIL` per
statement with the first 60 characters of each.

Both files are `CREATE DATABASE/TABLE IF NOT EXISTS` throughout, so re-running is safe and changes
nothing on an existing install.

### 4.2 What it will not do

`IF NOT EXISTS` means **the script cannot migrate an existing table**. Adding a column, changing a
codec or changing an engine requires explicit DDL:

```bash
curl -s --data-binary "ALTER TABLE ev.recommendations ADD COLUMN IF NOT EXISTS would_qty UInt32 DEFAULT 0" http://127.0.0.1:8123/
```

That is visibly how `would_qty` / `would_notional` were added — they sit **last** in the live table
while `schema_v2.sql` declares them mid-table. Harmless (inserts are by name via Arrow) but it
means a fresh install produces a different physical column order than the running one.

Engine changes (`ReplacingMergeTree` → `MergeTree`, as happened to `ev.recommendations` and
`ev.rejections`) require **drop and recreate** and therefore lose data. That is why
`ev.recommendations` holds one `run_id` while `ev.rejections` holds five.

### 4.3 Verifying

```bash
curl -s --data-binary "SHOW TABLES FROM md" http://127.0.0.1:8123/
curl -s --data-binary "DESCRIBE TABLE md.bars_1d FORMAT TSV" http://127.0.0.1:8123/
curl -s --data-binary "SELECT database||'.'||name, engine, sorting_key, total_rows
  FROM system.tables WHERE database IN ('md','nx','fn','ev','ref')
  ORDER BY database, name FORMAT PrettyCompactMonoBlock" http://127.0.0.1:8123/
```

**Adding a table with a `knowledge_time` column?** Register it in
`ta_core.asof.PIT_TABLES` or `tests/pit/test_guard_hardening.py::test_every_pit_table_in_the_schema_is_registered_in_the_guard`
fails the build. That test parses `infra/clickhouse/schema*.sql` directly.

---

## 5. Logs

All under `data/logs/` (**7.5 MB** total today, growing — nothing rotates them):

| File | Written by | Contents |
| --- | --- | --- |
| `clickhouse-server.log` / `.err.log` | ClickHouse (`config.xml` `<logger>`) | level `warning`, rotated at 100 MB × 3 |
| `clickhouse.out.log` / `clickhouse.err.log` | `services.sh` / launchd stdio | startup output, crash tails |
| `mongod.log` | mongod (`mongod.conf` `systemLog`) | JSON lines, `logAppend: true`, **not rotated** (3.9 MB today) |
| `mongod.out.log` / `mongod.err.log` | launchd stdio | |
| `api.log` / `api.err.log` | launchd stdio for uvicorn | uvicorn runs at `--log-level warning`, so `api.log` is empty and tracebacks land in `api.err.log` |
| `nightly.log` / `nightly.err.log` | launchd stdio for `ta daily` | **does not exist yet** — the 18:45 job has not fired |
| `backfill.log` | ad-hoc | the initial 2-year ingest |

```bash
tail -f data/logs/nightly.log
tail -50 data/logs/api.err.log
grep -i error data/logs/clickhouse-server.err.log | tail -20
```

ClickHouse also keeps queryable logs internally:

```bash
curl -s --data-binary "SELECT event_time, query_duration_ms, read_rows, memory_usage, query
  FROM system.query_log WHERE type='QueryFinish'
  ORDER BY event_time DESC LIMIT 10 FORMAT Vertical" http://127.0.0.1:8123/
```

There is **no log rotation for `mongod.log`** and no retention policy for `data/logs/`. Both grow
without bound.

---

## 6. Troubleshooting — the real failure modes

Every entry below is a failure this project actually hit. Each one **looked fine, started cleanly,
and was wrong** — which is why every phase needs a gate.

### 6.1 ClickHouse dies with exit 137 and no log output

**Cause:** macOS **Gatekeeper** killing a quarantined binary. The Homebrew *cask* ClickHouse
carries `com.apple.quarantine` and is terminated before it can write anything. The symptom is an
exit code and silence — not a crash log, not a permissions error.

**Fix in place:** the binary is the official GitHub release fetched with `curl` into
`infra/bin/clickhouse`. Files fetched with `curl` get no quarantine attribute.

**Do not reinstall the cask. Do not strip the quarantine attribute** — that was correctly refused
as a security action.

### 6.2 ClickHouse has no memory cap even though the config sets one

**Cause:** `max_server_memory_usage_to_ram_ratio` set to `0`. ClickHouse computes
`48 GB × 0 = 0` and **treats 0 as unlimited**, silently cancelling the absolute cap. The server
starts happily, the config file looks correct, and there is no cap at all.

**Rule:** leave the ratio at its `0.9` default; set only the absolute
(`<max_server_memory_usage>5368709120</max_server_memory_usage>`). Effective limit is
`min(5 GiB, 0.9 × 48 GB) = 5 GiB`.

**Always verify rather than trusting the file:**

```bash
curl -s --data-binary "SELECT name, value, default FROM system.server_settings
  WHERE name LIKE 'max_server_memory%' FORMAT PrettyCompactMonoBlock" http://127.0.0.1:8123/
```

Expect `max_server_memory_usage = 5368709120` and `..._to_ram_ratio = 0.9`.
`infra/smoke_test.sh` asserts the literal `5368709120`.

### 6.3 ClickHouse hangs on "startup" and logs nothing useful

**Cause:** a malformed XML config — most often **`--` inside an XML comment**, which is illegal in
XML. ClickHouse's failure mode is to log nothing useful and **hang in its shutdown path**, which
reads exactly like a slow startup.

**Prevention:** `infra/services.sh up` runs `infra/validate_config.sh` before every start and
aborts on failure. It parses every `infra/clickhouse/*.xml` with `xml.etree.ElementTree`.

**Diagnosis when it happens anyway** — sample the process to see which path it is in:

```bash
sample $(pgrep -f "infra/bin/clickhouse server" | head -1) 3 -mayDie
```

A main thread in `ContextSharedPart::shutdown()` proves it is *shutting down*, not starting. **This
is the diagnostic that cracked it.**

For the real error, run with a debug/console config:

```bash
sed 's|<level>warning</level>|<level>debug</level>\n<console>1</console>|' \
    infra/clickhouse/config.xml > /tmp/ch_debug.xml
infra/bin/clickhouse server --config-file=/tmp/ch_debug.xml 2>&1 | grep -v "@ 0x"
```

### 6.4 `Code 36` on a mutation

**Cause:** `number_of_free_entries_in_pool_to_execute_mutation` (default **20**) must be **less
than** `background_pool_size × background_merges_mutations_concurrency_ratio`. With `pool 8 ×
ratio 2 = 16`, the default 20 exceeds it.

**Fix in place:** `config.xml` lowers three `merge_tree` thresholds to **4**:
`number_of_free_entries_in_pool_to_execute_mutation`,
`..._to_lower_max_size_of_merge`, `..._to_execute_optimize_entire_partition`.

If you raise `background_pool_size`, you may raise these; if you lower it, you **must** lower them.

### 6.5 Restart fails with "Address already in use" and the new process exits silently

**Cause:** ClickHouse holds TCP **9000** briefly after SIGTERM. A restart issued too quickly binds
nothing and dies without a message.

**Fix in place:** `services.sh down` polls for up to 30 s until the port is free:

```bash
for i in $(seq 1 30); do
  lsof -nP -iTCP:9000 -sTCP:LISTEN >/dev/null 2>&1 || break
  sleep 1
done
```

Check by hand with `lsof -nP -iTCP:9000 -sTCP:LISTEN`. Note this is the **native** port; the HTTP
port 8123 that everything in this repo actually uses frees sooner, so `/ping` succeeding is not
proof the restart will work.

### 6.6 MongoDB eats 23 GB

**Cause:** WiredTiger's default cache is `max(0.5 × (RAM − 1 GB), 256 MB)` = **23.5 GB** here.

**Fix in place:** `cacheSizeGB: 2` in `infra/mongo/mongod.conf`. Verify:

```bash
mongosh --quiet --eval 'print(db.serverStatus().wiredTiger.cache["maximum bytes configured"])'
# expect 2147483648
```

### 6.7 `mongosh` reports "not primary" or replica-set errors

The deployment is a **single-node replica set `rs0`** — required for change streams, transactions
and `mongodump --oplog`. If `rs.status()` shows no config:

```bash
mongosh --quiet --eval 'rs.initiate({_id:"rs0", members:[{_id:0, host:"127.0.0.1:27017"}]})'
mongosh --quiet --eval 'print(db.serverStatus().repl.setName)'   # expect rs0
```

### 6.8 The dashboard is blank / `/api/*` returns 503

```bash
curl -s localhost:8000/api/health | python3 -m json.tool
```

- `services.clickhouse: false` → `./infra/services.sh up`, then §6.1–6.5.
- `status: "ok"` but a blank page → the SPA bundle is missing. `cd apps/ui && npm run build`.
  Check `data/logs/api.err.log` for `UI bundle not found at …`.
- API unreachable entirely → `launchctl print gui/$(id -u)/com.tradingagent.api`, and
  `tail -50 data/logs/api.err.log`.

### 6.9 The dashboard shows duplicate rows

**Cause:** `ReplacingMergeTree` collapses duplicates only on a background merge whose timing is not
guaranteed. Handlers that do not dedupe on read show every unmerged version.

Observed this session: with `md.sector_daily` on **3 unmerged parts**, `/api/heatmap` returned 40
rows covering only 14 distinct industries and silently dropped six — see
[API.md](API.md#apiheatmap). A background merge has since hidden it; the code is unchanged, so it
recurs on the next multi-run night.

`/api/rejections` has the same symptom from a different cause and **no merge will ever hide it**:
`ev.rejections` is `MergeTree` and the handler does not pin `run_id`, so it returns 124 rows from
six runs representing only 18 distinct facts.

**Operational mitigation** (not a fix — the handlers should dedupe):

```bash
curl -s --data-binary "OPTIMIZE TABLE md.sector_daily FINAL" http://127.0.0.1:8123/
curl -s --data-binary "OPTIMIZE TABLE md.breadth_daily FINAL" http://127.0.0.1:8123/
curl -s --data-binary "OPTIMIZE TABLE md.trend_1d FINAL" http://127.0.0.1:8123/
curl -s --data-binary "OPTIMIZE TABLE ref.symbols FINAL" http://127.0.0.1:8123/
```

**Do not `OPTIMIZE … FINAL` on `ev.recommendations` or `ev.rejections`** — they are `MergeTree`, so
it is merely expensive rather than destructive, but the duplication there is *by design* (multiple
runs) and the correct fix is to pin `run_id` in the query.

To see unmerged parts:

```bash
curl -s --data-binary "SELECT database, table, count() parts, sum(rows)
  FROM system.parts WHERE active GROUP BY database, table
  HAVING parts > 1 ORDER BY parts DESC FORMAT PrettyCompactMonoBlock" http://127.0.0.1:8123/
```

### 6.10 Ingest returns zero sessions

`ingest_sessions` counts 404s as `holidays`, not `failed`. All weekdays showing as holidays means
the URL shape or the host changed, not that the market was shut.

```bash
curl -sI "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20261030_F_0000.csv.zip"
```

Expect `200` and ~200 KB. A `403` means the archive host has started rejecting non-browser clients
— **do not respond by adding cookie-warming.** NSE's Terms of Use prohibit systematic automated
collection and the project's posture is published archive files only.

Every body ever fetched is in `data/raw/` (497 files, 86 MB), so a parser fix is a re-parse, not a
re-download.

### 6.11 The nightly job did not run

```bash
launchctl print gui/$(id -u)/com.tradingagent.nightly
ls -la data/logs/nightly*.log
pmset -g sched
```

If the Mac was asleep at 18:45, launchd runs the job at the **next wake** — see §3.2. There is
currently no `pmset repeat` entry, so this is the expected behaviour today.

Force a run:

```bash
launchctl kickstart -k gui/$(id -u)/com.tradingagent.nightly
```

### 6.12 A PIT guard failure (`PITViolation`)

```
ta_core.asof.PITViolation: unbounded read of point-in-time table(s): md.bars_1d
ta_core.asof.PITViolation: knowledge_time bound must be a literal as-of, not now()
ta_core.asof.PITViolation: knowledge_time bound '…' != as_of '…'
```

**This is the guard working.** Do not route around it. The three fixes, in order of preference:

1. Use `GuardedClient.query_pit(table, as_of, …)` — it builds the bound for you.
2. If the query shape genuinely cannot go through `build_pit_query` (e.g. it needs a `GROUP BY`,
   which cannot live inside the parenthesised `where`), hand-write it with
   `knowledge_time <= {as_of.sql()}` and **pass `as_of` to `query_df`** so equality is checked.
   `_latest_run` in `apps/api/main.py` is the worked example.
3. If it is genuinely an operational question about the store rather than about a date, use
   `query_admin` — but note a CI test greps for it under `packages/quant`, `packages/llm`,
   `packages/ledger` and `apps/api` and will fail the build.

A join touching two PIT tables needs **both** sides bounded, each with a qualified bound
(`b.knowledge_time <= …`, `n.knowledge_time <= …`). A bare bound is only accepted when exactly one
PIT table is in scope.

---

## 7. Backup and restore

### 7.1 What is irreplaceable

| Path | Size | Replaceable? |
| --- | ---: | --- |
| **`data/raw/`** | **86 MB (497 files)** | **No.** NSE archives are published files; nothing guarantees indefinite availability, and the project's own posture forbids re-scraping at volume. **This is the one thing that cannot be regenerated.** |
| `uv.lock`, `infra/`, source | small | in git (2 commits: `857942c`, `49f5354`) |
| `data/clickhouse/` | 37 MB | yes — rebuildable from `data/raw/` plus `ta ingest` and `ta daily`, **except** `ev.recommendations` / `ev.rejections`, which are the historical record of what the system said and when |
| `data/mongo/` | 4.4 MB | currently holds **zero application data** |
| `data/logs/` | 7.5 MB | no need |

Governing principle 3: *raw bytes are immutable; written before anything parses them. Everything
else is reconstructible.* Back up `data/raw/` and the repo; everything else is a convenience.

### 7.2 Cold backup (simplest, correct)

```bash
./infra/services.sh down
tar czf ~/backups/ta-$(date +%Y%m%d).tar.gz \
    data/raw data/clickhouse data/mongo \
    infra pyproject.toml uv.lock .python-version
./infra/services.sh up
```

If the launchd agents are installed they will restart ClickHouse and mongod immediately
(`KeepAlive=true`), so for a genuinely cold backup bootout first:

```bash
launchctl bootout gui/$(id -u)/com.tradingagent.clickhouse
launchctl bootout gui/$(id -u)/com.tradingagent.mongod
# … tar …
./infra/install_launchd.sh install
```

### 7.3 Hot backup, per store

**ClickHouse** — native, consistent, no downtime:

```bash
curl -s --data-binary "BACKUP DATABASE md TO Disk('backups','md_$(date +%Y%m%d)')" http://127.0.0.1:8123/
```

This requires a `<backups>` disk in `config.xml`, which is **not configured today**. Until it is,
the portable alternative is a per-table dump:

```bash
for t in md.bars_1d md.index_members md.sector_daily md.breadth_daily md.trend_1d \
         ref.symbols ev.recommendations ev.rejections; do
  curl -s --data-binary "SELECT * FROM $t FORMAT Native" http://127.0.0.1:8123/ > ~/backups/$t.native
done
```

Restore with `INSERT INTO <table> FORMAT Native` posting the file back.

**MongoDB** — the replica set exists specifically so this works:

```bash
mongodump --host 127.0.0.1:27017 --oplog --out ~/backups/mongo-$(date +%Y%m%d)
mongorestore --host 127.0.0.1:27017 --oplogReplay ~/backups/mongo-YYYYMMDD
```

`--oplog` is point-in-time consistent and **requires a replica set** — that is the whole reason
`replSetName: rs0` is in the config.

**Redis** holds no project state; nothing to back up.

### 7.4 Rebuild from scratch

```bash
# 1. deps
uv sync --frozen

# 2. services
./infra/services.sh up
./infra/smoke_test.sh                    # must print ALL PASS

# 3. schema
./infra/apply_schema.sh infra/clickhouse/schema.sql
./infra/apply_schema.sh infra/clickhouse/schema_v2.sql

# 4. data — re-parses data/raw/ if present, re-downloads only what is missing
uv run --frozen ta ingest --start 2024-11-01 --end 2026-10-30
uv run --frozen ta constituents
uv run --frozen ta symbols
uv run --frozen ta daily

# 5. UI
cd apps/ui && npm ci && npm run build

# 6. verify
uv run --frozen pytest                   # expect 107 passed
uv run --frozen ta status
curl -s localhost:8000/api/health
```

Step 4 with `data/raw/` intact is a **re-parse, not a re-download** — `PoliteClient` reuses any
cached body. Without it, expect ~500 HTTPS requests at 1 req/s to `nsearchives.nseindia.com`,
roughly 10 minutes.

**What a rebuild cannot restore:** the contents of `ev.recommendations` and `ev.rejections` from
previous runs. Re-running `ta daily` produces a *new* `run_id` with today's code, which is a
different fact from what the system said last week. That history is the audit trail — see
[DATA_MODEL.md §4](DATA_MODEL.md#4-evrecommendations-and-evrejections--the-two-deliberate-engine-changes).

---

## 8. Health checks

### 8.1 `infra/smoke_test.sh` — 10 gates

```
== ClickHouse ==   ping · memory cap 5GiB · write+read · value roundtrip
== MongoDB ==      ping · replset rs0 · wt cap 2GB · write+read
== Redis ==        ping
== Machine ==      swap is 0M
```

Prints `ALL PASS` and exits 0, or `FAILURES PRESENT` and exits non-zero. It creates and drops a
`smoke` database in both stores.

> `CLAUDE.md` says "11 infra gates". There are **10** `chk` calls. HANDOFF.md says 10 and is
> correct.

The **swap gate** is the one people skip and it is the most important: `sysctl vm.swapusage` must
read `0.00M`. A machine already swapping before the LLM loads will thrash, and llama.cpp's failure
mode is to page-fault from SSD rather than fail cleanly — only a throughput floor alarm catches it.

### 8.2 Day-to-day

```bash
uv run --frozen ta status
curl -s localhost:8000/api/health
./infra/services.sh status
./infra/install_launchd.sh status

# row counts and on-disk sizes
curl -s --data-binary "SELECT database, table, sum(rows) rows,
  formatReadableSize(sum(bytes_on_disk)) disk, count() parts
  FROM system.parts WHERE active AND database IN ('md','ev','ref','nx','fn')
  GROUP BY database, table ORDER BY database, table FORMAT PrettyCompactMonoBlock" \
  http://127.0.0.1:8123/

# did tonight's run land?
curl -s --data-binary "SELECT run_id, d, count(), max(as_of) FROM ev.recommendations
  GROUP BY run_id, d ORDER BY max(as_of) DESC LIMIT 5 FORMAT PrettyCompactMonoBlock" \
  http://127.0.0.1:8123/
```

Expected steady state (verified 2026-10-31):

```
md.bars_1d         1,220,615 rows · 3,197 symbols · 492 sessions · 2024-11-01 .. 2026-10-30
md.index_members   851 rows (NIFTY 50/100/200/500, snapshot 2026-10-31)
ref.symbols        2,620 distinct symbols
md.trend_1d        2,919 · md.sector_daily 20 · md.breadth_daily 1   (one session, merged)
ev.recommendations 25 (latest run 65bd044013ec) · ev.rejections 124 (6 runs, append-only)
on disk            35.40 MiB for md.bars_1d; 37 MB for the whole ClickHouse directory
```

Counts on the `ReplacingMergeTree` tables (`md.trend_1d`, `md.sector_daily`, `md.breadth_daily`,
`ref.symbols`) are **merge-state dependent** and read n× higher between a nightly run and the
background merge that collapses it. `ev.recommendations` and `ev.rejections` are `MergeTree` and
grow permanently, one run at a time — that is the audit trail, not a leak.

**Zero BUYs and 25 WATCH cards is the correct, expected output** — not a failure. See
[LLD.md §3.4](LLD.md#risk-p1-as-a-downgrade-not-a-rejection) and HANDOFF.md.

### 8.3 Memory

```bash
sysctl vm.swapusage                      # must be 0.00M before any model load
ps -o rss=,comm= -p $(pgrep -f "infra/bin/clickhouse server") | awk '{print $1/1024 " MB"}'
ps -o rss=,comm= -p $(pgrep -f mongod) | awk '{print $1/1024 " MB"}'
```

Expected at rest (from HANDOFF): ClickHouse **0.42 GB** + watchdog 0.10, mongod **0.14 GB**,
redis ≈ 0.

---

## 9. Security posture

- Everything binds **`127.0.0.1` only**: ClickHouse 8123/9000 (`<listen_host>127.0.0.1</listen_host>`),
  mongod (`bindIp: 127.0.0.1`), redis, uvicorn.
- **No authentication anywhere.** ClickHouse's `default` user has an empty password restricted to
  `127.0.0.1`/`::1` with `access_management: 0`. The API has no auth and no rate limiting.
  **Do not expose any of these ports.**
- SQL in the API is **string-interpolated**, not parameter-bound. The interpolated values are
  server-derived dates, a `run_id` read back from the database, and one path parameter sanitised to
  `[A-Za-z0-9\-&]{0,24}`. A future handler accepting a free-form string must not copy the pattern.
- `infra/bin/clickhouse` is a curl-fetched release binary with **no checksum recorded in the repo**.
- **NSE Terms of Use** prohibit systematic automated collection, redistribution, and use for
  "virtual trading or simulation". The posture is: download published archive files only, never
  defeat bot protection, never redistribute. `www.nseindia.com` returns 403 to non-browser clients
  and the design routes around that rather than through it. Read the terms yourself before building
  on this.
