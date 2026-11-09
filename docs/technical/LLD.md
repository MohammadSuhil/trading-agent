# Low-level design

Module-by-module reference for an engineer who has just been handed this repo. Companion
documents: [ARCHITECTURE.md](ARCHITECTURE.md) (high-level shape), [DATA_MODEL.md](DATA_MODEL.md)
(every ClickHouse table), [API.md](API.md) (HTTP surface), [TECH_STACK.md](TECH_STACK.md)
(dependencies and version constraints), [OPERATIONS.md](OPERATIONS.md) (running it),
[TESTING.md](TESTING.md) (what the suites guard), [diagrams/](diagrams/).

Everything below was verified on 2026-10-31 against the running system or read directly from
source. Unverified statements are prefixed `unverified:`.

---

## 0. Orientation

### 0.1 Indian-market vocabulary you need before reading the code

An engineer outside India will not have met these. They appear in identifiers, not just comments.

| Term | What it is |
| --- | --- |
| **NSE / BSE** | National Stock Exchange and Bombay Stock Exchange — the two Indian equity venues. NSE is the primary one here. |
| **bhavcopy** | NSE's end-of-day settlement file: one CSV per trading session covering the entire cash market. "Bhav" = rate/price. Published ~18:30 IST as a zip at a stable archive URL. This project's entire price history comes from it. |
| **UDiFF** | *Unified Data Interface File Format* — the current bhavcopy schema (`TckrSymb`, `OpnPric`, `TtlTrfVal`…). Replaced the older positional format. Parsed by `ta_datafeed/parse/udiff.py`. |
| **series code** | A per-listing trading category in the bhavcopy's `SctySrs` column. `EQ` = normal rolling-settlement equity. `BE` = *trade-to-trade*: no intraday netting, every trade must be delivered, which is a liquidity/manipulation flag. Others in the file (`SG`, `GB`, `N1`…) are sovereign gold bonds, ETFs and debt. Only `EQ`+`BE` are ingested; only `EQ` is tradeable (`RISK-12`). |
| **ASM / GSM** | *Additional Surveillance Measure* / *Graded Surveillance Measure* — SEBI/exchange regimes that impose extra margin, price bands or periodic-call auctions on names showing abnormal price/volume behaviour. Being on either is a hard disqualifier (`RISK-12`). `md.surveillance` exists for this and is currently empty. |
| **STT** | *Securities Transaction Tax*. 0.1% on **both** legs for delivery; 0.025% on the **sell leg only** for intraday. The asymmetry is in `ta_core.costs`. |
| **DP charge** | *Depository Participant* charge: a **flat ₹15.34 per scrip per delivery-sell day**, independent of size. This single number is why the minimum position is ₹50,000 and why the book holds 7 names, not 20. |
| **CNC / MIS** | Broker product codes. CNC = delivery (`ProductType.DELIVERY`), MIS = intraday (`ProductType.INTRADAY`). |
| **lakh / crore** | Indian number grouping. 1 lakh = 10⁵, 1 crore = 10⁷. "₹5,00,000" is ₹500,000 written with Indian digit grouping. `adtv_rupees >= 5e7` in `RiskConfig` is "₹5 crore". `turnover_cr` in `/api/heatmap` is turnover divided by 1e7. |
| **Muhurat trading** | A ceremonial one-hour session on Diwali, which can land on a **Saturday or Sunday**. This is why `TradingCalendar` has `special_sessions` and why a Mon–Fri weekmask calendar library cannot be used. |
| **NIFTY 50 / 200 / 500** | NSE index families. The project scores and trades Nifty 500 by default; a narrower tradeable index can be configured. |
| **TRI** | *Total Return Index* — an index series including reinvested dividends. Benchmarking against the price index instead flatters returns by ~1.2%/yr. `md.benchmark_1d.series_kind` carries `TRI | PRICE`. |
| **circuit limit** | A daily price band (±5/10/20%) beyond which a stock cannot trade. A circuit-locked day is a bar with no liquidity, which is why barrier probabilities are resampled from real paths rather than modelled analytically. |

### 0.2 Repository layout as built

```
packages/core/src/ta_core/          zero I/O domain core
packages/datafeed/src/ta_datafeed/  HTTP, parsing, ClickHouse access
packages/quant/src/ta_quant/        factors, trend, barriers, the nightly pipeline
packages/ledger/src/ta_ledger/      risk gates only (broker + events not written)
packages/cli/src/ta_cli/            the `ta` operator CLI
apps/api/src/ta_api/                FastAPI read model
apps/ui/                            Vite + React SPA (out of scope for this doc)
infra/                              ClickHouse/Mongo config, launchd plists, shell scripts
tests/                              86 explicit tests + 21 doctests = 107 total
```

Source size, measured: **5,220 lines of Python** across `packages/` + `apps/api/` + `tests/`.
Largest modules: `ta_quant/pipeline.py` (497), `ta_api/main.py` (496), `ta_ledger/risk.py` (494),
`ta_core/asof.py` (307), `ta_quant/barriers.py` (277).

### 0.3 Packages that exist as empty shells

`packages/llm` and `packages/news` are described in `docs/PLAN.md` §Repo layout but **do not exist
on disk**. Several directories exist with empty `__init__.py` and nothing else:

| Path | Lines | Status |
| --- | --- | --- |
| `packages/quant/src/ta_quant/screener/__init__.py` | 0 | empty; `ta screen` imports `run_screen` from it and crashes |
| `packages/quant/src/ta_quant/backtest/__init__.py` | 0 | empty; M3's anti-drift invariant has nothing to run against |
| `packages/quant/src/ta_quant/ml/__init__.py` | 0 | empty |
| `packages/datafeed/src/ta_datafeed/adjust/__init__.py` | 0 | empty; **no corporate-action adjustment exists** |
| `packages/datafeed/src/ta_datafeed/universe/__init__.py` | 0 | empty |
| `packages/ledger/src/ta_ledger/broker/__init__.py` | 0 | empty; no PaperBroker |

See [§9 Deliberate incompleteness](#9-what-is-deliberately-not-built-and-what-that-costs-you).

---

## 1. `ta_core` — the pure domain core

**Package:** `packages/core/src/ta_core/` · **Distribution:** `ta-core` · **Deps:** `pydantic`,
`pydantic-settings`, `pyyaml` only.

**Invariant, enforced socially and by dependency list:** `ta_core` performs **zero I/O**. It may
not open a socket, read a file or touch a database. Its own `__init__.py` says so. This is what
makes the entire domain layer testable with no services running — `uv run --frozen pytest` passes
on a machine with ClickHouse down (ClickHouse-dependent API tests skip themselves; see
[TESTING.md](TESTING.md)).

Consequence you will hit: `calendar.py` cannot read a holiday YAML and `costs.py` cannot read
`config/costs.yaml`. Both take the data as constructor arguments and the app layer does the
loading. `config/` is currently an empty directory.

### 1.1 `ta_core.asof` — the point-in-time guard

**307 lines. This is the single most important module in the repository.** Read its docstring
before changing anything in it.

#### The failure it exists to prevent

Look-ahead bias does not raise, does not look wrong, and produces a beautiful equity curve. You
find out it was fiction when live results diverge, months later, after you have trusted it with
money. The module docstring enumerates four distinct leakage vectors:

1. News timestamped by when the **crawler** saw it, not when the exchange disseminated it.
2. **Today's** index membership applied to past dates — NSE churns constituents constantly, so the
   survivors look like the whole universe and every backtest inherits survivorship bias.
3. Unadjusted splits: a 1:2 split reads as an instant −50% return, which a momentum factor happily
   trades on.
4. A feature whose source row landed **after** the decision it fed.

#### Public surface

```python
PIT_TABLES: frozenset[str]            # 23 tables; see DATA_MODEL.md §2
class PITViolation(AssertionError)
@dataclass(frozen=True, slots=True)
class AsOf:
    ts: datetime                      # MUST be tz-aware
    def sql(self) -> str              # -> "'2026-10-30 14:00:00'" (UTC)
def is_pit_table(name: str) -> bool
def build_pit_query(table, as_of, *, columns="*", where=None,
                    order_by=None, limit=None) -> str
def assert_pit_bounded(sql: str, as_of: AsOf | None = None) -> None
```

`AsOf.__post_init__` refuses a naive datetime: *"what did we know at 14:00" is meaningless without
saying 14:00 where*, and this system spans IST feeds and UTC storage.

`build_pit_query` **injects** the `knowledge_time` bound; the caller never writes it. The stated
reason is the correct one: *a bound you have to remember is a bound you will forget*. Non-PIT
tables pass through unbounded, which is right — `ref.symbols` has no `knowledge_time` column.

#### Why the guard inspects the bound's right-hand side instead of just its presence

This is the question the task brief singles out, and it has a failure story.

The first version of `assert_pit_bounded` proved only that a `knowledge_time` bound **existed**.
That made this query pass the build-failing leakage test:

```sql
SELECT * FROM md.bars_1d WHERE knowledge_time <= now()
```

`now()` is evaluated at **execution** time and moves with the wall clock. So replaying 2026-10-15
today reads everything the system learned in the sixteen days since — a live look-ahead path
straight through the one guard `CLAUDE.md` calls *"if only one thing stays correct, make it this."*
It is syntactically indistinguishable from a correct query. Nothing in the test suite caught it,
because the test suite only asked the same question the guard asked.

The fix, landed 2026-10-31, is a two-stage check. `_BOUND` is a regex that **captures** each
bound's RHS rather than merely detecting the bound:

```python
_BOUND = re.compile(
    r"(?<![a-z0-9_.])((?:[a-z_][a-z0-9_.]*)\.)?knowledge_time\s*<=?\s*"
    r"('[^']*'"                        # literal timestamp -- what AsOf.sql() emits
    r"|%\([a-z_][a-z0-9_]*\)s"         # DB-API named parameter
    r"|\{[a-z_][a-z0-9_]*:[^}]+\}"     # ClickHouse native parameter binding
    r"|[a-z_][a-z0-9_]*\s*\([^)]*\)"   # a function call -- captured so we can REFUSE it
    r"|[^\s)]+)",                      # anything else -- also refused
    re.IGNORECASE,
)
```

The function-call alternative exists **only so the RHS can be captured and then rejected**. If it
were absent the regex would fall through to `[^\s)]+`, match only `now` and leave `()` dangling —
the error message would be worse and the parenthesised-argument case (`toDate(now())`) would
mis-split. `_PINNED_RHS` then whitelists exactly three shapes: a quoted literal, a DB-API
placeholder `%(name)s`, and a ClickHouse native binding `{name:Type}`.

A driver-bound parameter is **trusted**, not refused, and the reason is in
`tests/pit/test_guard_hardening.py::test_a_driver_bound_parameter_is_accepted`: refusing it would
push callers toward string interpolation, which is strictly worse.

`_assert_bounds_hold` is split out from `assert_pit_bounded` deliberately, because it answers a
different question — *does the bound hold*, not *is there a bound* — and the second question is
the one a reviewer skips.

#### The third hole: a literal bound to the wrong instant

A constant copied from another query is a literal, so it passes every check above, and it reads
the future. Only the caller knows which instant was meant, so `assert_pit_bounded` takes an
optional `as_of` and, when given, requires **equality**:

```python
want = as_of.sql()
for _, rhs in bounds:
    if rhs.startswith("'") and rhs != want:
        raise PITViolation(f"knowledge_time bound {rhs} != as_of {want}")
```

`GuardedClient.query_pit` always passes `as_of` through, because that path builds the query itself
and there is no excuse for the bound to be anything else.

#### Alias resolution, and why the guard does not cry wolf

`FROM md.bars_1d b ... WHERE b.knowledge_time <= X` looks unbounded to a naive matcher. The guard
builds, per PIT table, the set of names it can be referred to by — full name, bare table name, and
any alias bound in a `FROM`/`JOIN` clause — and filters aliases through `_SQL_KEYWORDS` so
`JOIN md.bars_1d ON ...` does not register `on` as an alias.

A **bare** `knowledge_time <= X` (no qualifier) is accepted only when exactly one PIT table is in
scope. With two it is ambiguous and the guard refuses. This is why `/api/screener` performs two
separate PIT-bounded reads and joins them in polars rather than issuing one SQL join — the
handler's own comment says qualifying the bound through subquery aliases would be *fighting the
guard to reach the same answer less safely*.

#### What this guard is NOT

It is a **build-time, syntactic check over query text**. It is not a SQL parse, and it is not
proof that the bound held at execution. A `#` comment containing `md.bars_1d` would trip it; a
dynamically-assembled query string would evade it. The runtime proof is the `traceparent` span
described in PLAN.md §Observability, scheduled at M4 and not built.

### 1.2 `ta_core.enums` — closed sets

261 lines of `StrEnum`. These are closed **on purpose**: the learning loop attributes P&L per
reason code, and you cannot build a posterior over a category that never repeats. Adding a member
invalidates prior statistics for that code, so it is a deliberate, versioned act.

| Enum | Members | Notes |
| --- | --- | --- |
| `Horizon` | `INTRADAY`, `SWING`, `POSITIONAL` | `HORIZON_SESSIONS = {INTRADAY:1, SWING:10, POSITIONAL:60}` |
| `Side` | `LONG`, `SHORT` | |
| `Action` | `BUY`, `WATCH`, `HOLD`, `TRIM`, `SELL`, `AVOID` | see below |
| `ProductType` | `DELIVERY`, `INTRADAY` | drives `compute_costs` |
| `Regime` | `TREND_UP`, `CHOP`, `TREND_DOWN`, `HIGH_VOL`, `UNKNOWN` | risk gate only, never a signal flipper |
| `Sleeve` | 6 members | Thompson sampler's allocation units (sampler not built) |
| `Arm` | `FROZEN_CONTROL`, `DETERMINISTIC`, `COMMITTEE`, `RANDOM` | only `DETERMINISTIC` is emitted today |
| `BarrierHit` | `UP`, `DOWN`, `VERTICAL`, `CANCELLED`, `OPEN` | `CANCELLED` (vetoed pre-open) is itself a scoreable signal |
| `ReasonCode` | 29 members | the unit of learning |
| `FailureMode` | 14 members | weekly critic's labels; critic not built |
| `DataQuality` | `FULL`, `PARTIAL`, `DEGRADED` | |
| `TrendState` | 8 members | see §3.2 |
| `RuleState` | `OK`, `WARN`, `BREACH`, `DISPLAY_ONLY` | |
| `RuleAction` | `ALLOW`, `WARN`, `RESIZE`, `REJECT`, `HALT_NEW` | separated from the rule so it is auditable |
| `BarrierState` | `OK`, `UNAVAILABLE` | |

#### `Action`, and the three portfolio-only members

```python
PORTFOLIO_ONLY_ACTIONS: frozenset[Action] = frozenset({
    Action.HOLD, Action.TRIM, Action.SELL,
})
```

`HOLD` means *you own this and should keep owning it*. Emitting it for a screened candidate you
have never bought is a **real misstatement**, not a labelling quibble: it reads as advice about an
existing position and hides the fact that the system is not telling you to do anything. `WATCH` is
the honest label for "ranked but not actionable", and a WATCH card carries a full trade plan with
`qty = 0`.

This is enforced, not merely documented: `tests/test_action_semantics.py` fetches
`/api/portfolio`, collects owned symbols, and asserts no row in `/api/recommendations` carries a
portfolio-only action for a symbol outside that set. Git history shows this was a bug that was
fixed (`49f5354 Fix HOLD-for-unowned-name, …`).

`HORIZON_SESSIONS` carries a comment that is load-bearing for the labeller: *the vertical-barrier
length MUST equal the live engine's time stop or labels and execution disagree and every model is
trained on a counterfactual.* The same principle applied to price barriers is §3.3's failure story.

`TREND_ENUM_VERSION = 1` is bumped whenever a `TrendState` threshold moves. Rows carrying an older
version are excluded from posterior updates, because *a state that meant something different then
is not the same observation*. The column exists in `md.trend_1d`; nothing consumes it yet.

### 1.3 `ta_core.costs` — the Indian transaction-cost model

175 lines, doctested. **Used by both the backtester and the paper broker** — that shared use is the
single strongest guard against backtest/live divergence. If costs are modelled in two places they
drift and the backtest quietly becomes optimistic. (Neither consumer exists yet; the pipeline is
the only caller today. The invariant is still the one to protect at M3.)

```python
@dataclass(frozen=True, slots=True)
class CostConfig:                      # all rates as decimal fractions
    delivery_brokerage_rate   = 0.0
    delivery_stt_buy          = 0.001       # 0.1% BOTH legs
    delivery_stt_sell         = 0.001
    delivery_stamp_duty_buy   = 0.00015     # buy leg only
    dp_charge_per_sell        = 15.34       # FLAT rupees, per scrip per sell day
    intraday_brokerage_rate   = 0.0003
    intraday_brokerage_cap    = 20.0        # per EXECUTED ORDER
    intraday_stt_sell         = 0.00025     # SELL leg only
    intraday_stamp_duty_buy   = 0.00003
    exchange_txn_rate         = 0.0000297   # NSE cash, revised 2024-10-01
    sebi_turnover_rate        = 0.000001    # Rs 10 per crore
    gst_rate                  = 0.18        # on brokerage + exch + sebi only

@dataclass(frozen=True, slots=True)
class CostBreakdown:
    brokerage: float; stt: float; exchange_txn: float; sebi_fee: float
    stamp_duty: float; gst: float; dp_charge: float
    @property total(self) -> float
    def bps_of(self, buy_turnover: float) -> float

def compute_costs(buy_turnover, sell_turnover, product, *,
                  n_buy_orders=1, n_sell_orders=1, cfg=DEFAULT_COSTS) -> CostBreakdown
```

Verified on the running system (`uv run --frozen ta cost --delivery --notional 100000`):

```
DELIVERY round trip on Rs 100,000
  brokerage          0.00
  STT              200.00
  exchange           5.94
  SEBI               0.20
  stamp duty        15.00
  GST                1.11
  DP charge         15.34
  TOTAL            237.59   (23.76 bps)
```

Intraday on the same notional: **₹82.45 (8.25 bps)** — no DP charge, STT on one leg.

Four asymmetries that are easy to get wrong and are all encoded:

- STT is 0.1% on **both** legs for delivery but 0.025% on the **sell leg only** for intraday.
- Stamp duty is charged on the **buy** leg only.
- Intraday brokerage is capped **per executed order**, so scaling into a position multiplies the
  cap. `compute_costs(100_000, 100_000, INTRADAY, n_buy_orders=3).brokerage == 80.0`, not 40.0.
- GST applies to brokerage + exchange + SEBI, **not** to STT or stamp duty.

`_r2` rounds half-up via `Decimal`, not banker's rounding — money rounds half-up and
`round()`'s banker's rounding would produce off-by-a-paisa drift that compounds over a ledger.

**The DP charge is the design driver.** It is flat, so its bps cost is inversely proportional to
position size:

| Position | DP drag |
| --- | --- |
| ₹10,000 | 15.34 bps |
| ₹25,000 | 6.14 bps |
| ₹50,000 | 3.07 bps |
| ₹1,00,000 | 1.53 bps |
| ₹5,00,000 | 0.31 bps |

That table is why `RiskConfig.min_notional = 50_000`, which with a 0.8% risk budget on ₹5L caps
the workable stop distance at 12.5%, which caps the book at seven names. The whole risk model
falls out of one flat fee.

### 1.4 `ta_core.calendar` — NSE sessions

Two load-bearing decisions.

**A session is a weekday-and-not-a-holiday *or* an explicitly declared special session.** The
second clause exists because Muhurat trading falls on Diwali, which can land on a Saturday or
Sunday. A calendar generated from a Mon–Fri weekmask — which is how `pandas_market_calendars`
works — *structurally cannot represent it*, so no such library is used.

**For historical dates the authoritative source is not a holiday list at all: it is whether NSE
published a bhavcopy.** `TradingCalendar.from_observed(session_dates)` builds a calendar from
ingested session dates. The exchange's own output is ground truth and cannot drift from reality.
Weekend sessions in the observed set are automatically recorded as `special_sessions`; weekdays in
range with no bar become `holidays`. The YAML holiday list is only needed to schedule *forward*.

```python
PREOPEN_START = time(9, 0); MARKET_OPEN = time(9, 15); MARKET_CLOSE = time(15, 30)

@dataclass(frozen=True, slots=True)
class TradingCalendar:
    holidays: frozenset[date]
    special_sessions: frozenset[date]
    @classmethod from_observed(cls, session_dates) -> TradingCalendar
    def is_trading_day(d) -> bool
    def next_session(d) / prev_session(d) -> date      # raise after 400 days
    def shift_sessions(d, n) -> date
    def sessions_between(start, end) -> list[date]     # start EXCLUSIVE, end INCLUSIVE
    def iter_sessions(start, end) -> Iterator[date]
    def session_open(d) / session_close(d) -> datetime # tz-aware IST
```

`sessions_between` is **exclusive at the start** because that is how a holding period counts: the
entry day is day 0, not day 1. Session arithmetic must agree with the labeller exactly or time
stops and barrier horizons diverge.

`__post_init__` raises if a date is in both sets — a contradiction that would otherwise resolve
silently in favour of whichever branch ran first.

### 1.5 `ta_core.timefmt` — the one date normaliser

Every feed this system will read dates differently, and several omit the timezone entirely. The
module exists because the obvious fallback — defaulting to `now()` when parsing fails — destroys
both the novelty signal and the staleness check: a five-year-old recycled story looks brand new.
`UnparseableDate` is raised instead.

Seven observed formats are handled. The trap is #6:

```
'Fri, 30 Oct 2026 18:40:00'   RBI — RFC822 SHAPE BUT NO TIMEZONE
```

A naive RFC822 parser assumes UTC and silently shifts every RBI event 5h30m into the future.
`_FORMATS` is a tuple of `(format, has_tz)` tried in order; anything without `%z` is interpreted as
**IST**, because every one of these publishers is Indian. That assumption is explicit here rather
than implicit in a third-party parser.

`_strip` handles CDATA wrappers (Mint, BusinessLine ship them) and rewrites `GMT`/`UTC` to `+0000`
so `%z` matches.

`IST = timezone(timedelta(hours=5, minutes=30), name="IST")` is defined here and imported
everywhere else. `to_ist()` refuses a naive datetime rather than guessing.

This module has no consumer yet — the news pipeline is M7. It is tested by doctest and is correct
today, which is the point: it was written before the thing that needs it.

### 1.6 `ta_core.glossary` — plain-English definitions

47 terms, served to the UI by `/api/glossary`. Lives in the **backend**, not the UI, for the same
reason every other number does: one source of truth. A tooltip hand-written in React drifts from
the rule that actually fires, and the drift is invisible until someone trades on the tooltip.

```python
@dataclass(frozen=True, slots=True)
class Term:
    term: str; short: str; plain: str; why: str = ""; group: str = "general"

GLOSSARY: dict[str, Term]
def describe(term: str) -> Term | None
def as_dict() -> dict[str, dict[str, str]]
```

Groups: `action` (6), `reason` (14), `sleeve` (6), `trend` (8), `metric` (13).

Two machine-checked properties, both in `tests/test_action_semantics.py`:

- `test_every_code_the_api_emits_has_a_plain_english_entry` — every `reasons[]` code, `action` and
  `sleeve` appearing in `/api/recommendations` must have an entry. A chip the user cannot hover for
  a meaning is a wall of acronyms.
- `test_glossary_explanations_do_not_lean_on_other_codes` — no entry's `plain` text may contain
  another `UPPER_SNAKE` code. *If an explanation needs a second acronym explained, it is not plain
  yet.*

The audience is explicitly **not** a fund manager. `MA_STACK_ALIGNED` means nothing to someone who
has not met it before.

---

## 2. `ta_datafeed` — ingestion and storage

**Package:** `packages/datafeed/src/ta_datafeed/` · **Deps:** `ta-core`, `httpx`, `polars`,
`pyarrow`, `clickhouse-connect`, `pymongo`.

`pyarrow` is an explicit dependency with a comment explaining why: `clickhouse-connect`'s fast path
is `insert_arrow`, and `polars.to_arrow()` requires pyarrow at runtime even though polars itself
does not.

### 2.1 `http.py` — `PoliteClient`

Three behaviours, each learned from a real failure mode.

**Per-host token bucket.** NSE and news sites block clients that hammer them, and being blocked
mid-backfill is expensive.

```python
HOST_DELAYS = {
    "nsearchives.nseindia.com": 1.0,
    "www.nseindia.com": 5.0,
    "niftyindices.com": 2.0,
    "api.bseindia.com": 2.0,
    "www.bseindia.com": 2.0,
    "_default": 2.0,
}
```

`_Bucket.wait()` holds a `threading.Lock` across the `time.sleep`, so concurrent callers serialise
rather than all sleeping the same interval and firing together.

**Immutable-response disk cache.** A bhavcopy for 2024-03-15 will never change. Cache path is
`data/raw/{subdir}/{sha256(url)[:16]}_{basename}`. The hash prefix disambiguates URLs that share a
filename; the readable suffix means you can find a file by eye.

**Raw bytes land on disk BEFORE anything parses them.** If the parser has a bug — and it will, NSE
changes CSV headers without notice — the fix is a re-parse, not a re-download. Everything
downstream is reconstructible from `data/raw/`. Measured: **497 files, 86 MB**, laid out as
`data/raw/nse_bhavcopy/YYYY/MM/`, `data/raw/nse_constituents/YYYY/MM/DD/`, `data/raw/nse_master/`.

```python
def get_bytes(url, *, subdir="misc", reuse=True, archive=True,
              headers=None, allow_404=False) -> bytes | None
```

**`reuse` and `archive` are deliberately separate knobs**, and conflating them loses data. A past
bhavcopy never changes, so it is both reused and archived. An index constituent list *does* change,
so it must never be reused — but it must still be archived, or the dated membership snapshot that
`md.index_members` depends on is gone forever the moment NSE updates the file. Principle 3 is
*"raw bytes are immutable"*, not *"raw bytes are cached"*.

`allow_404=True` returns `None` rather than raising, because for a bhavcopy probe a 404 is a
meaningful **answer** — NSE publishes nothing on a holiday — not an error.

The default `User-Agent` is a Chrome string. Note the posture limit this sits inside: the client
fetches **published archive files only**. It does not warm a browser session, does not carry
cookies and does not defeat bot protection. `www.nseindia.com` returns 403 to non-browser clients
and the design routes around that rather than through it; NSE's Terms of Use prohibit systematic
automated collection.

### 2.2 `parse/udiff.py` — the bhavcopy parser

```python
UDIFF_REQUIRED = ("TradDt", "TckrSymb", "SctySrs", "OpnPric", "HghPric",
                  "LwPric", "ClsPric", "PrvsClsgPric", "TtlTradgVol", "TtlTrfVal")
class BhavcopyFormatError(ValueError)
def parse_udiff_bhavcopy(blob, *, knowledge_time, series=("EQ","BE")) -> pl.DataFrame
def bhavcopy_url(d: date) -> str
```

**Defensive by design: maps columns by NAME, never by position.** NSE changes column headers
without notice. A missing required column is a hard `BhavcopyFormatError` rather than silently
producing nulls that look like real zeros. Optional columns (`ISIN`, `LastPric`,
`TtlNbOfTxsExctd`) degrade to a literal default.

`_unzip` sniffs the `PK` magic bytes so the function accepts both the zip and a bare CSV.

`series=("EQ","BE")` is the default and everything else in the file — sovereign gold bonds, ETFs,
debt, rights entitlements — is excluded, because they have different microstructure and would
pollute cross-sectional factor ranks. Measured in the store: **1,115,047 EQ rows + 105,568 BE
rows**.

**`knowledge_time` is when the file became available to us, not the trade date.**
`BHAVCOPY_PUBLISH_IST = time(18, 30)` in `sources/nse_archive.py` is combined with the session date
in IST and converted to UTC. A decision made at 15:00 on 2026-10-29 could not have used a file that
lands at 18:30 that evening — and the PIT guard makes that structural rather than aspirational.

The output frame is the `md.bars_1d` shape with `deliv_qty`/`deliv_pct` stubbed to 0 (the delivery
percentage lives in a separate NSE file that is not yet ingested) and `source='nse_udiff'`.

### 2.3 `parse/constituents.py` — index membership + industry taxonomy

`niftyindices.com/IndexConstituent/ind_nifty<N>list.csv` carries the schema
`Company Name, Industry, Symbol, Series, ISIN Code`. **The official `Industry` column is the heat
map's grouping** — using the exchange's own taxonomy means the sector labels match what every
Indian market participant sees, and it is free and first-party.

`INDEX_FILES` maps 10 index names to file slugs. `parse_constituents` normalises headers
case-insensitively and `pick()` accepts aliases (`"isin code"` or `"isin"`), raising with the
observed column list if neither is present.

Rows are stored as a **dated snapshot**: `snapshot_date` is a column, not an overwrite. Membership
churns constantly; applying today's list to a past date is survivorship bias and materially
inflates backtest returns. This is leakage vector #2 from `asof.py`'s docstring.

### 2.4 `sources/nse_archive.py` — `NSEArchiveSource`

```python
@dataclass(frozen=True)
class SessionResult:
    d: date; bars: pl.DataFrame | None; was_session: bool

class NSEArchiveSource:
    def fetch_session(self, d) -> SessionResult
    def fetch_range(self, start, end) -> list[SessionResult]
    def fetch_constituents(self, index_name, snapshot_date) -> pl.DataFrame
```

`fetch_session` returning `was_session=False` on a 404 makes **the exchange's own publication
schedule the trading calendar**. This is the most reliable calendar source available because it is
the exchange's output rather than a transcription of it.

`fetch_range` skips Saturdays and Sundays before requesting (saves ~290 requests/year) and catches
per-day exceptions so one bad day cannot kill a backfill.

`fetch_constituents` passes `reuse=False, archive=True` and a **day-granular** subdir
(`nse_constituents/YYYY/MM/DD/`). The comment explains the granularity: two snapshots in one month
must not collide, because overwriting one silently reintroduces the survivorship bias the table
exists to prevent.

Note `fetch_constituents` stamps `knowledge_time = datetime.now(UTC)` — correct for a live
snapshot, and the reason a *replay* at a past as-of legitimately sees zero index members. See
§4.1's `as_of_for` note.

### 2.5 `store.py` — `GuardedClient`

**The guard is installed at the only door into the database**, not left to each caller to remember.

```python
CH_HOST = os.environ.get("TA_CH_HOST", "127.0.0.1")
CH_PORT = int(os.environ.get("TA_CH_PORT", "8123"))

class GuardedClient:                              # context manager
    def insert_df(self, table, df: pl.DataFrame) -> int   # via insert_arrow
    def command(self, sql) -> Any
    def query_df(self, sql, as_of: AsOf | None = None) -> pl.DataFrame
    def query_pit(self, table, as_of, *, columns="*", where=None,
                  order_by=None, limit=None) -> pl.DataFrame
    def query_admin(self, sql) -> pl.DataFrame            # UNGUARDED
    def scalar(self, sql) -> Any
```

`query_pit` is the only correct way to read history. It builds the query *and* passes `as_of`
through, so the bound is checked for **equality**, not presence.

`query_admin` is the deliberate, named escape hatch, and the docstring is the argument for its
existence: *"How many rows are in `md.bars_1d`?" is not a point-in-time read. The guard rightly
refuses it, and without a named escape hatch the next person simply reaches past the guard with a
raw client — which is how a **decision** read ends up unbounded.* It is named to be obvious in
review and it is greppable:
`tests/pit/test_guard_hardening.py::test_query_admin_is_not_used_in_decision_code` greps
`packages/quant`, `packages/llm`, `packages/ledger` and `apps/api` and fails on any hit. Verified
green: its only caller is `ta_cli.__main__.cmd_status`.

Settings pinned at connect: `max_execution_time=300`.

**Known cost:** a fresh `GuardedClient()` costs **~38 ms** to construct (measured p50 38.2 ms, min
37.1 ms over 10 constructions); a warm query on the same client is **0.8 ms**. `apps/api` builds a
new client per request. See §5.6.

### 2.6 `backfill.py` — the ingestion entry points

```python
BARS_COLS = [...17 names...]
def ingest_sessions(start, end, *, raw_root="data/raw") -> dict
def refresh_constituents(indices=("NIFTY50","NIFTY100","NIFTY200","NIFTY500"),
                         snapshot_date=None, *, raw_root="data/raw") -> dict
def refresh_symbol_master(*, raw_root="data/raw") -> int
```

`ingest_sessions` is **idempotent**: the HTTP layer caches immutable bodies so a repeat run costs
almost nothing, and `md.bars_1d` is `ReplacingMergeTree(ingested_at)` keyed on `(symbol, d)`.
Returns `{"requested", "sessions", "holidays", "rows", "failed"}`.

> **Caveat the docstring understates.** ReplacingMergeTree collapses duplicates only on a
> background merge whose timing is not guaranteed. "Cannot double-count" is true *eventually*, not
> *on read*. This is HANDOFF bug #1 and the reason `load_panel_inputs` dedupes in polars — see
> §4.1.

`refresh_symbol_master` carries a real-data scar. NSE ships `EQUITY_L.csv` with **leading spaces**
in most headers (`" ISIN NUMBER"`, `" SERIES"`). The code normalises once into a
`{stripped_upper: original}` map and looks every column up through it:

```python
cols = {c.strip().upper(): c for c in df.columns}
missing = [c for c in ("SYMBOL", "ISIN NUMBER", "NAME OF COMPANY", "SERIES") if c not in cols]
if missing:
    raise RuntimeError(f"EQUITY_L.csv missing {missing}; got {sorted(cols)}")
```

The inline comment records what the previous version did wrong: it *fell back to a bare literal
name on miss*, which could never match the real spaced header, so the fallback was dead code that
looked like a safety net.

`listed_date` is stubbed to `1990-01-01` for every symbol — there is no listing-date source wired
up. This matters: see the `listed_sessions` finding in §4.2.

---

## 3. `ta_quant` — factors, trend, barriers, pipeline

**Package:** `packages/quant/src/ta_quant/` · **Deps:** `ta-core`, `ta-datafeed`, `polars`, `numpy`.

### 3.1 `factors/price.py` — vectorised price/volume factors

All computed with polars window functions **over the whole panel at once**. ~2,700 symbols ×
500 sessions runs in well under a second, which matters because this runs nightly and a per-symbol
Python loop would take minutes.

```python
FACTOR_COLUMNS = [
    "ret_1d","ret_5d","ret_21d","ret_63d","ret_126d","ret_252d","mom_12_1",
    "sma20","sma50","sma200","ma_stack","atr14","realised_vol_21d",
    "rsi14","adx14","bb_z","dist_52w_high","dist_52w_low","rvol_20","adtv_20",
]
def add_price_factors(df: pl.DataFrame) -> pl.DataFrame   # df sorted by (symbol, d)
```

**Invariant: every factor uses only data at or before the bar it is attached to.** The
`over("symbol")` partitioning plus strictly-trailing windows is what makes that true. Any factor
needing a centred window would leak the future and is **absent on purpose**.

`_wilder(col, n)` is `ewm_mean(alpha=1/n, adjust=False, min_samples=n)`. RSI and ADX are *defined*
with Wilder smoothing, not a simple mean; using a rolling mean produces numbers that look plausible
and disagree with every chart the user will compare against. This is a correctness-by-convention
decision: the "right" answer is the one the user's broker terminal shows.

`mom_12_1` skips the most recent month (`close.shift(21) / close.shift(252) - 1`) — the standard
academic definition, because short-horizon reversal contaminates raw 12-month momentum.

`ma_stack` is a ternary: `+1` when `close > sma20 > sma50 > sma200`, `-1` when fully bearish, `0`
mixed. It feeds both `ReasonCode.MA_STACK_ALIGNED` and the `*_STRONG` trend predicates.

All intermediates are prefixed `_` and dropped at the end:
`df.drop([c for c in df.columns if c.startswith("_")])`. **This convention has a side effect that
bit the pipeline — see §4.2.**

### 3.2 `trend.py` — multi-timeframe trend states

178 lines. Trend is the load-bearing half of the dashboard's analysis surface and the easiest place
to fool yourself.

```python
LOOKBACKS = (20, 63, 252)
TREND_COLUMNS = [trend_20/63/252, t_drift_20/63/252, er_20/63/252,
                 adx_delta_10, flips_63d, trend_enum_version]
def nw_lag(n: int) -> int                       # floor(n ** 1/3); (20,63,252) -> (2,3,6)
def add_trend_states(df: pl.DataFrame) -> pl.DataFrame
```

#### Why a Newey-West HAC variance instead of an OLS t-stat

This is the brief's second named decision, and the argument is statistical rather than stylistic.

The obvious approach is to regress `log(close)` on `t` and read the slope's t-stat. On price data
that is **badly wrong**: the residuals of a trend line fitted to a price series are strongly
autocorrelated (prices are near-integrated), the OLS standard error assumes IID residuals and is
therefore far too small, and the t-stat is inflated by roughly the square root of the
autocorrelation inflation factor. The practical consequence is that **almost every random walk
scores "significant"** — a trend classifier built that way labels noise as `UP` and the learning
loop downstream is fed observations that never happened.

The module instead takes the t-stat of the **mean daily log return** with a Newey-West (Bartlett)
long-run variance:

```python
def _hac_t_stat(ret, n, s):
    lag = nw_lag(n)
    mean = ret.rolling_mean(n).over(s)
    gamma0 = (ret * ret).rolling_mean(n).over(s) - mean**2
    var = gamma0
    for k in range(1, lag + 1):
        gamma_k = (ret * ret.shift(k).over(s)).rolling_mean(n).over(s) - mean**2
        var = var + 2.0 * (1.0 - k / (lag + 1.0)) * gamma_k
    var = pl.when(var > 1e-12).then(var).otherwise(1e-12)
    return mean / (var / n).sqrt()
```

Two changes, both necessary:

1. **Returns, not levels.** Daily log returns are far closer to serially uncorrelated than log
   prices are, so the residual autocorrelation the HAC correction must absorb is small rather than
   near-unity. Differencing first is what makes the correction tractable.
2. **Bartlett-weighted autocovariances.** `gamma_k` is the lag-k autocovariance estimated inside
   the same rolling window; the weight `(1 - k/(L+1))` keeps the variance estimate positive
   semi-definite, which a flat-weighted (truncated) estimator does not guarantee. `L = floor(N^⅓)`
   is the standard rule.

The clamp `var = max(var, 1e-12)` is there because a HAC variance **can still go negative in small
samples** even with Bartlett weights, when the window is short relative to the lag. Clamping makes
the t-stat finite and the row merely uninformative, instead of null-propagating through every
downstream comparison and quietly dropping the symbol.

#### Direction is not strength

`UP` requires a *significant* drift. A stock that drifted up 2% over 63 noisy sessions is `RANGE`.
Two further statistics separate the cases:

- **Kaufman efficiency ratio** `_efficiency_ratio` = `|close_t − close_{t−n}| / Σ|Δclose|`, in
  [0,1]. 1.0 is a straight line; ~0 is a round trip. This distinguishes "went up" from "went up in
  a straight line" — a trend you can hold from a chop that happens to end higher.
- **`adx_delta_10`** = `adx14 − adx14.shift(10)`. Separates "trending" from "trending less than it
  was", which is exactly what the `*_DECAYING` states capture.

`_classify(n)` is a first-match-wins `when/then` chain:

| Order | State | Predicate |
| --- | --- | --- |
| 1 | `UNDEFINED` | `_bars_seen < n + 21` or any of `t`, `er`, `adx` is null |
| 2 | `UP_STRONG` | `t ≥ 2.5` ∧ `er ≥ 0.35` ∧ `ma_stack == 1` ∧ `adx14 ≥ 25` |
| 3 | `DOWN_STRONG` | `t ≤ −2.5` ∧ `er ≥ 0.35` ∧ `ma_stack == −1` ∧ `adx14 ≥ 25` |
| 4 | `UP_DECAYING` | `t ≥ 1.7` ∧ `er ≥ 0.25` ∧ `adx_delta_10 < −2.0` |
| 5 | `DOWN_DECAYING` | `t ≤ −1.7` ∧ `er ≥ 0.25` ∧ `adx_delta_10 < −2.0` |
| 6 | `UP` | `t ≥ 1.7` ∧ `er ≥ 0.25` |
| 7 | `DOWN` | `t ≤ −1.7` ∧ `er ≥ 0.25` |
| 8 | `RANGE` | otherwise |

Thresholds are deliberately asymmetric between "strong" and plain: the strong states gate position
sizing, so they must be rare.

#### `UNDEFINED` is not `RANGE`

Too little history, or a failed liquidity gate, means the trend was **never measured**. Recording
that as "range-bound" would feed the learning loop an observation that never happened. The
glossary entry says so in plain English: *"This is NOT the same as 'sideways' — it means not
measured."*

#### Hysteresis

A state change requires the raw predicate to hold on **two consecutive sessions**:

```python
pl.when(pl.col(f"_raw_{n}") == pl.col(f"_raw_{n}").shift(1).over(s))
  .then(pl.col(f"_raw_{n}"))
  .otherwise(pl.col(f"_raw_{n}").shift(1).over(s))
```

Without it a symbol oscillating around a threshold emits a stream of state flips, each of which
looks like a real event to the attribution layer. The system would then learn from events that are
artefacts of its own thresholds.

`flips_63d` counts `trend_63` changes in a rolling 63 sessions. A name with ≥ 6 flips in a quarter
is excluded from trend-following sleeves (`TRD-05` in the pipeline — the **single largest rejection
category on the live run: 93 of 106 rejections**).

### 3.3 `barriers.py` — P(target), P(stop), P(time-stop)

277 lines. *"The most dangerous numbers here. A percentage looks authoritative whether or not it is
earned, so this module is built to REFUSE more readily than it answers."*

```python
MIN_N_EFF = 200.0          # Kish effective sample size floor
KAPPA     = 30.0           # Dirichlet concentration of the shrink prior
WIDE_INTERVAL_PP = 0.15    # 90% interval wider than 15pp suppresses the point estimate

@dataclass(frozen=True, slots=True)
class BarrierEstimate:
    state: BarrierState; p_up/p_dn/p_vert: float|None; ci_lo/ci_hi: float|None
    n_eff: float; reason: str; horizon: Horizon = SWING; k_up: float = 2.0; k_dn: float = 1.0
    @property wide(self) -> bool
    def as_dict(self) -> dict

def build_path_library(bars, *, horizon=SWING, k_up=2.0, k_dn=1.0) -> pl.DataFrame
def estimate_barriers(lib, *, atr_pct=None, horizon=SWING, k_up=2.0, k_dn=1.0) -> BarrierEstimate
```

#### Empirical, not analytic

A Brownian/lognormal kernel systematically **understates P(stop)** on Indian equities: it has no
gaps, no circuit limits and no fat tails, and all three of those hit the stop rather than the
target. The module resamples real forward paths, ATR-standardised at t0, so gaps and circuits are
in the sample by construction.

#### Competing risks, one Dirichlet

The three outcomes are mutually exclusive and exhaustive over the horizon, so they come from **one
Dirichlet posterior** and sum to 1 by construction. Three independently-fitted Betas would not sum
to 1 and the error would be invisible — the API contract test
(`test_barrier_block_never_shows_a_number_it_has_not_earned`) asserts `|Σtriad − 1| < 1e-3`
precisely because this is the kind of error that renders plausibly.

#### Pessimistic same-bar resolution

When a single bar's range spans both barriers, daily data **cannot say which came first**. The code
records `DOWN`:

```python
.when(pl.col("_hd") <= pl.col("_hu")).then(pl.lit("DOWN"))
```

The `<=` is the whole decision. Choosing `UP` would flatter every backtest in exactly the cases
that hurt most.

#### `n_eff`, never a row count

Overlapping forward windows are not independent observations. Each start carries a uniqueness
weight `w = 1/h` (h = horizon in sessions), and the display gate is **Kish's effective sample
size**:

```python
sw, sw2 = float(w.sum()), float((w**2).sum())
n_eff = (sw * sw) / sw2 if sw2 > 0 else 0.0
```

Treating h overlapping starts as h independent draws would inflate `n_eff` by an order of
magnitude. A raw count may not appear in any UI element. Below `MIN_N_EFF = 200` the estimate is
`UNAVAILABLE` with a reason string and the UI renders an em-dash. *A 73% with four observations
behind it is not a weak estimate — it is a different kind of object, and the two must not share a
visual language.*

#### Conditioning and shrinkage

`_vol_tercile` splits the library on ATR/price terciles. Volatility is the strongest conditioner of
barrier outcomes **and it is observable at t0**, so conditioning on it leaks nothing. The cell's
weighted counts are shrunk toward the pooled base rate:

```python
alpha = [KAPPA * prior_c + obs_c * (n_eff / obs_tot) for c in (UP, DOWN, VERTICAL)]
p = [a / sum(alpha) for a in alpha]
```

`KAPPA = 30` means a cell needs ~30 effective observations before it meaningfully departs from the
pooled estimate. The credible interval is the **Beta marginal of the Dirichlet for `p_up`
specifically** — `var_up = a_up(a_tot − a_up) / (a_tot²(a_tot+1))`, ±1.645 SD for 90%. It is not an
interval around the triad, and API consumers must not read it as one.

#### The barrier geometry must equal the live geometry — HANDOFF bug #3

This is the brief's third named decision and it has the clearest failure story of the four.

An early version measured **P(touch −1.0 ATR)** while the pipeline placed the stop at **−1.5 ATR**.
A −1.0 ATR level is touched far more often than a −1.5 ATR level, so `p_dn` was overstated. Since
expectancy is `TARGET_ATR·p_up − STOP_ATR·p_dn − costs`, an inflated `p_dn` dragged every
candidate's expectancy negative, and the screener concluded that **no stock in the Nifty 200 was
worth buying on any night**. That is a plausible-looking output. Nothing raised. The system was
confidently measuring a trade it was not placing.

The fix is structural rather than a corrected constant — `pipeline.py` binds the two together at
module scope and says why:

```python
STOP_ATR = 1.5
TARGET_ATR = 2.0
#: The barrier geometry MUST equal the live geometry. [...]
K_UP, K_DN = TARGET_ATR, STOP_ATR
```

so `build_path_library(..., k_up=K_UP, k_dn=K_DN)` and `estimate_barriers(..., k_up=K_UP,
k_dn=K_DN)` cannot drift from the stop and target actually written onto the card. `BarrierEstimate`
additionally **carries `k_up`/`k_dn` in its payload** and prints them into `reason`, so the
geometry is visible in the recorded row: `"empirical, swing h=10, k_up=2.0 k_dn=1.5"`. Verified in
the live `/api/recommendations` response.

This is the same class of error PLAN.md warns about for the **time** stop (*"H must equal the live
engine's time-stop exactly, or labels and execution disagree"*), applied to the **price** barriers.
If you add a horizon, a sleeve-specific stop, or a per-candidate target, this coupling is the
thing to re-establish first.

#### Measured today

The live run's path library yields `n_eff = 25,755` and the base-rate triad
`p_up = 0.2995, p_dn = 0.4815, p_vert = 0.2190`, CI `[0.2948, 0.3042]`. Against a 2.0 ATR target
and a 1.5 ATR stop, gross expectancy is `2.0(0.2995) − 1.5(0.4815) = −0.123 ATR` before costs.
That is the honest arithmetic, and it is why the system issues zero BUYs — see §4.3. The best
card in run `65bd044013ec` scores **+0.0455 ATR** (CPPLUS, ACMESOLAR) against a **+0.05** bar.

### 3.4 `pipeline.py` — the nightly screen (pure)

497 lines, **no I/O**. Takes frames, returns frames. This is what makes the whole nightly run
testable without a database; `nightly.py` holds the I/O.

```python
STOP_ATR, TARGET_ATR = 1.5, 2.0
K_UP, K_DN = TARGET_ATR, STOP_ATR
MIN_EXPECTANCY_ATR = 0.05
_BOOK_LEVEL_RULES = frozenset({"RISK-04", "RISK-05", "RISK-06", "RISK-08"})

@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str; as_of: datetime; d: date
    recommendations: pl.DataFrame; rejections: pl.DataFrame
    sector: pl.DataFrame; breadth: pl.DataFrame; trend: pl.DataFrame
    n_universe: int; regime: Regime

def build_panel(bars, members) -> pl.DataFrame
def sector_aggregates(panel, d, universe=None) -> pl.DataFrame
def breadth_row(panel, d, universe, symbols) -> dict
def infer_regime(breadth: dict) -> tuple[Regime, str]
def score_universe(panel, d, tradeable) -> pl.DataFrame
def run_nightly(bars, members, *, d, portfolio, tradeable_index="NIFTY500",
                scored_index="NIFTY500", max_cards=25, cfg=DEFAULT, as_of=None) -> RunResult
```

#### Order of operations, and why

1. **factors + trend** — panel-wide, vectorised.
2. **sector aggregates** — so the heat map is one SELECT from a precomputed table.
3. **hard gates** — liquidity/surveillance/geometry, **before** scoring, so an untradeable name
   never consumes ranking budget.
4. **composite score** — sector-neutralised cross-sectional ranks.
5. **barrier estimate** — refuses rather than guesses.
6. **risk gates + sizing** — the capital-protection rules decide the size.
7. **record** — plans **and** rejections, *because a gate nobody can see is a gate nobody trusts.*

#### `sector_aggregates` and the Unclassified tile

`universe` restricts the rollup to named index constituents. The comment records what happens
without it: the whole ~2,700-symbol market rolls up, ~2,400 of which carry no industry label, and
the heat map is dominated by a single meaningless "Unclassified" tile larger than every real sector
combined. The function also filters `industry != "Unclassified"` explicitly, and
`test_heatmap_is_not_swamped_by_an_unclassified_bucket` asserts the endpoint never emits one.

`cap_weighted_ret` is **turnover-weighted**, so a thin stock's 15% print does not move the sector
tile the way an equal-weight mean would. `median_ret` is reported alongside, because the two
disagreeing is itself information.

#### `score_universe` — sector-neutral ranks

```python
composite = 0.30·z(mom_12_1) + 0.20·z(ret_63d) + 0.25·z(rs_sector)
          + 0.15·z(adx14) + 0.10·(−z(realised_vol_21d))
```

where `z(col)` is standardised **within industry** (`c.mean().over("industry")`). Ranking across
industries would return whichever sector ran hardest this month — *a sector bet wearing a
stock-picking costume*.

`rs_sector = ret_21d − mean(ret_21d) over industry`, i.e. 21-day relative strength against the
stock's own industry.

These weights are hard-coded here. PLAN.md's M2 gate calls for them to be frozen into
`weights.v1.json` and git-tagged as the permanent `FROZEN_CONTROL` arm. **That file does not
exist.** Until it does there is no frozen control, so the four-arm comparison has no baseline and
the project's single most valuable number cannot be computed. See §9.

#### `infer_regime` — honest about being four thresholds

```python
if disp > 0.030:                      -> HIGH_VOL
if above >= 0.60 and med > 0:         -> TREND_UP
if above <= 0.35:                     -> TREND_DOWN
otherwise                             -> CHOP
```

*"A deliberately crude, auditable stand-in for the M10 HMM. Stated plainly on the dashboard as a
heuristic, because calling this a 'regime model' when it is four thresholds would be the kind of
overclaim the rest of the system exists to prevent."* It gates risk only; it never flips a signal's
side.

Live today: `TREND_DOWN`, note `"only 23% above 50-DMA"`.

> **Duplication to be aware of.** This exact threshold ladder is reimplemented in
> `apps/api/main.py::_regime_from_breadth`. The two agree today. They are separate code and
> nothing asserts they stay in agreement — if you change one, change both. The right fix is to
> move it into `ta_core`.

#### Why `evaluate_entry` is gated against a growing book — HANDOFF bug #4

The brief's fourth named decision. The loop body is:

```python
decision:   Decision = evaluate_entry(pf,        cand, regime=regime, cfg=cfg)
affordable: Decision = evaluate_entry(portfolio, cand, regime=regime, cfg=cfg)
...
if not tradeable:
    continue            # a WATCH card consumes no capital; the book does not grow
pf = replace(pf, cash=pf.cash - decision.notional,
             positions=(*pf.positions, Position(...)))
```

The earlier version passed the **starting** `portfolio` to every candidate. Each candidate was
therefore sized as though it were the only position in an empty book. Every book-level rule —
`RISK-04` portfolio heat, `RISK-05` position count and cash floor, `RISK-06` industry
concentration, `RISK-08` pair correlation — is a function of the *current* book, so against a
constant empty book **none of them ever bound**. The screener emitted 25 cards which, taken
together, breached every cap the system claimed to enforce: more than 7 positions, more than 2
names per industry, heat far above 6%, and gross exposure above 100% of equity.

This failed silently and attractively. Each individual card was internally consistent. The risk
panel showed green, because `rule_states` evaluates the *actual* book, which was empty. The breach
existed only in the set of recommendations, which nothing cross-checked.

Note the asymmetry in the fix, which is easy to get wrong in the other direction: a **WATCH** card
must *not* grow the book. It advises nothing and consumes no capital, so letting it consume
headroom would starve genuinely actionable candidates further down the ranking. The `continue`
before the `replace()` is load-bearing.

`tests/test_risk_gates.py::test_sequential_construction_actually_binds_the_industry_cap` is the
regression: it loops six Financial Services candidates against a book it grows itself and asserts
at most `finserv_max_names` (3) are accepted.

#### Two sizings, because they answer two different questions

The sequential-gating fix above introduced a second, subtler confusion, and the fix for *that* is
the `decision` / `affordable` pair. From the comment:

> **TWO sizings, answering two different questions.** Collapsing them was confusing: a card showed
> a size that shrank as earlier cards above it consumed the hypothetical book, so the same stock
> was "worth Rs 74k" at rank 3 and "Rs 0" at rank 20, **for reasons invisible on the card.**
>
> - `decision` — what you are advised to buy **NOW**, gated against the book as it fills. This is
>   the instruction.
> - `affordable` — the most you could put into this name today given your **REAL** portfolio: your
>   risk appetite, the cash you hold, and the headroom left in its industry. This is the question
>   *"how much of this could I buy?"* and it does not change based on what ranked above it.

Note the two calls differ in exactly one argument — `pf` (the running hypothetical book) versus
`portfolio` (the real, unchanged starting book). Everything else, including the regime caps and the
config, is identical.

This is the right split and it is worth defending. The sequential gate is **necessary** for the
instruction to be honest (a set of cards must be jointly takeable), but it makes the *size* on a
WATCH card a function of its rank, which is meaningless to a reader who is not buying the whole
list. A per-name affordability figure gated against the real book is stable, interpretable, and is
what a user asking "how much of this could I buy?" actually wants.

The split lands in the payload as:

| Field | Gated against | Meaning |
| --- | --- | --- |
| `qty`, `notional`, `risk_rupees`, `risk_pct` | `pf` — the running book | **the instruction.** Zero unless `action == BUY` |
| `would_qty`, `would_notional`, `would_risk_rupees` | `portfolio` — the real book | **affordability.** Stable per name, independent of rank |
| `afford_note` | — | `affordable.detail` when allowed (e.g. `"risk 0.48% of equity, stop 3.2%"`), otherwise `"{rule_id}: {reason}"` |

On an alternate card (`-ALT`) all the `would_*` fields are zeroed and `afford_note` becomes
`"displaced by {rule_id}"` — the candidate was blocked by a book-level cap, so there is no
affordability figure to state.

#### Book-level rejections become ranked alternates

```python
_BOOK_LEVEL_RULES = frozenset({"RISK-04", "RISK-05", "RISK-06", "RISK-08"})
```

These rules reject because **the book is full**, not because the candidate is bad. Such candidates
are re-emitted as `WATCH` cards with `rec_id` suffixed `-ALT`, `qty = would_qty = 0`,
`barrier_state = UNAVAILABLE`, and a reason naming the rule that displaced them. The user should be
able to see that a good idea was displaced by a cap, not silently dropped.

> **Inconsistency found.** The comment above that block reads *"Shown as HOLD, never BUY"*; the
> code writes `Action.WATCH.value`. The **code is correct** — `HOLD` is portfolio-only and
> `tests/test_action_semantics.py` would fail if an alternate were emitted as `HOLD`. The comment
> is stale and should be fixed to say WATCH.
>
> Also: the alternates slice is `alternates[: max(0, max_cards - len(recs)) + max_alternates]`,
> which with `max_alternates = max_cards = 25` can emit up to 25 extra cards *beyond* the 25-card
> budget. It is latent today (zero alternates on the live run, because the book is empty so no
> book-level rule fires) and would surface the first night a position is held.

#### `_reasons` and `_sleeve`

`_reasons(row)` maps factor thresholds onto closed `ReasonCode` members and falls back to
`QUALITY_COMPOSITE` if none fire — *"free text would make attribution impossible."* `_sleeve(row)`
is a four-branch classifier defaulting to `MOMENTUM_BREAKOUT`.

#### RISK-P1 as a downgrade, not a rejection

```python
tradeable = expectancy >= MIN_EXPECTANCY_ATR     # 0.05 ATR after full costs
action = Action.BUY if tradeable else Action.WATCH
```

The twenty-line comment above this is the most important prose in the pipeline. The barrier
estimate is the **arm-invariant BASE rate** — symbol-agnostic by design — and the EDGE term that
would differentiate a candidate contributes exactly `0.0` until the system has resolved ≥ 30 of its
own trades. With symmetric-ish barriers a base rate is ≈ zero expectancy *before* costs and
necessarily negative after them. So a hard `+0.05 ATR` rejection gate on BASE alone would reject
100% of candidates on every night **forever**: that is not a risk control, it is a shutdown with
extra steps.

The honest treatment is to publish the plan and label it truthfully. A card marked
`no demonstrated edge` with its arithmetic printed is informative; an empty screen that silently
swallowed 367 candidates is not.

Expectancy arithmetic:

```python
gross_atr = TARGET_ATR·p_up − STOP_ATR·p_dn
cost_atr  = (cost_bps / 10_000) · price / atr
expectancy = gross_atr − cost_atr
```

Costs are computed on the **actual** notional, not a round number, because the DP charge is flat
and a ₹50,000 position is materially more expensive per rupee than a ₹1,00,000 one.

#### `run_nightly` invariants

- `as_of` is **recorded, never re-derived**. A replay in 2027 must stamp the instant the decision
  was actually made, not the one the replay happens to run at. This is ADR-0002's rule applied to
  the one environment-sampled value the deterministic pipeline has.
- `run_id = uuid.uuid4().hex[:12]`, and `rec_id = f"{run_id}-{symbol}"`. The run_id is what makes
  `ev.recommendations` a history rather than a snapshot — see [DATA_MODEL.md §4](DATA_MODEL.md).
- The path library is built **once** from the whole panel, not per candidate.
- `tradeable_index="NIFTY500"` (what may be bought), `scored_index="NIFTY500"` (what is ranked),
  per `CLAUDE.md`'s universe decision. A narrower tradeable index remains configurable.

### 3.5 `nightly.py` — the I/O half

```python
PUBLISH_IST = time(18, 30)
LOOKBACK_SESSIONS = 420
def as_of_for(d: date) -> AsOf
def latest_session(ch) -> date
def load_panel_inputs(ch, d, *, a: AsOf, lookback=420) -> tuple[pl.DataFrame, pl.DataFrame]
def current_portfolio(ch, *, equity=500_000.0) -> Portfolio
def execute_nightly(d=None, *, equity=500_000.0, replay=False) -> RunResult
def persist(ch, res: RunResult) -> dict[str, int]
```

#### Live as-of vs replay as-of — the bug the separation caught

```python
a = as_of_for(day) if replay else AsOf(datetime.now(UTC))
```

`as_of_for(d)` is 18:30 IST on session `d`, in UTC (`as_of_for(date(2026,10,30)).sql()` →
`'2026-10-30 13:00:00'`). The docstring records what it is **not**:

> For a live run the decision instant is *now*, not the session's publish time: deciding on Monday
> evening from Friday's close legitimately sees everything learned over the weekend, including an
> index membership snapshot taken this morning. Conflating the two was the first bug this
> separation caught — a live run found **zero index members**, because today's snapshot is
> correctly invisible to Friday's as-of.

That is worth internalising: `md.index_members.knowledge_time` is stamped `now()` at fetch, and the
only snapshot in the store is dated **2026-10-31** while the latest session is **2026-10-30**. A
replay of 2026-10-30 bounded at `'2026-10-30 13:00:00'` therefore legitimately sees **no** index
members, so `trade_syms` is empty, so `score_syms` falls back to `trade_syms` (also empty) and the
screen produces nothing. This is correct PIT behaviour and will stay true until enough dated
snapshots accumulate. **`ta daily` runs live, not replay**, which is why it works today.

#### `load_panel_inputs` and the dedupe-on-read — HANDOFF bug #1

Two axes are bounded independently: `d` bounds the **bar date**, `a` bounds what was **knowable**.
The plan is emphatic that these are different (`knowledge_time ≠ event date`).

The lookback is resolved by a subquery that takes the last 420 distinct dates visible at `a`, then
`query_pit` bounds the bar read. Then:

```python
before = bars.height
bars = bars.unique(subset=["symbol", "d"], keep="last").sort(["symbol", "d"])
if bars.height != before:
    log.warning("dropped %d duplicate bar rows (unmerged ReplacingMergeTree parts)", ...)
```

The comment is the whole lesson:

> ReplacingMergeTree collapses duplicates only on a background merge whose timing is not
> guaranteed, so re-ingesting a session leaves two rows for `(symbol, d)` until then. That is not a
> cosmetic problem here: a duplicated bar shifts every trailing window by one, so **SMA20 silently
> becomes a 20-row average of 19 distinct sessions** and the whole factor stack is quietly wrong.

Every rolling window — SMA, ATR, RSI, ADX, the HAC autocovariances, the 52-week extremes — is
computed by row position within `over("symbol")`, not by date. One duplicated row shifts all of
them. Nothing errors. The factor values stay in a plausible range. **Dedupe on read rather than
trusting the engine's timing** is the correct general policy against ReplacingMergeTree anywhere
correctness depends on row identity.

`md.bars_1d` currently has **24 active parts**; `SELECT count()` and
`SELECT count() FROM (SELECT DISTINCT symbol, d …)` both return 1,220,615, so there are no live
duplicates *right now* — which is exactly the state that makes this bug easy to re-introduce.

`md.index_members` is additionally collapsed to the latest snapshot at or before `d` per
`(index_name, symbol)`:

```python
members.sort("snapshot_date").group_by(["index_name", "symbol"]).last()
```

This is what makes membership **dated** rather than current.

#### `persist` — independent writes

Each table is written in its own `try`, *because one failure must not lose the others — a missing
heat map is better than a missing run*. Failures are logged and recorded as `-1` in the returned
dict rather than raised.

`_stamp` sets `as_of`, `run_id` and `knowledge_time` (= `as_of`) on recommendation rows. Note that
`knowledge_time == as_of` for recommendations: the row becomes knowable at the instant the decision
was made, which is the correct semantics and is what lets `/api/recommendations` serve a past
recommendation **exactly as recorded** rather than recomputing it.

Explicit dtype casts before each write (`UInt16`, `Float32`, `UInt32`) exist because
`insert_arrow` is strict and polars' inferred `Int64`/`Float64` will not silently narrow.

---

## 4. `ta_ledger` — capital protection

**Package:** `packages/ledger/src/ta_ledger/` · **494 lines, one module.** `broker/` is an empty
package. The hash-chained event ledger, the `RunManifest`, `rebuild_from_ledger()`, `replay`,
`outcomes`, `attribute`, `posteriors`, `bandit`, `calibration` and `kpis` modules named in PLAN.md
**do not exist**. What exists is `risk.py`, and it is complete.

### 4.1 The arithmetic the whole design falls out of

```
Rs 5,00,000 equity
flat Rs 15.34 DP charge per scrip per delivery-sell  ->  Rs 50,000 position floor
0.8% risk per trade (Rs 4,000) with a 6.0% portfolio heat cap
------------------------------------------------------------------
=> SEVEN concurrent positions, not ten.
```

PLAN.md's "~10 concurrent positions" satisfies the DP floor but **not** the heat cap at a realistic
stop distance. Seven is the number satisfying both; nine is an absolute backstop. The module
docstring corrects the plan in place rather than leaving the two to disagree.

The honest cost, stated once: the ₹50,000 floor combined with the 0.8% risk budget caps the
workable stop distance at **12.5%**. That structurally excludes high-ATR names and biases the book
toward correlated large caps. The diversification rules push back against that bias, *which is why
`RISK-08`'s residual-correlation gate is not optional*.

### 4.2 Three enforcement points, deliberately distinct

| Kind | When | Effect |
| --- | --- | --- |
| **GATE** | before sizing | candidate never reaches the dossier, never consumes reasoning budget |
| **SIZING** | during sizing | clamps the number but keeps the trade |
| **BREAKER** | post-hoc, against the book | can halt new entries |

A rule specified before the statistics that would justify it is `DISPLAY_ONLY` and contributes
nothing to any decision. *Showing it as `OK` would imply an assurance nobody has earned.*

### 4.3 Types

```python
@dataclass(frozen=True, slots=True)
class RiskConfig:                      # DEFAULT = RiskConfig()
    equity_base = 500_000.0
    min_notional = 50_000.0 ; max_notional = 100_000.0
    risk_per_trade_pct = 0.008 ; risk_per_trade_ceiling_pct = 0.010
    max_stop_distance_pct = 0.125
    max_positions = 7 ; max_positions_backstop = 9
    cash_floor_pct = 0.08 ; cash_target_pct = 0.10
    max_gross_pct = 0.92 ; max_portfolio_heat_pct = 0.060
    max_industry_pct = 0.25 ; max_industry_names = 2
    finserv_industry = "Financial Services" ; finserv_max_pct = 0.30 ; finserv_max_names = 3
    max_pair_residual_corr = 0.65 ; pair_corr_release = 0.55
    corr_pair_combined_cap_pct = 0.15
    min_adtv_rupees = 5e7 ; max_pct_of_adtv = 0.005
    min_trades_per_day = 3_000 ; min_price = 30.0 ; min_listed_sessions = 150
    max_daily_realised_loss_pct = 0.020 ; max_daily_mtm_dd_pct = 0.035
    max_5d_loss_pct = 0.040 ; max_21d_loss_pct = 0.070
    dd_rungs = (0.08, 0.12, 0.15, 0.18) ; dd_halt_pct = 0.25
    consecutive_loss_warn/half/halt = 6 / 8 / 10

class Position:  symbol, industry, qty, entry, last, stop, opened
    @property notional      -> qty * last
    @property risk_rupees   -> max(0.0, (last - stop) * qty)

class Portfolio: equity, cash, positions, watermark, realised_today, ret_5d, ret_21d,
                 consecutive_losses
    @property deployed / heat / heat_pct / drawdown_pct
    def industry_notional(ind) / industry_names(ind)

class Candidate: symbol, industry, price, stop, target, adtv_rupees, n_trades,
                 listed_sessions, series, under_surveillance, pair_corr: dict[str, float]
    @property stop_distance_pct

class Decision: action: RuleAction, qty, notional, risk_rupees, rule_id, reason, detail
    @property allowed -> action in (ALLOW, RESIZE)

class RuleStatus: id, label, state, value, limit, unit, detail
    @property headroom_pct -> clamp(1 - value/limit, 0, 1)
```

**Every threshold in one place, so the dashboard and the gate cannot drift.** These are
pre-committed deliberately: *a drawdown ladder invented while you are in a drawdown is not a risk
rule, it is a rationalisation.*

`Position.risk_rupees` is floored at zero and uses the **current resting stop**, not the entry
stop. A stop trailed above entry has locked in a gain and contributes no heat; using the original
would keep the book permanently hot and block entries it should allow. Two tests pin this
(`test_heat_uses_the_current_stop_not_the_entry_stop`,
`test_a_stop_above_the_last_price_contributes_zero_heat_not_negative`).

### 4.4 `regime_caps` — RISK-25

The regime gates exactly **five** quantities and nothing else:

| Regime | gross | cash | heat | name | new/night |
| --- | --- | --- | --- | --- | --- |
| `TREND_UP` | 0.92 | 0.08 | 0.060 | 0.20 | 3 |
| `CHOP` | 0.80 | 0.20 | 0.045 | 0.18 | 2 |
| `TREND_DOWN` | 0.50 | 0.50 | 0.030 | 0.15 | 1 |
| `HIGH_VOL` | 0.35 | 0.65 | 0.020 | 0.12 | 1 |
| `UNKNOWN` | 0.50 | 0.50 | 0.030 | 0.15 | 1 |

`UNKNOWN` is **identical to `TREND_DOWN`**, and a test asserts it: not knowing the regime must be
as defensive as knowing it is bad. `gross + cash == 1.0` for every row, also asserted.

`new` (entries per night) is in the table but **not consulted by `evaluate_entry`** — see §9.

### 4.5 `evaluate_entry` — order is the design

```python
def evaluate_entry(pf, c, *, regime=Regime.UNKNOWN, cfg=DEFAULT, halted=False) -> Decision
```

*Order matters: the cheapest and most absolute checks run first, so an illiquid or
surveillance-flagged name is rejected before any correlation matrix is consulted.*

| # | Rule | Kind | Condition | On breach |
| --- | --- | --- | --- | --- |
| 0 | `RISK-18` | BREAKER | `halted` | REJECT `HALT_DRAWDOWN` |
| 1 | `RISK-13` | GATE | `price ≤ 0 ∨ stop ≤ 0 ∨ stop ≥ price` | REJECT `INVALID_GEOMETRY` — a programming error, not a risk event |
| 2 | `RISK-12` | GATE | `series != "EQ" ∨ under_surveillance` | REJECT `SURVEILLANCE` |
| 3 | `RISK-11` | GATE | `adtv < ₹5cr ∨ n_trades < 3000 ∨ price < ₹30 ∨ listed_sessions < 150` | REJECT `LIQUIDITY` — **never resize into illiquidity** |
| 4 | `RISK-03` | GATE | `stop_distance > 12.5%` | REJECT `STOP_TOO_WIDE` — never resize |
| 5 | `RISK-05` | GATE | `len(positions) ≥ 7` | REJECT `MAX_POSITIONS` |
| 6 | `RISK-02` | SIZING | `notional = equity·0.008 / stop_distance`, clamped to `min(₹1L, equity·caps["name"])` | resize |
| 7 | `RISK-06` | GATE+SIZING | ≥ cap names in industry → REJECT; else clamp to industry headroom | reject / resize |
| 8 | `RISK-08` | GATE+SIZING | any `pair_corr[held] ≥ 0.65` → clamp to 15% combined cap, REJECT if headroom < ₹50k | reject / resize |
| 9 | `RISK-04` | GATE+SIZING | heat headroom ≤ 0 → REJECT; else clamp so `notional·stop_distance ≤ headroom` | reject / resize |
| 10 | `RISK-05` | GATE+SIZING | spendable = `cash − equity·max(0.08, caps["cash"])`; < ₹50k → REJECT `CASH_FLOOR` | reject / resize |
| 11 | `RISK-05g` | SIZING | `deployed + notional ≤ equity·min(0.92, caps["gross"])` | resize |
| 12 | `RISK-11` | SIZING | `notional ≤ adtv·0.005` | resize |
| 13 | `RISK-01` | GATE | `qty = int(notional // price)`; `qty·price < ₹50,000` → REJECT | reject |
| 14 | `RISK-02` | SIZING | `actual_risk > equity·0.010` → shrink qty; if that drops below the floor, REJECT `RISK_CEILING_VS_FLOOR` | reject / resize |

Two subtleties worth preserving:

- **Round DOWN to whole shares, then re-test the floor.** Rounding up to reach ₹50,000 would
  silently breach the per-trade risk ceiling, *which is the entire reason the floor exists*.
  `test_risk_01_rejects_rather_than_rounding_up_to_the_floor` pins it.
- **The ceiling and the floor can be jointly unsatisfiable**, and that is an explicit `REJECT` with
  its own reason code rather than a silent clamp that violates one of them.

Returned `Decision.action` is `RESIZE` if any clamp fired, `ALLOW` otherwise; `allowed` is true for
both. `reason="ACCEPTED"` and `detail` prints realised risk as a fraction of equity.

#### Found bug: `listed_sessions` never binds in the live pipeline

`RISK-11`'s `min_listed_sessions = 150` is a real gate and is unit-tested
(`test_risk_11_rejects_a_newly_listed_name`). But the pipeline constructs the candidate with:

```python
listed_sessions=int(row.get("_bars_seen") or 999),
```

and `_bars_seen` is created inside `add_trend_states` then **dropped** by that function's trailing
`df.drop([c for c in df.columns if c.startswith("_")])`. Verified empirically: after
`add_trend_states(add_price_factors(df))` the frame contains no column starting with `_`. So
`row.get("_bars_seen")` is always `None` and `listed_sessions` is always `999`. **The newly-listed
check cannot fire from the nightly run.** It is not currently dangerous — a name must also have
≥ 273 bars to escape `TrendState.UNDEFINED` at the 252 lookback, and 420 sessions of history are
loaded — but it is a gate that renders green and never binds, which is precisely the failure mode
`tests/test_risk_gates.py`'s docstring warns about. `ref.symbols.listed_date` is stubbed to
`1990-01-01` for all 2,620 symbols, so there is no alternative source wired up either.

### 4.6 `rule_states` — the dashboard contract

```python
def rule_states(pf, *, regime=Regime.UNKNOWN, cfg=DEFAULT) -> list[RuleStatus]
```

Returns 12 `RuleStatus` rows: `RISK-04`, `RISK-05`, `RISK-05c`, `RISK-05g`, `RISK-07`, `RISK-18`,
`RISK-14`, `RISK-16`, `RISK-17`, `RISK-20`, `RISK-22`, `RISK-10`.

> **The dashboard DISPLAYS these; it never enforces them.** Enforcement lives in `evaluate_entry`
> and in the broker, so a dashboard that is down, stale or simply not being looked at cannot change
> what the system is allowed to do.

That separation is the right one and is worth not eroding: a UI that both shows and enforces a
limit fails open when it fails.

`st(v, lim, warn=0.85)` is the shared classifier: `BREACH` above the limit, `WARN` at ≥85% of it,
`OK` below. `RISK-22` (expected portfolio loss) and `RISK-10` (factor exposure) are hard-coded
`DISPLAY_ONLY` with `value=None`, and `test_rule_states_cover_every_rule_and_never_invent_an_assurance`
asserts both properties.

`RISK-07` (distinct industries ≥ ⌈positions/2⌉) is an **assertion**, not a gate: it is implied by
`RISK-06`, so a failure means the industry join is broken rather than that a risk event occurred.
It is not enforced below 5 positions and the detail string says so.

---

## 5. `apps/api` — the FastAPI read model

**496 lines, one module** (`apps/api/src/ta_api/main.py`). Full endpoint reference with real
captured responses is in [API.md](API.md); this section covers the engineering.

### 5.1 Two disciplines

**1. Every endpoint is one SELECT from a pre-aggregated table.** The heat map's M1 gate is
p95 < 30 ms, achievable only if the nightly run has already done the work. *A panel that needs a
per-request computation over 500 stocks must first add a nightly pre-aggregate; there is no
exception path.* **The API computes nothing** — every number on the dashboard was decided and
recorded by the nightly run.

**2. Reads are PIT-bounded with an explicit as-of.** Each handler pins `_now()` (an `AsOf` at
`datetime.now(UTC)`) and passes it to `query_pit`. Serving a past recommendation must return what
was recorded, never a value recomputed with data that arrived afterwards — which is why
`ev.recommendations` is in `PIT_TABLES` at all.

### 5.2 The `*_pct`-is-a-fraction convention

The module docstring makes this the first thing a reader sees, and it is repeated prominently in
[API.md](API.md#units--read-this-before-anything-else) because misreading it is a 100× error:

> **Every `*_pct` field is a FRACTION of one, not a percentage.** `pct_above_sma50: 0.235` means
> 23.5%; `risk_pct: 0.0079` means 0.79%. The suffix is a trap and it nearly caused a 100× display
> error: read as "already a percentage" it would have rendered *0.24% above the 50-DMA* on a day
> when 23.5% of the universe was above it — wrong by two orders of magnitude and entirely
> plausible-looking.

Rupee amounts carry an explicit `_rupees` suffix. The `/api/industries` payload shows the fix
applied: it emits **both** `headroom_rupees` and `headroom_frac`, with the comment recording that
a bare `headroom` was ambiguous — *the dashboard could not tell rupees from a fraction and had to
guess from magnitude, which silently stops working the day a fraction happens to exceed 1.0.*
`headroom` survives as a deprecated alias for `headroom_rupees`.

### 5.3 `_latest_run` — why run pinning exists

```python
def _latest_run(ch, a: AsOf, d) -> str | None:
    runs = ch.query_df(
        f"SELECT run_id FROM {RECS} WHERE knowledge_time <= {a.sql()} AND d = '{d}' "
        f"GROUP BY run_id ORDER BY max(as_of) DESC LIMIT 1", a)
```

> Re-running the job writes a second set of rows with fresh `rec_id`s, so deduping on `rec_id` is
> not enough: without pinning the run, the dashboard shows every historical run's cards merged,
> each symbol repeated once per run. ReplacingMergeTree does not help — the rows are genuinely
> distinct.

It is hand-written rather than built by `build_pit_query` because that function parenthesises
`where`, which a `GROUP BY` cannot live inside. `as_of` is still passed, so the guard checks the
bound for **equality**.

`/api/screener` and `/api/recommendations` both use it.

### 5.4 `_rows` — polars → JSON

```python
if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
    d[k] = None
```

`v != v` is the NaN test: NaN is the only value unequal to itself, and `math.isnan` would reject
the non-float branch. A NaN serialised into JSON is **invalid JSON** and crashes the client; a
silent `0.0` would be worse, because the client would render it.

### 5.5 Dedupe-on-read, applied unevenly

`/api/screener` and `/api/recommendations` dedupe explicitly (`.unique(subset=["symbol"],
keep="last")` and `.unique(subset=["id"], keep="last")`), with a comment naming the
ReplacingMergeTree merge-timing hazard. `test_screener_returns_no_duplicate_symbols` pins the
screener.

> **Found bug — latent, and observed.** `/api/heatmap` does **not** dedupe. Earlier in this
> session `md.sector_daily` was sitting on **3 unmerged parts** (60 rows, 20 distinct industries)
> and `/api/heatmap` returned **40 rows covering only 14 distinct industries** — "Financial
> Services", "Information Technology", "Healthcare", "Capital Goods" and
> "Automobile and Auto Components" each appearing 3×. Because `LIMIT 40` truncates *after* the
> duplicates, **six real industries were missing entirely** from the heat map: the treemap drew
> five oversized duplicate tiles and silently omitted the tail.
>
> A background merge has since collapsed the table to **1 part / 20 rows**, so the endpoint now
> returns a clean 20 rows and the defect is **no longer observable**. The code is unchanged and the
> bug will recur on the next multi-run night. That is precisely the hazard: a correctness defect
> whose visibility depends on ClickHouse's merge scheduler. The fix is the same one-liner applied
> in the screener. `test_heatmap_is_not_swamped_by_an_unclassified_bucket` passes in both states
> because it only checks for the Unclassified bucket and `len(rows) >= 5`.
>
> `/api/rejections` has the same shape of problem for a different reason, and merging **cannot**
> rescue it: `ev.rejections` is `MergeTree`, append-only by design, and the handler does not call
> `_latest_run`. Measured now: **124 rows across 6 run_ids**, only 18 of which are distinct
> `(symbol, rule_id)` pairs — some repeated **10×**. The latest run contributed 18.
>
> `/api/context` and `/api/risk` read `md.breadth_daily` with `limit=1` and so pick an arbitrary
> row when more than one part is live — harmless when they agree, wrong in principle.

### 5.6 Per-request client construction is the real latency cost

`_ch()` builds a **fresh `GuardedClient` per request**. Measured:

| | p50 | p95 |
| --- | --- | --- |
| `GuardedClient()` construction | **38.2 ms** | 47.6 ms |
| warm `query_admin` on an existing client | **0.8 ms** | — |
| ClickHouse-side `elapsed` for the heatmap SELECT | **2.64 ms** | — |

End-to-end over loopback with HTTP keep-alive, 30 requests each:

| Endpoint | p50 | p95 |
| --- | --- | --- |
| `/api/glossary` (no DB) | **0.3 ms** | 0.3 ms |
| `/api/portfolio` | 43.9 ms | 45.4 ms |
| `/api/heatmap` | 47.9 ms | **58.0 ms** |
| `/api/context` | 47.3 ms | 54.3 ms |
| `/api/recommendations` | 50.8 ms | 56.4 ms |
| `/api/screener` | 54.5 ms | 60.6 ms |
| `/api/trend/RELIANCE` | 53.2 ms | 63.5 ms |

**The M1 gate (`/api/heatmap` p95 < 30 ms) is not met**, and the cause is not the query — it is the
~38 ms connection handshake paid on every request. The pre-aggregation discipline is working
exactly as designed (2.64 ms server-side for a 40-row scan); the client lifecycle is not. A
module-level client or a small pool would put every endpoint comfortably under the gate. This is
the highest-value single fix in the API layer.

### 5.7 SPA mount

```python
_DIST = Path(__file__).resolve().parents[3] / "ui" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="ui")
```

Mounted **last** so it never shadows `/api/*`. `html=True` makes unknown paths fall back to
`index.html`, which is what a client-side router needs. A missing bundle logs a warning rather than
failing startup — the API stays usable without the UI.

CORS allows only `http://127.0.0.1:5173` and `http://localhost:5173`, methods `GET` only.

`/api/trend/{symbol}` sanitises the path parameter to `[A-Za-z0-9\-&]{0,24}` before interpolating
it into SQL. Every other interpolated value is a server-derived `date` or a `run_id` read back from
the database, so the surface is narrow; it is still string interpolation rather than parameter
binding, and a future handler taking a user-supplied string must not copy the pattern.

---

## 6. `ta_cli` — the operator CLI

`packages/cli/src/ta_cli/__main__.py`, 237 lines, argparse, entry point `ta`. *Every pipeline stage
is runnable by hand.*

| Command | Status | Does |
| --- | --- | --- |
| `ta cost [--notional N] [--intraday|--delivery]` | **works** | prints the itemised Indian round-trip cost |
| `ta ingest [--start|--end|--days N]` | **works** | fetch bhavcopy sessions into `md.bars_1d` |
| `ta daily [--end D] [--days N]` | **works** | the whole nightly run, 4 stages |
| `ta constituents [--indices ...]` | **works** | dated index-membership snapshot |
| `ta symbols` | **works** | refresh `ref.symbols` from `EQUITY_L.csv` |
| `ta status` | **works** | row counts and date ranges (the only `query_admin` caller) |
| `ta features [--date D]` | **BROKEN** | `ImportError: cannot import name 'build_features' from 'ta_quant.pipeline'` |
| `ta sectors [--date D]` | **BROKEN** | `ImportError: cannot import name 'build_sector_daily'` |
| `ta screen [--date D] [--top N] [--index X]` | **BROKEN** | `ImportError: cannot import name 'run_screen' from 'ta_quant.screener'` |
| `ta calendar YYYY-MM` | **BROKEN** | `ImportError: cannot import name 'observed_calendar'` |

All four failures verified by execution. The imports are lazy (inside the command function), so
`ta --help` lists them and the failure only appears on invocation. The functionality is not lost —
`sector_aggregates` exists in `pipeline.py` under a different name, `score_universe` is the screen,
and `TradingCalendar.from_observed` is the calendar — but the CLI was written against an older
module layout and never reconciled. **Fixing these is four small functions and would make the
pipeline stage-runnable by hand, which is what the CLI is for.**

`ta daily` is the one that matters and its error handling is deliberate:

> Each stage is independent and a failure is reported rather than raised: a missing constituents
> refresh must not cost you the day's bars, and a screen that fails must not hide the fact that
> ingestion succeeded. The exit code is non-zero if the **screen** did not produce a run, because
> that is the stage whose silence would otherwise look like "no trades today".

That last clause is the right instinct and is worth keeping when the ledger lands.

Stages: `[1/4] ingest` → `[2/4] symbol master` → `[3/4] index membership snapshot` →
`[4/4] screen + size + record`. Only stages 1 and 4 affect the exit code.

---

## 7. Cross-cutting invariants

Collected so you can check a change against them in one place.

| # | Invariant | Enforced by |
| --- | --- | --- |
| 1 | `ta_core` performs no I/O | dependency list + convention; nothing machine-checks it |
| 2 | Every read of a PIT table carries a `knowledge_time` bound that is a literal or a bound parameter | `ta_core.asof.assert_pit_bounded`, installed in `GuardedClient`; `tests/pit/` (36 tests) |
| 3 | A bound, when `as_of` is supplied, must **equal** it | `_assert_bounds_hold`; `query_pit` always supplies it |
| 4 | Every table with a `knowledge_time` column is registered in `PIT_TABLES` | `test_every_pit_table_in_the_schema_is_registered_in_the_guard` parses `infra/clickhouse/schema*.sql` |
| 5 | `query_admin` appears nowhere under `packages/quant`, `packages/llm`, `packages/ledger`, `apps/api` | `test_query_admin_is_not_used_in_decision_code` (grep) |
| 6 | Raw bytes land in `data/raw/` before anything parses them | `PoliteClient.get_bytes` writes before returning |
| 7 | Barrier geometry `(k_up, k_dn)` equals live geometry `(TARGET_ATR, STOP_ATR)` | module-level `K_UP, K_DN = TARGET_ATR, STOP_ATR`; carried in `BarrierEstimate.reason` |
| 8 | Vertical-barrier length equals the live time stop | `HORIZON_SESSIONS`; **no test** |
| 9 | Candidates are gated sequentially against a growing book | `run_nightly` rebuilds `pf` per acceptance; `test_sequential_construction_actually_binds_the_industry_cap` |
| 10 | A WATCH card never grows the book | `continue` before `replace()` in `run_nightly` |
| 11 | Portfolio-only actions are never emitted for an unowned symbol | `PORTFOLIO_ONLY_ACTIONS` + `test_no_recommendation_uses_a_portfolio_only_action_for_an_unowned_name` |
| 12 | `BarrierState.UNAVAILABLE` implies all of `p_up/p_dn/p_vert` are null | `test_barrier_block_never_shows_a_number_it_has_not_earned` |
| 13 | The barrier triad sums to 1 | one Dirichlet; same test asserts `< 1e-3` |
| 14 | `n_eff`, never a raw row count, gates display | `estimate_barriers`; Kish formula |
| 15 | Every emitted code has a glossary entry, and no entry leans on another code | two tests in `test_action_semantics.py` |
| 16 | `*_pct` is a fraction of one | convention + docstring; **no test** |
| 17 | Every API response field the UI renders is present | `test_recommendation_cards_carry_a_complete_trade_plan` |
| 18 | `as_of` is recorded, never re-derived | `run_nightly(as_of=...)`, stamped by `_stamp` |
| 19 | Costs are modelled in exactly one place | `ta_core.costs`; will matter at M3 when the backtester exists |
| 20 | Index membership is read as a dated snapshot, never current membership | `load_panel_inputs`'s `group_by(...).last()` after a PIT-bounded read |

---

## 8. Data flow end to end

```
nsearchives.nseindia.com  ──GET (1 req/s)──▶  PoliteClient
niftyindices.com          ──GET (1 per 2s)─▶       │
                                                   ▼
                                         data/raw/<subdir>/<sha16>_<name>
                                                   │  (bytes land BEFORE parsing)
                                                   ▼
                            parse_udiff_bhavcopy / parse_constituents  (polars)
                                                   │  stamps knowledge_time
                                                   ▼
                             GuardedClient.insert_df  ──▶  md.bars_1d, md.index_members,
                                                           ref.symbols
                                                   │
                      ┌────────────────────────────┘
                      ▼
       load_panel_inputs(ch, d, a)      ← PIT-bounded at as_of; dedupes (symbol,d)
                      │
                      ▼
       build_panel  = add_trend_states(add_price_factors(bars)) ⋈ members[industry]
                      │
         ┌────────────┼──────────────┬──────────────────┐
         ▼            ▼              ▼                  ▼
  sector_aggregates  breadth_row  score_universe   build_path_library
         │            │              │                  │
         │        infer_regime       ▼                  ▼
         │            │     for each candidate:  estimate_barriers
         │            └────────────▶ evaluate_entry(pf, cand, regime)
         │                           compute_costs(notional, …)
         │                           expectancy = 2.0·p_up − 1.5·p_dn − cost_atr
         │                           action = BUY if expectancy ≥ 0.05 else WATCH
         │                           pf := pf + Position   (only if BUY)
         ▼                                    │
   md.sector_daily  md.breadth_daily  md.trend_1d  ev.recommendations  ev.rejections
         │                                    │
         └──────────────┬─────────────────────┘
                        ▼
      FastAPI: one PIT-bounded SELECT per endpoint, zero computation
                        ▼
              Vite/React SPA at 127.0.0.1:8000
```

See [diagrams/](diagrams/) for rendered Mermaid versions.

---

## 9. What is deliberately not built, and what that costs you

Stated plainly, because building on a gap you did not know about is how a week disappears.

### No ledger, no broker, nothing resolves

`packages/ledger` contains `risk.py` only. There is **no** hash-chained `ev.ledger_events` writer,
no `RunManifest`, no `rebuild_from_ledger()`, no `PaperBroker`, no fill simulation.
`ev.ledger_events`, `ev.positions`, `ev.outcomes`, `ev.attribution`, `ev.posteriors` and
`ev.equity_daily` all exist as tables and all contain **zero rows** (verified).

Consequences that cascade:

- The system recommends but **does not record fills**. Nothing ever resolves.
- `/api/portfolio` returns a hard-coded flat book: `equity = cash = 500,000`, zero positions.
  `_portfolio()` reads `ev.positions WHERE status='OPEN'` and gets nothing.
- `/api/industries` returns `rows: []`, because it iterates open positions.
- `/api/calibration` and `/api/simulation` return `INSUFFICIENT` with `n = 0`.
- The barrier `EDGE` term is permanently `0.0` (it needs `n_eff ≥ 30` of the system's **own**
  resolved trades), so expectancy is always the arm-invariant base rate, so **zero BUYs, forever**,
  until this is built.
- The four-arm comparison has no data. Only `arm = "DETERMINISTIC"` is ever written.

**This is the next thing that unblocks everything downstream.** HANDOFF.md says so and it is right.

### No frozen control arm

PLAN.md's M2 gate requires `weights.v1.json` frozen and git-tagged — *"the single most valuable
number in the project"*. The composite weights are hard-coded in `score_universe`. There is no
frozen baseline, so "did the learning help?" is currently unanswerable by construction.

### No LLM layer

`packages/llm` does not exist. No `llama-server`, no model weights, no committee, no personas, no
validators, no GBNF grammars, no generation record. `Arm.COMMITTEE` is an enum member with no
producer. All of PLAN.md §The committee, §Observability and ADR-0001/0002's implementation
obligations are unstarted. ADR-0002's `RunManifest` fields are specified and not written.

**Note:** no orchestrator is installed either. PLAN.md names Prefect 3.9; `uv.lock` contains no
Prefect (see [TECH_STACK.md](TECH_STACK.md)). Scheduling today is **launchd**, one
`StartCalendarInterval` at 18:45. That is adequate for one job and will need revisiting when the
pipeline has stages with different wall-clock slots.

### No news, no fundamentals

`packages/news` does not exist. `nx.news_features`, `nx.filings`, `nx.events`, `fn.statements`,
`fn.ratios`, `fn.shareholding`, `fn.estimates` are in `PIT_TABLES`; only `nx.news_features`,
`fn.statements` and `fn.shareholding` exist as tables, all empty. `ta_core.timefmt` is written and
tested against the seven real feed date formats and has no consumer. `ReasonCode` has six
news/event codes and five fundamentals codes that nothing can emit.

### No corporate-action adjustment

`md.corporate_actions` exists and is empty. `ta_datafeed/adjust/` is an empty package.
`md.bars_1d` stores **raw, unadjusted prices** (the schema comment says so deliberately: adjustment
factors live separately so a late-arriving corporate action never silently rewrites history we
already trained on). **Until the adjust module exists, leakage vector #3 from `asof.py`'s own
docstring is live** — a 1:2 split reads as an instant −50% return and `ret_1d`, `mom_12_1`,
`bb_z`, the HAC drift and every barrier path will all see it. Over a 2-year window across ~3,200
symbols this is not hypothetical. This is the most under-advertised gap in the system.

### No minute bars, no broker API, no intraday

`md.bars_1m` is empty. No Angel One / Upstox adapter. `Horizon.INTRADAY` and
`ProductType.INTRADAY` are modelled in costs and enums and unreachable from the pipeline, which
hard-codes `Horizon.SWING` and `ProductType.DELIVERY`.

### No backtester

`ta_quant/backtest/` is empty. M3's anti-drift invariant — *replaying history through the paper
engine reproduces the backtest curve to rounding* — has neither side built.
`tests/test_backtest_matches_paper.py`, `tests/test_ledger_rebuild.py`,
`tests/test_committee_shape.py` and `tests/test_gpu_mutex.py` are all named in PLAN.md §Verification
and **none of them exist**. See [TESTING.md](TESTING.md).

### Smaller gaps

- `ta_core.ports`, `dto`, `ids`, `settings` are named in HANDOFF as "still to write" and are absent.
- `md.benchmark_1d` is empty; `/api/context` returns a `benchmark` block with every field `null`
  and `tri_source: "RECONSTRUCTED"` hard-coded. There is **no NIFTY 50 TRI series**, so the equity
  curve has nothing to be benchmarked against.
- `md.features_1d` exists and is empty — factors are computed in-memory each run and only
  `md.trend_1d` is persisted. Re-deriving factors for a past date means re-running the pipeline.
- `md.surveillance` is empty, so `Candidate.under_surveillance` is always `False` and `RISK-12`
  fires only on the `series != "EQ"` branch (3 of 106 rejections on the live run, all `series=BE`).
  **ASM/GSM names are not currently excluded.**
- `deliv_qty`/`deliv_pct` are stubbed to 0, so `ReasonCode.DELIVERY_PCT_SPIKE` can never fire.
- `RISK-08` needs a `pair_corr` dict that nothing computes; the pipeline passes an empty one, so
  the correlation gate never fires. Given that the ₹50k floor *already* biases the book toward
  correlated large caps (§4.1), this is the gap that most directly contradicts a stated design
  intent.
- `regime_caps(...)["new"]` (max new entries per night) is defined and never read.
- `TREND_ENUM_VERSION` is written to `md.trend_1d` and never consumed.

---

## 10. Where the docs and the code disagree

Found while writing this. None are dangerous; all are the kind of drift that costs an hour.

| Claim | Where | Reality |
| --- | --- | --- |
| `ref.symbols 2,620` | HANDOFF.md | Correct **after** a merge. Mid-session the table held 7,860 physical rows across 3 unmerged parts for the same 2,620 distinct symbols; a background merge has since collapsed it to 2,620. Both numbers are "right" at different moments — the table is ReplacingMergeTree, and every row count on a Replacing table is merge-state-dependent. |
| `on disk 39.7 MiB`, `compression ~5.3×` | HANDOFF.md | Now **35.40 MiB on disk / 37.12 MB total**, uncompressed 129.87 MB, **ratio 3.59×** from `system.parts`. The 5.3× figure was derived from an assumed ~170 B/row raw size, not from `data_uncompressed_bytes`. 3.59× is the measured number. |
| `100 passed` | HANDOFF.md | **107 passed** (86 explicit + 21 doctests). |
| `smoke_test.sh` has **11** gates | CLAUDE.md | 10 `chk` calls. HANDOFF.md says 10 and is correct. |
| "launchd agents … written but **not installed**" | HANDOFF.md §Deliberately NOT done | All four **are** installed and loaded (`com.tradingagent.{clickhouse,mongod,api,nightly}`). HANDOFF's own header says "4 agents loaded", so the document contradicts itself. |
| "current lockfile **19** packages" | PLAN.md §ADR-0001 dependency table | `uv.lock` has **52** entries (45 third-party + 6 workspace members + the root). The comparison against DBOS-12 / LangGraph-41 / Prefect-103 should be re-struck against 45. |
| Prefect 3.9 is the orchestrator | PLAN.md §The stack, §Daily schedule | **Not installed.** Scheduling is launchd. |
| `ta screen`, `ta features`, `ta sectors`, `ta calendar` | CLI `--help` | All four raise `ImportError` (§6). |
| "Shown as HOLD, never BUY" | `pipeline.py` alternates block | Code emits `Action.WATCH`. Code is correct; comment is stale. |
| `would_*` / `afford_note` column position | `schema_v2.sql` places them mid-table | Live table has `would_qty`, `would_notional`, `would_risk_rupees`, `afford_note` **last**, i.e. they were added by `ALTER TABLE ADD COLUMN`. Harmless (inserts are by name via Arrow) but a fresh `apply_schema.sh` produces a different physical layout than the running one. |
| `/api/heatmap` p95 < 30 ms | PLAN.md M1 gate, HANDOFF | Measured **58 ms p95**. The query is 2.64 ms; the gap is per-request client construction (§5.6). |
| `~10 concurrent positions max` | CLAUDE.md | Corrected to **7** in `ta_ledger.risk` (and in PLAN.md §Capital protection). CLAUDE.md still carries the old number. |
