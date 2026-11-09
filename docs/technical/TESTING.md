# Testing

What each suite guards, why the PIT suite is the one that matters, how to run them, and what is not
covered.

Companion documents: [LLD.md](LLD.md), [API.md](API.md), [OPERATIONS.md](OPERATIONS.md).

Verified by execution on **2026-10-31**: `uv run --frozen pytest` → **107 passed**.

---

## 1. Running the tests

```bash
cd /Users/msuhi00/Projects/trading-agent

uv run --frozen pytest                       # all 107
uv run --frozen pytest tests/pit -v          # the leakage suite (36)
uv run --frozen pytest tests/test_risk_gates.py -v
uv run --frozen pytest --doctest-modules packages/core   # arithmetic docstrings only
uv run --frozen pytest -k barrier -v
uv run --frozen ruff check                   # lint
```

**No services are required.** ClickHouse-dependent API tests skip themselves via a
`needs_ch` marker that probes `/api/health` at collection time. On a fresh checkout with nothing
running, the suite is still green — that is the payoff of `ta_core` having zero I/O.

From `pyproject.toml`:

```toml
testpaths = ["tests", "packages", "apps"]
addopts = "--doctest-modules -q"
pythonpath = ["packages/core/src", "packages/datafeed/src", "packages/quant/src",
              "packages/ledger/src", "apps/api/src", "packages/cli/src"]
norecursedirs = ["apps/ui", ".venv", "data", "infra", "legacy", "node_modules"]
```

`testpaths` includes `packages` and `apps` because `--doctest-modules` collects executable
docstrings from the source tree, not just from `tests/`. `norecursedirs` excludes `apps/ui` because
the UI build output is not Python and must never be imported by collection.

There is **no CI**, no pre-commit hook and no coverage gate. `pytest-cov` is installed and unwired.
The suite runs when someone runs it.

---

## 2. The 107 tests

| Suite | Tests | Needs ClickHouse | Guards |
| --- | ---: | --- | --- |
| `tests/pit/test_no_unbounded_read.py` | 18 | no | the leakage guard against realistic bad queries |
| `tests/pit/test_guard_hardening.py` | 18 | no | the three ways a `knowledge_time` bound can be a lie |
| `tests/test_risk_gates.py` | 26 | no | every capital-protection rule FIRES |
| `tests/test_api_contract.py` | 19 | 17 of 19 | the dashboard's contract with the API |
| `tests/test_action_semantics.py` | 5 | 4 of 5 | `HOLD` is never emitted for an unowned name |
| **explicit subtotal** | **86** | | |
| doctests in `ta_core.asof` | 5 | no | the guard's own examples |
| doctests in `ta_core.calendar` | 3 | no | Muhurat-on-a-Sunday, session arithmetic |
| doctests in `ta_core.timefmt` | 2 | no | the seven observed feed date formats |
| doctests in `ta_core.glossary` | 2 | no | |
| doctests in `ta_core.costs` | 1 | no | the full Indian cost arithmetic |
| doctests in `ta_ledger.risk` | 2 | no | sizing, RISK-03, RISK-11 |
| doctests in `ta_quant.barriers` | 2 | no | path resolution, the UNAVAILABLE path |
| doctests in `ta_quant.trend` | 1 | no | Newey-West lag rule |
| doctests in `ta_quant.nightly` | 1 | no | `as_of_for` |
| doctests in `ta_datafeed.parse.udiff` | 1 | no | bhavcopy URL shape |
| doctests in `ta_datafeed.parse.constituents` | 1 | no | constituents URL shape |
| **doctest subtotal** | **21** | | |
| **total** | **107** | | |

---

## 3. `tests/pit/` — the suite that matters most

**36 of the 107 tests. If only one thing here is implemented properly, make it this.**

### Why it outranks everything else

Look-ahead bias is categorically different from an ordinary bug. From `ta_core/asof.py`'s
docstring:

> The failure this prevents does not raise an error, does not look wrong, and produces a
> **BEAUTIFUL backtest**. You find out it was fiction only when live results diverge, months later,
> after you have trusted it with money.

Every other failure mode in this system announces itself eventually. A `KeyError` crashes. A wrong
cost model produces a number someone can check against a broker statement. A broken heat map is
visibly broken. Leakage produces *better* results than the truth, arrives through the path of least
resistance, and is indistinguishable from skill right up until money is at stake.

It is also **unfixable retroactively**. A leaked backtest cannot be repaired by patching the query
— every statistic derived from it, every weight tuned on it and every posterior updated from it is
contaminated, and there is no way to tell which. The only defence is structural and must exist
*before* the data does.

The test file states its own standard plainly:

> The decisive test here is `test_guard_catches_a_deliberately_leaky_client`: we wire a stub
> database client that "forgets" the as-of bound, exactly as a hurried human would, and assert the
> guard refuses it. **A guard that has never been shown to catch a real leak is decoration.**

### `test_no_unbounded_read.py` (18)

Three groups.

**Queries that must pass (6).** Bounded reads with a bare bound, a qualified bound, an `AS`-aliased
bound; non-PIT reference tables; `system.parts`; a join with **both** sides bounded. These exist so
the guard does not cry wolf — a guard that rejects correct queries gets disabled.

**Queries that must fail (7).** Each is annotated with the real leakage vector it represents:

| Query | Vector |
| --- | --- |
| `SELECT * FROM md.bars_1d WHERE sym='RELIANCE'` | plain unbounded read |
| `SELECT * FROM md.index_members WHERE idx='NIFTY200'` | today's membership applied to the past = survivorship bias |
| `SELECT * FROM nx.news_features WHERE sym='TCS'` | news visible before it was disseminated |
| `SELECT * FROM md.corporate_actions` | unadjusted splits read as a −50% return |
| `SELECT * FROM fn.statements WHERE sym='INFY'` | financials visible before they were filed |
| join bounding **only one side** | *"survives code review easily"* |
| join bounding **neither** side | |

**The M0 gate (5).** `_LeakyClient` is a stub that executes whatever SQL it is handed.
`_GuardedClient` subclasses it and calls `assert_pit_bounded` first.

```python
def test_leaky_client_really_does_leak():
    """Sanity: without the guard the bad query sails straight through.
       If this ever fails, the next test proves nothing."""

def test_guard_catches_a_deliberately_leaky_client():
    client = _GuardedClient()
    with pytest.raises(PITViolation, match="md.bars_1d"):
        client.query(LEAKY_SQL)
    assert client.executed == [], "the query must never reach the database"
```

The first test is the control. Without it, the second could pass because the stub was broken rather
than because the guard worked — which is the same class of error the whole project exists to
prevent, applied to its own test suite. The `client.executed == []` assertion is also load-bearing:
it proves the guard fires *before* execution, not after.

`test_every_pit_table_is_actually_guarded` loops all 23 `PIT_TABLES` entries and asserts each is
refused unbounded, catching the case where a table is registered but the guard's regex fails to
match its name shape.

### `test_guard_hardening.py` (18)

Regressions for **the three ways a bound can be a lie**, two of which were live holes found on
2026-10-31.

**1. No bound** (1 test) — the obvious case.

**2. A bound evaluated at runtime** (8 parametrised + 2). This was a **live look-ahead path through
the most important guard in the system**:

```python
@pytest.mark.parametrize("rhs", [
    "now()", "now64()", "today()", "yesterday()", "currentDate()",
    "CURRENT_TIMESTAMP", "toDate(now())", "d",
])
def test_a_volatile_bound_is_refused(rhs):
    """`knowledge_time <= now()` moves with the wall clock, so replaying a past day reads
       everything learned since. It is syntactically indistinguishable from a correct query,
       which is what made it dangerous."""
```

The first version of `assert_pit_bounded` proved only that a bound *existed*, so
`WHERE knowledge_time <= now()` passed it and read the future. Note the parametrisation includes
`"d"` — a bare **column reference**, not a function — because the failure is "the RHS is not pinned
to a fixed instant", not "the RHS is a function call".

The companion test asserts a **driver-bound parameter is accepted**, with the reasoning stated:
*refusing it would push callers toward string interpolation, which is strictly worse.*

**3. A bound to the wrong instant** (1 test). A constant copied from another query is a literal, so
it passes every check above, and it reads the future. Only the caller knows which instant was meant,
so `assert_pit_bounded(sql, as_of)` requires **equality** when `as_of` is supplied.

**Two structural tests** that are the most valuable in the file because they fail on a *future*
mistake rather than a past one:

```python
def test_query_admin_is_not_used_in_decision_code():
    """`query_admin` is the deliberate unguarded escape hatch for operational questions
       ("how many rows?"). It must never appear in code that makes or serves a decision,
       or the guard is decorative."""
    forbidden = ["packages/quant", "packages/llm", "packages/ledger", "apps/api"]
    hits = subprocess.run(["grep", "-rn", "query_admin", *present], ...).stdout
    assert not hits
```

```python
def test_every_pit_table_in_the_schema_is_registered_in_the_guard():
    """A table with a knowledge_time column that nobody registered is read unbounded
       forever and nothing complains."""
```

The second parses `infra/clickhouse/schema*.sql` directly, collects every `CREATE TABLE` whose body
declares `knowledge_time`, and asserts the set is a subset of `PIT_TABLES`. **Adding a PIT table and
forgetting to register it fails the build.** That is the correct direction of coupling: the schema
is the source of truth and the Python constant must follow it.

### What the PIT suite does NOT prove

Stated here because overestimating it is the risk.

- It is a **build-time, syntactic check over query text**. Not a SQL parse. A dynamically-assembled
  query string evades it entirely; a `--` comment mentioning `md.bars_1d` trips it.
- It does **not** prove the bound held at execution, only that the text was right when linted.
- It does **not** verify `knowledge_time` was *stamped* correctly at write time. If
  `parse_udiff_bhavcopy` stamped midnight instead of 18:30 IST, every query would be perfectly
  bounded against a wrong value and all 36 tests would pass.
- It does **not** cover the `ASOF JOIN` path (unused today).

PLAN.md names the runtime proof — a `traceparent` span propagated into the ClickHouse session so
every query, its `knowledge_time` predicate and its row count land under the span of the persona
that cited the number. That is M4 and does not exist.

---

## 4. `tests/test_risk_gates.py` (26)

**Every capital-protection rule gets a test that makes it FIRE.** From the docstring:

> A risk rule without a failing-case test is a comment. The specific failure this prevents: a
> predicate that silently evaluates to `None` (or compares against a NULL column) never raises and
> never rejects, so **the rule renders green forever and is indistinguishable from one that simply
> never binds.**

That distinction is the whole design of this file. Asserting a clean candidate is accepted proves
nothing about whether a gate works — only constructing a *breaching* portfolio and asserting the
specific `rule_id` comes back does.

The fixtures make each test vary exactly one thing:

```python
def flat() -> Portfolio:              # Rs 5,00,000, all cash, at watermark
def good(symbol, industry, price, stop) -> Candidate:
    """A candidate that passes everything, so each test varies ONE thing."""
def pos(symbol, industry, qty, price) -> Position:
```

Coverage by rule:

| Rule | Test asserts |
| --- | --- |
| happy path | sized on risk: `3_500 ≤ risk_rupees ≤ 4_000` (0.8% of ₹5L minus whole-share rounding), `₹50k ≤ notional ≤ ₹1L` |
| `RISK-03` | a 20% stop is **rejected, not resized** |
| `RISK-11` | illiquidity rejected **and `qty == 0`** — *a smaller position in a name you cannot exit is still a name you cannot exit* |
| `RISK-11` | a 20-session listing rejected |
| `RISK-12` | both branches: `under_surveillance=True` **and** `series="BE"` |
| `RISK-13` | `stop == price` rejected as invalid geometry |
| `RISK-05` | the **eighth** position rejected — *"The book is SEVEN, not ten. This is the plan's number corrected."* |
| `RISK-06` | a third name in one industry rejected |
| `RISK-06` | a third **Financial Services** name **allowed** — 30%/3 names, because FS is ~24% of the Nifty 200 by count and a 2-name cap would make the book structurally unable to track the index's largest sector |
| `RISK-08` | ρ=0.82 with no room under the 15% combined cap → rejected |
| `RISK-04` | six positions each risking ~1% saturate the 6% heat cap |
| `RISK-05` | cash floor breach, and `"CASH" in d.reason` |
| `RISK-01` | **rejects rather than rounding up to the floor** — rounding up would silently breach the per-trade risk ceiling, *which is the entire reason the floor exists* |
| `RISK-18` | `halted=True` blocks every entry |
| regime | parametrised over **all five** `Regime` members: keys are exactly `{gross, cash, heat, name, new}`, `0 < gross ≤ 0.92`, `gross + cash == 1.0` |
| regime | `HIGH_VOL` sizes strictly smaller than `TREND_UP` |
| regime | `UNKNOWN == TREND_DOWN` — not knowing must be as defensive as knowing it is bad |
| `rule_states` | `RISK-22` and `RISK-10` are `DISPLAY_ONLY` with `value is None` |
| heat | uses the **current** resting stop: a position at `last=1200, stop=1100, qty=100` contributes exactly ₹10,000 |
| heat | a stop **above** `last` contributes **0.0, not negative** |
| drawdown | measured from the watermark; equity above the watermark gives 0.0, never negative |
| sequential | **the composite result** — six Financial Services candidates against a book the test grows itself; at most `finserv_max_names` accepted |

That last test is the regression for HANDOFF bug #4:

```python
def test_sequential_construction_actually_binds_the_industry_cap():
    """Sizing every candidate against the STARTING book is how a screener produces N cards
       that jointly breach every cap it claims to enforce."""
```

It is the only test in the file that exercises the *interaction* between the gate and the caller,
and it is the one that catches the bug that actually shipped.

Two tests accept a set of rule ids (`assert d.rule_id in {"RISK-04", "RISK-05"}`) because the
ordering between the heat cap and the position cap depends on the fixture. That is a small
weakening — it would not catch the two rules swapping places.

---

## 5. `tests/test_api_contract.py` (19)

> These run against the real app object (no network), so they fail if an endpoint stops returning
> the shape the UI renders. The UI computes nothing, which means **a field that silently disappears
> here becomes a blank tile there with no error anywhere** — the exact failure this file exists to
> catch.

Uses `fastapi.testclient.TestClient(app, raise_server_exceptions=False)` so a 500 is asserted on
rather than propagated.

```python
def _ch_up() -> bool:
    return client.get("/api/health").json()["services"]["clickhouse"]

needs_ch = pytest.mark.skipif(not _ch_up(), reason="ClickHouse not running")
```

Note the skip is evaluated at **import time**, so a ClickHouse that dies mid-suite is not detected.

| Test | Guards |
| --- | --- |
| `test_health_always_answers_even_with_everything_down` | the one endpoint that must never fail. Not `needs_ch`-gated |
| `test_every_endpoint_returns_200_and_json` (×10) | parametrised smoke over every non-path endpoint |
| `test_recommendation_cards_carry_a_complete_trade_plan` | 18 required keys; `action` in the six-member enum; **`stop < entry_lo <= entry_hi < target`** — *geometry must be coherent or the card is lying about the trade*; `reasons` non-empty |
| `test_barrier_block_never_shows_a_number_it_has_not_earned` | `state ∈ {OK, UNAVAILABLE}`; `reason` always present; `UNAVAILABLE ⟹ triad all null`; `OK ⟹ Σtriad ≈ 1` (competing risks), `ci_lo ≤ p_up ≤ ci_hi`, `n_eff > 0` |
| `test_a_buy_card_always_has_a_real_size_and_a_hold_card_never_does` | `BUY ⟹ qty > 0 ∧ notional ≥ 50_000 ∧ risk_rupees > 0` |
| `test_risk_rules_expose_state_and_never_a_bare_unexplained_number` | rules list non-empty; `state` in the four-member enum; **`detail` never empty**; `DISPLAY_ONLY ⟹ value is None` |
| `test_calibration_and_simulation_refuse_rather_than_guess` | `INSUFFICIENT ⟹` numeric payload all null **and** the `needed` count appears in the `note` |
| `test_screener_returns_no_duplicate_symbols` | the ReplacingMergeTree merge-timing hazard |
| `test_heatmap_is_not_swamped_by_an_unclassified_bucket` | no `Unclassified` tile; ≥5 rows |
| `test_an_unknown_symbol_404s_rather_than_500s` | accepts `{404, 503}` |

**The barrier test is the most valuable in the file.** `Σtriad ≈ 1` is the kind of invariant that
renders plausibly when violated — three probabilities summing to 1.07 look fine on a card. It is
asserted because the design chose *one Dirichlet* over three independent Betas precisely so the
sum would be structural, and this test is what keeps that true if someone refactors.

### Known weakness

`test_heatmap_is_not_swamped_by_an_unclassified_bucket` passed throughout a window in which the
endpoint was **returning 40 rows covering only 14 distinct industries**, with six industries
missing entirely (see [API.md](API.md#apiheatmap)). The test checks for the `Unclassified` bucket
and `len(rows) >= 5` and nothing else. A `test_heatmap_returns_no_duplicate_industries` mirroring
the screener's test would have caught it — **the pattern existed one file over and was not
applied.**

This is also a lesson about what "green" means here. The duplication appears only while
`md.sector_daily` has more than one live part, and a ClickHouse background merge has since
collapsed it, so the symptom is gone while the code is unchanged. A suite that only asserts
properties on whatever the database happens to contain will certify both states as correct.

Several tests `pytest.skip` when `rows` is empty, so on a database with no recorded run they are
vacuous rather than failing. That is right for a fresh checkout and worth remembering when reading
a green run.

---

## 6. `tests/test_action_semantics.py` (5)

> `HOLD` must never be emitted for something you do not own. A screened candidate labelled HOLD
> reads as advice about an existing position, and quietly hides that the system is not telling you
> to do anything. WATCH is the honest label. **This is a user-facing correctness property, so it
> gets a test rather than a convention.**

This was a **real shipped bug** — git `49f5354 Fix HOLD-for-unowned-name, add plain-English
glossary, architecture doc`.

| Test | Guards |
| --- | --- |
| `test_watch_and_hold_are_distinct_members` | `HOLD ∈ PORTFOLIO_ONLY_ACTIONS`, `WATCH ∉` |
| `test_no_recommendation_uses_a_portfolio_only_action_for_an_unowned_name` | cross-references `/api/recommendations` against `/api/portfolio`'s owned set |
| `test_watch_cards_carry_no_size_and_buy_cards_do` | `WATCH ⟹ qty == 0 ∧ notional == 0` |
| `test_every_code_the_api_emits_has_a_plain_english_entry` | every `reasons[]` code, `action` and `sleeve` has a glossary entry — *a chip the user cannot hover for a meaning is a wall of acronyms* |
| `test_glossary_explanations_do_not_lean_on_other_codes` | no entry's `plain` text contains another `UPPER_SNAKE` code — *if an explanation needs a second acronym explained, it is not plain yet* |

The last test is a genuinely unusual thing to machine-check and it is the right call: "plain
English" is otherwise an aspiration that decays silently as codes are added.

> **Weakness worth knowing:** `test_no_recommendation_uses_a_portfolio_only_action_for_an_unowned_name`
> is vacuously true today, because `/api/portfolio` returns zero positions and
> `/api/recommendations` emits only `WATCH`. It will only start doing real work when the ledger
> exists. It is correct to have written it first.

---

## 7. Doctests (21)

`addopts = "--doctest-modules"` makes the arithmetic in the source **executable**. This is not
decoration — the modules with doctests are exactly the ones where a plausible-looking wrong number
is the danger.

`ta_core.costs` (1) pins the full Indian cost model against hand-checked figures:

```python
>>> c = compute_costs(100_000, 100_000, ProductType.DELIVERY)
>>> c.stt, c.stamp_duty, c.dp_charge
(200.0, 15.0, 15.34)
>>> c.total
237.59
>>> c.bps_of(100_000)
23.76
>>> compute_costs(25_000, 25_000, ProductType.DELIVERY).bps_of(25_000)
28.36
>>> compute_costs(100_000, 100_000, ProductType.INTRADAY, n_buy_orders=3, n_sell_orders=1).brokerage
80.0
```

Those six assertions cover: STT on both delivery legs, buy-only stamp duty, the flat DP charge, the
DP charge's size-dependent bps drag, and the per-order intraday brokerage cap. A regression in any
one changes a published number.

`ta_core.calendar` (3) pins **Muhurat trading on a Sunday**:

```python
>>> cal.is_trading_day(date(2026, 11, 8))   # Muhurat on a SUNDAY
True
>>> cal.shift_sessions(date(2026, 11, 4), 2)   # Muhurat counts as a session
datetime.date(2026, 11, 8)
```

`ta_core.timefmt` (2) pins the RBI trap — RFC822 *shape* with no timezone, which a naive parser
shifts 5h30m into the future.

`ta_core.asof` (5) holds the guard's own three-failure narrative, including the `now()` case, as
executable examples.

`ta_ledger.risk` (2) pins the happy-path sizing (`qty=66`, `risk≈3960`) and two rejections.

`ta_quant.barriers` (2) pins a clean run-up resolving `UP`, and the `UNAVAILABLE` path with its
exact reason string.

`ta_quant.nightly` (1) pins `as_of_for(date(2026,10,30)).sql() == "'2026-10-30 13:00:00'"` — the
18:30 IST → UTC conversion.

---

## 8. What is NOT covered, and the risk that leaves

### 8.1 Tests PLAN.md names that do not exist

| Test | Would guard | Risk of its absence |
| --- | --- | --- |
| `tests/test_backtest_matches_paper.py` | replaying history through the paper engine reproduces the backtest curve to rounding | **the anti-drift invariant**. Neither engine exists, so nothing can drift yet — but the M3 gate is unmeetable until both exist and this test with them. `ta_core.costs` being the single shared cost model is currently the only structural defence. |
| `tests/test_ledger_rebuild.py` | `rebuild_from_ledger() == stored` | the ledger does not exist. Without it the hash chain's tamper-evidence is untested. |
| `tests/test_committee_shape.py` | `COMMITTEE_STEPS` name sequence equals a golden constant; AST walk asserts no conditional branching | the committee does not exist. ADR-0001 makes this test the thing that turns *"control flow is fixed and must never vary"* from a comment into a machine-checked claim — the single rule it steals from LangGraph. |
| `tests/test_gpu_mutex.py` | two concurrent `ModelSession.__enter__` serialise | no LLM layer. "One model resident at a time, enforced at three levels that fail differently" is currently enforced at zero. |
| `ta daily --date X --replay` byte-equality | ADR-0002's replay-from-record | the `--replay` **CLI flag does not exist** (`execute_nightly(replay=True)` does, as a Python kwarg). There is no byte-equality assertion anywhere. |

### 8.2 Untested code paths in what *does* exist

| Gap | Risk |
| --- | --- |
| **`ta_quant.pipeline.run_nightly` has no unit test.** 497 lines — the composite of scoring, gating, sizing, costing and barrier estimation — is exercised only through the API contract tests against whatever the last real run happened to write. | This is the largest single gap. The pipeline is *designed* to be testable (pure, frames in/frames out, no I/O) and that affordance is unused. A synthetic panel through `run_nightly` asserting the card count, the action split and the rejection reasons would be perhaps 40 lines and would catch most pipeline regressions. |
| **`ta_quant.factors.price.add_price_factors` has no test** beyond being invoked. RSI, ADX, ATR and the MA stack are hand-rolled with Wilder smoothing. | A silent off-by-one in `_wilder` or a `shift` changes every factor, every composite rank and every card, while leaving all values in a plausible range. There is no golden-vector test against a known RSI/ADX series. |
| **`ta_quant.trend.add_trend_states` has one doctest, covering only `nw_lag`.** The HAC variance, the hysteresis and the eight-state classifier are untested. | The HAC correction exists *because* the naive t-stat scores random walks as significant. Nothing asserts the correction is actually applied — a regression reverting to `gamma0` alone would relabel large parts of the universe as trending and look entirely normal. |
| **No test asserts barrier geometry equals live geometry.** `K_UP, K_DN = TARGET_ATR, STOP_ATR` is enforced by one line of module-level code. | HANDOFF bug #3 re-introduces itself the moment someone adds a per-sleeve stop. `assert (K_UP, K_DN) == (TARGET_ATR, STOP_ATR)` is a one-line test. |
| **No test asserts `HORIZON_SESSIONS[h]` equals the live time stop.** | Invariant #8 in [LLD.md §7](LLD.md#7-cross-cutting-invariants) is documented and unguarded. It cannot bind until the labeller exists, but the enum comment already claims it. |
| **No test for the `*_pct`-is-a-fraction convention.** | A 100× display error that *nearly shipped* is defended by a docstring. A test asserting every `*_pct` field in every endpoint is in `[-1.5, 1.5]` would catch a percentage leaking in. |
| **`ta_datafeed` has almost no tests** — two URL-shape doctests. `parse_udiff_bhavcopy`, `parse_constituents`, `PoliteClient`'s cache/reuse semantics and `refresh_symbol_master`'s header normalisation are untested. | NSE changes CSV headers without notice; the parser is written defensively for exactly that, and the defence is unverified. There is no fixture bhavcopy in the repo despite 497 real ones sitting in `data/raw/`. The `reuse`/`archive` split — whose conflation would permanently lose index snapshots — has no test. |
| **`ta_cli` has no tests.** | Four of ten subcommands raise `ImportError` on invocation and the suite is green. A single `test_every_subcommand_imports` would have caught all four. |
| **`apps/ui` has no tests.** `npm run build` runs `tsc --noEmit`, so types are checked; behaviour is not. | The UI's honesty rules — em-dash for `UNAVAILABLE`, suppressing a point estimate when the interval exceeds 15 pp, lakh formatting, muted `would_qty` — are product requirements enforced nowhere. `BarrierEstimate.wide` is not even serialised. |
| **`GuardedClient` itself is untested.** The guard is tested in isolation; its installation at the client boundary is not. | A refactor that calls `self._c.query_arrow` directly, bypassing `query_df`, removes the guard silently. |
| **No concurrency tests.** `_Bucket.wait()` holds a lock across a sleep; `GuardedClient` is not thread-safe. | FastAPI runs sync handlers in a threadpool. Each builds its own client, so there is no shared mutable state today — but that is accidental, not asserted. |

### 8.3 Structural gaps in the approach

- **No CI.** Nothing runs the suite except a human. Every guarantee above is conditional on someone
  typing the command.
- **No coverage gate.** `pytest-cov` is installed and unwired, so the untested regions in §8.2 are
  invisible unless you go looking.
- **`needs_ch` is evaluated at import time**, so a ClickHouse that dies mid-suite yields false
  passes rather than errors.
- **Several API tests skip on empty data**, so a run that wrote nothing is indistinguishable from a
  run that wrote correctly.
- **No property-based testing.** `evaluate_entry` is a pure function over a small, well-typed
  parameter space and is a natural fit for Hypothesis — *"no accepted decision ever leaves the book
  in a state violating any cap"* is a property, not 26 examples. Hypothesis is not a dependency.
- **No fixture data.** 497 real bhavcopies sit in `data/raw/` and none is checked in as a test
  fixture, so no parser test can exist without network or a local cache.

### 8.4 The honest summary

The suite is **unusually strong where correctness is invisible** (point-in-time discipline, risk
gates, the barrier refusal contract, action semantics, the money arithmetic) and **thin where
failures are loud** (parsers, the pipeline composite, the CLI, the factor maths).

That is a defensible prioritisation and almost certainly the right one for this stage — it matches
PLAN.md's claim that *~60% of the engineering here produces zero alpha* and is spent on exactly
these guarantees. But it has one soft spot that undercuts the rest: **the factor and trend
computations, on which every downstream number depends, have no golden-vector test.** A silent
arithmetic regression there would flow through the composite, the barrier library, the expectancy
and every card, and every one of the 107 tests would still pass.
