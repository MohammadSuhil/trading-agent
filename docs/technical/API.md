# HTTP API reference

`apps/api/src/ta_api/main.py` — FastAPI, 13 endpoints, **all `GET`**, all read-only. Served at
`http://127.0.0.1:8000`, which also serves the built SPA from `apps/ui/dist`.

Companion documents: [LLD.md §5](LLD.md#5-appsapi--the-fastapi-read-model) (engineering),
[DATA_MODEL.md](DATA_MODEL.md) (the tables behind each endpoint), [OPERATIONS.md](OPERATIONS.md).

Every example response below was captured live on **2026-10-31** against session **2026-10-30**,
run `65bd044013ec`. The book is empty and no trade has resolved, so several endpoints are showing
their refusal paths — which is the point, and is what you will see on a fresh install too.

---

## Units — read this before anything else

### `*_pct` is a FRACTION of one, not a percentage

This is the single most dangerous convention in the API. From the module docstring:

> **Every `*_pct` field is a FRACTION of one, not a percentage.** `pct_above_sma50: 0.235` means
> 23.5%; `risk_pct: 0.0079` means 0.79%. The suffix is a trap and it nearly caused a 100× display
> error: read as "already a percentage" it would have rendered *0.24% above the 50-DMA* on a day
> when 23.5% of the universe was above it — wrong by two orders of magnitude and entirely
> plausible-looking.

Render with `×100`, or a `toLocaleString(…, {style:'percent'})`-equivalent, **always**.

The fields this applies to: `risk_pct`, `pct_above_sma50`, `heat_pct`, `drawdown_pct`, `pnl_pct`,
`pct_equity`, `cap_pct`, `headroom_pct`, `headroom_frac`, `breadth_pct`, `ret_1d`, `ret_5d`,
`ret_21d`, `p_up`, `p_dn`, `p_vert`, `ci_lo`, `ci_hi`, and every `value`/`limit` in `/api/risk`
whose `unit` is `pct`, `pct_floor` or `beta`.

### The other suffixes

| Suffix / field | Unit |
| --- | --- |
| `_rupees` | **rupees**. `risk_rupees`, `would_risk_rupees`, `headroom_rupees` |
| no suffix, money-shaped | rupees. `entry_lo`, `entry_hi`, `stop`, `target`, `notional`, `would_notional`, `equity`, `cash`, `deployed`, `watermark`, `pnl`, `atr14`, `close`, `entry`, `last` |
| `_cr` | **crore of rupees** (1 crore = 10⁷). `turnover_cr: 18399.99` is ₹18,400 crore = ₹184 billion |
| `_bps` | **basis points** (1 bp = 0.01% = 0.0001). `cost_bps: 24.72` is 0.2472% |
| `_atr` | **multiples of the symbol's ATR(14)**. `expectancy_atr: -0.239` is −0.239 × that stock's typical daily range |
| `qty`, `would_qty` | **whole shares** |
| `n_eff` | effective sample size, dimensionless, real-valued |
| `rr` | reward:risk ratio, dimensionless |
| `composite`, `t_drift_*`, `adx14`, `er_*`, `rs_63` | dimensionless statistics (z-units, t-stats, 0–100, [0,1]) |

`headroom` in `/api/industries` is a **deprecated alias** for `headroom_rupees`. It exists because
a bare `headroom` was ambiguous — the dashboard could not tell rupees from a fraction and had to
guess from magnitude, which silently stops working the day a fraction exceeds 1.0. Use the
explicit names.

### Dates and NaN

Dates are ISO strings (`"2026-10-30"`). `as_of` at the top level of most responses is the **latest
session present in `md.bars_1d`**, not the current wall-clock time.

NaN and ±Inf are serialised as **`null`**. A NaN in JSON is invalid and crashes the client; a
silent `0.0` would be worse because the client would render it. So `null` means "not available",
never "zero".

---

## `qty` vs `would_qty`

The split exists because a card can carry a full trade plan **and** not be an instruction.

| Field | Gated against | Meaning |
| --- | --- | --- |
| `qty` / `notional` / `risk_rupees` / `risk_pct` | the **running** book, as cards are accepted | **what you are actually being advised to do.** Zero on any card that is not a `BUY`. |
| `would_qty` / `would_notional` / `would_risk_rupees` | your **real, unchanged** portfolio | **affordability** — the most you could put into this name today. Stable per name, **independent of rank**. |
| `afford_note` | — | why that affordability figure, in words |

From the schema comment on `qty`:

> ACTUAL size being advised. Zero on a WATCH card: you are not being told to buy it, and a card
> that shows both "do not buy" and a share count is the exact ambiguity this split exists to
> remove.

**The two numbers come from two separate `evaluate_entry` calls** that differ in exactly one
argument: the portfolio. The pipeline comment records why collapsing them was wrong:

> Collapsing them was confusing: a card showed a size that shrank as earlier cards above it
> consumed the hypothetical book, so the same stock was **"worth Rs 74k" at rank 3 and "Rs 0" at
> rank 20, for reasons invisible on the card.**

So `qty` answers *"what should I do now"* (and a set of BUY cards is jointly takeable, because the
book grows as they are accepted), while `would_qty` answers *"how much of this could I buy"* and
does not move because something else ranked above it.

**UI contract:** render `qty` in the primary numeric style; render `would_qty` only in muted type,
clearly labelled as affordability, and never in a column headed "Qty". A WATCH card with
`would_qty: 25` is saying *"you could afford 25 shares of this"* — it is not saying buy 25 shares.

`afford_note` is a plain-language string and should be shown verbatim next to the affordability
figure. Three shapes, all observed live:

| Shape | Example | When |
| --- | --- | --- |
| sizing detail | `"risk 0.48% of equity, stop 3.2%"` | the name is affordable; this is `Decision.detail` |
| rule rejection | `"RISK-01: BELOW_MIN_NOTIONAL"` | you could not take this name at all today |
| displacement | `"displaced by RISK-06"` | an `-ALT` card: a book-level cap crowded it out |

Two tests pin this: `test_watch_cards_carry_no_size_and_buy_cards_do` asserts
`WATCH ⟹ qty == 0 ∧ notional == 0`, and `test_a_buy_card_always_has_a_real_size_and_a_hold_card_never_does`
asserts `BUY ⟹ qty > 0 ∧ notional ≥ 50_000 ∧ risk_rupees > 0`.

On an alternate card (`rec_id` ending `-ALT`) **all** of `qty`, `notional`, `would_qty`,
`would_notional` and `would_risk_rupees` are `0`, and `afford_note` names the rule that displaced
it — taking one would breach the cap that crowded it out, so there is no affordability figure to
state.

---

## The `Action` enum

Six members. Three are **portfolio-only**.

| Member | Meaning | Valid for an unowned symbol? |
| --- | --- | --- |
| `BUY` | open a new position now, at the stated size | yes |
| `WATCH` | ranked, **not actionable**; `qty` is 0; the full plan is shown so you can see what the trade would be | yes |
| `AVOID` | explicitly screened out | yes |
| **`HOLD`** | **you own this → keep owning it** | **no** |
| **`TRIM`** | **you own this → reduce it** | **no** |
| **`SELL`** | **you own this → exit in full** | **no** |

```python
PORTFOLIO_ONLY_ACTIONS = frozenset({Action.HOLD, Action.TRIM, Action.SELL})
```

The `WATCH` / `HOLD` distinction is the point, and the enum docstring is explicit that getting it
wrong is a real misstatement rather than a labelling quibble:

> `HOLD` means *you own this and should keep owning it*. It is only ever valid for a symbol that is
> in the portfolio. Using it for a screened candidate you have never bought [...] reads as advice
> about an existing position and hides the fact that the system is not telling you to do anything.

This was a shipped bug (git `49f5354 Fix HOLD-for-unowned-name, …`) and is now machine-checked:
`tests/test_action_semantics.py::test_no_recommendation_uses_a_portfolio_only_action_for_an_unowned_name`
cross-references `/api/recommendations` against `/api/portfolio` and fails on any violation.

`TRIM` and `SELL` have **no producer today** — the broker and position-reviewer layers do not
exist. In practice the API currently emits only `WATCH` (and would emit `BUY` if any candidate
cleared `MIN_EXPECTANCY_ATR`).

---

## The `barrier` block

Every row in `/api/recommendations` carries a nested `barrier` object. It is the system's estimate
of P(target hit first), P(stop hit first), P(time stop), and it is designed to **refuse rather than
guess**.

```jsonc
"barrier": {
  "state":  "OK" | "UNAVAILABLE",
  "p_up":   0.2995,   // fraction; P(target touched before stop) within the horizon
  "p_dn":   0.4815,   // fraction; P(stop touched first)
  "p_vert": 0.2190,   // fraction; P(neither — time stop reached)
  "ci_lo":  0.2948,   // 90% credible interval LOWER bound, around p_up ONLY
  "ci_hi":  0.3042,   // 90% credible interval UPPER bound, around p_up ONLY
  "n_eff":  25755.0,  // Kish effective sample size behind the triad
  "reason": "empirical, swing h=10, k_up=2.0 k_dn=1.5. no demonstrated edge: ..."
}
```

### The `state` / `UNAVAILABLE` contract

**`state: "UNAVAILABLE"` means `p_up`, `p_dn`, `p_vert`, `ci_lo` and `ci_hi` are ALL `null`, and the
UI must render an em-dash plus `reason` — never a number, never a fallback, never a zero.**

From `ta_core.enums.BarrierState`:

> `UNAVAILABLE` must render as an em-dash plus a reason, never as a number. A percentage with four
> observations behind it is not a weak estimate, it is a **different kind of object**, and the UI
> must not let the two share a visual language.

The threshold is `MIN_N_EFF = 200.0` effective observations, where `n_eff` is **Kish's effective
sample size over uniqueness weights**, not a row count — overlapping forward windows are not
independent and treating them as such would inflate the count by roughly the horizon length. A
`reason` string always explains which branch fired, e.g.
`"library too small: n_eff 2.0 of 200 required for swing"`.

`test_barrier_block_never_shows_a_number_it_has_not_earned` enforces all of it:

```python
assert b["state"] in {"OK", "UNAVAILABLE"}
assert b["reason"], "a barrier block must always explain itself"
if b["state"] == "UNAVAILABLE":
    assert b["p_up"] is None and b["p_dn"] is None and b["p_vert"] is None
else:
    assert abs(sum((b["p_up"], b["p_dn"], b["p_vert"])) - 1.0) < 1e-3   # one Dirichlet
    assert b["ci_lo"] <= b["p_up"] <= b["ci_hi"]
    assert b["n_eff"] > 0
```

### Three further rules the UI must honour

1. **The triad sums to 1 by construction.** The three outcomes are competing risks from one
   Dirichlet posterior. Do not renormalise, and do not display them as three independent
   probabilities.
2. **`ci_lo`/`ci_hi` bracket `p_up` ONLY** — they are the Beta marginal of the Dirichlet for the
   up-barrier. They are not an interval around the triad and must not be drawn as one.
3. **Render the interval, not a bare point.** PLAN.md's honesty rules require the barrier block to
   show its interval ("30–30%") and to suppress the point estimate entirely when the 90% interval
   exceeds **15 percentage points** (`WIDE_INTERVAL_PP = 0.15`, exposed on the Python side as
   `BarrierEstimate.wide`). **That flag is not currently serialised** — a client wanting it must
   compute `(ci_hi - ci_lo) > 0.15` itself.

### `reason` carries the geometry and the edge arithmetic

```
empirical, swing h=10, k_up=2.0 k_dn=1.5. no demonstrated edge: E=-0.239 ATR at base
rates (p_up=0.299 p_dn=0.481), costs 24.7 bps = 0.116 ATR. EDGE term is 0.0 until the
system has its own track record
```

`k_up=2.0 k_dn=1.5` is the **barrier geometry**, and it is printed because it must equal the live
geometry (`target = close + 2.0·ATR`, `stop = close − 1.5·ATR`). An earlier version measured
P(touch −1.0 ATR) while placing the stop at −1.5 ATR, which overstated P(stop) badly enough to make
every candidate look negative-expectancy — see
[LLD.md §3.3](LLD.md#the-barrier-geometry-must-equal-the-live-geometry--handoff-bug-3). Printing it
into the recorded row is what makes a future drift visible.

The `no demonstrated edge` clause is appended whenever `expectancy_atr < 0.05`. It is not an error
and it is not a bug: the EDGE term is identically `0.0` until the system has resolved ≥ 30 of its
own trades, so the barrier estimate is the arm-invariant base rate, which against symmetric-ish
barriers is ≈ zero expectancy before costs and negative after them. **Expect every card to carry it
until the ledger exists.**

---

## Endpoints

| Method | Path | Params | Source table(s) |
| --- | --- | --- | --- |
| GET | `/api/health` | — | `md.bars_1d` + Mongo/Redis probes |
| GET | `/api/context` | — | `md.breadth_daily` |
| GET | `/api/heatmap` | — | `md.sector_daily` |
| GET | `/api/screener` | `limit` int, default 50 | `ev.recommendations` ⋈ `md.trend_1d` |
| GET | `/api/recommendations` | — | `ev.recommendations` |
| GET | `/api/rejections` | — | `ev.rejections` |
| GET | `/api/portfolio` | — | `ev.positions` |
| GET | `/api/risk` | — | `ev.positions` + `md.breadth_daily` |
| GET | `/api/industries` | — | `ev.positions` |
| GET | `/api/trend/{symbol}` | `symbol` path | `md.trend_1d`, `md.bars_1d` |
| GET | `/api/calibration` | — | `ev.outcomes` (count only) |
| GET | `/api/simulation` | — | `ev.equity_daily`, `ev.outcomes` (counts only) |
| GET | `/api/glossary` | — | in-process constant |

OpenAPI at `/openapi.json`, Swagger UI at `/docs`.

`limit` on `/api/screener` is clamped server-side to `[1, 200]`. `{symbol}` is sanitised to
`[A-Za-z0-9\-&]` and truncated to 24 chars before SQL interpolation.

Measured latency over loopback with HTTP keep-alive, 30 requests each (see
[LLD.md §5.6](LLD.md#56-per-request-client-construction-is-the-real-latency-cost)): everything that
touches ClickHouse sits at **p50 44–55 ms, p95 45–64 ms**, dominated by a ~38 ms per-request
connection handshake. `/api/glossary`, which touches no database, is **0.3 ms**. The M1 gate for
`/api/heatmap` is p95 < 30 ms and is **not currently met**, though the ClickHouse-side query is
2.64 ms.

---

### `/api/health`

Always answers, even with every service down — the only endpoint that does. `status` is `ok` iff
ClickHouse is reachable; Mongo and Redis are advisory chips. Mongo is probed with a 400 ms
`serverSelectionTimeoutMS`, Redis with a bare 400 ms TCP connect to 6379.

| Field | Type | Meaning |
| --- | --- | --- |
| `status` | `"ok" \| "degraded"` | `ok` iff `services.clickhouse` |
| `as_of` | ISO date \| null | latest session in `md.bars_1d` |
| `data_quality` | `"OK" \| "PARTIAL"` | `OK` iff a session was found |
| `services.clickhouse` / `.mongo` / `.redis` | bool | reachability |

```json
{"status":"ok","as_of":"2026-10-30","data_quality":"OK",
 "services":{"clickhouse":true,"mongo":true,"redis":true}}
```

---

### `/api/context`

Market context for the "Today" header: regime chip, breadth, benchmark.

| Field | Type | Meaning |
| --- | --- | --- |
| `as_of` | ISO date | latest session |
| `regime` | `Regime` member | `TREND_UP \| CHOP \| TREND_DOWN \| HIGH_VOL \| UNKNOWN` |
| `regime_note` | string | the threshold that fired, in words |
| `breadth.advancers` / `.decliners` | int | counts within `breadth.universe` |
| `breadth.pct_above_sma50` | **fraction** | 0.235 = 23.5% |
| `breadth.universe` | string | `NIFTY500` market breadth |
| `benchmark.*` | — | **every numeric field is `null`**; `md.benchmark_1d` is empty |
| `data_quality` | `"OK" \| "PARTIAL"` | `PARTIAL` when no breadth row exists |

```json
{"as_of":"2026-11-04","regime":"HIGH_VOL","regime_note":"cross-sectional dispersion 3.27% > 3.0%",
 "breadth":{"advancers":408,"decliners":89,"pct_above_sma50":0.3737373650074005,
            "universe":"NIFTY500"},
 "benchmark":{"name":"NIFTY 50 TRI","close":null,"ret_1d":null,"ret_21d":null,
              "tri_source":"RECONSTRUCTED"},
 "data_quality":"OK"}
```

**`regime` is a risk gate, never a signal.** It scales exactly five quantities (gross, cash, heat,
single-name cap, new entries per night) and never flips a position's side. The classifier is four
thresholds, stated plainly as a heuristic rather than dressed up as a model — see
[LLD.md §3.4](LLD.md#infer_regime--honest-about-being-four-thresholds).

`benchmark.tri_source` is hard-coded `"RECONSTRUCTED"` and `name` is hard-coded `"NIFTY 50 TRI"`.
No benchmark series exists; the shape is present so the UI tile does not need a second code path
when it lands.

**Float32 artefacts are visible in the wire format.** `0.23499999940395355` is the `Float32` column
widened to a JSON double. Format for display; never compare for equality.

---

### `/api/heatmap`

One row per industry, for the d3 treemap: sized by `turnover_cr`, coloured by `ret_1d`.
`ORDER BY turnover_cr DESC LIMIT 40`. Returns 20 rows today (20 distinct industries); the first
three are shown.

| Field | Type | Unit |
| --- | --- | --- |
| `industry` | string | NSE's official taxonomy; **never `Unclassified`** |
| `n` | int | constituents in the rollup |
| `ret_1d` | float | **fraction**. Turnover-weighted mean 1-day return (`cap_weighted_ret`) |
| `ret_5d` | float | **fraction**. *Actually `median_ret`, the 1-day median* — see the warning below |
| `ret_21d` | float | **fraction**. Median 21-day return (`rs_21d`) |
| `turnover_cr` | float | **crore of rupees** (`turnover / 1e7`) |
| `breadth_pct` | float | **fraction** of constituents up on the day |

```json
{"as_of":"2026-10-30","rows":[
 {"industry":"Financial Services","n":98,"ret_1d":-0.009634284302592278,
  "ret_5d":-0.017606182023882866,"ret_21d":-0.0429459773004055,
  "turnover_cr":18399.994352597,"breadth_pct":0.1734693944454193},
 {"industry":"Information Technology","n":25,"ret_1d":-0.007520422805100679,
  "ret_5d":-0.003680336521938443,"ret_21d":0.07558435201644897,
  "turnover_cr":11359.297949762,"breadth_pct":0.4399999976158142},
 {"industry":"Healthcare","n":44,"ret_1d":0.025654133409261703,
  "ret_5d":0.0067785391584038734,"ret_21d":-0.03042582981288433,
  "turnover_cr":9357.579338908,"breadth_pct":0.6590909361839294}]}
```

> **Two defects in this endpoint, both observed live.**
>
> **1. `ret_5d` is mislabelled.** The handler aliases `median_ret AS ret_5d`, but
> `md.sector_daily.median_ret` is the **median 1-day return**, not a 5-day return. The column is
> not a 5-day anything. A UI labelling it "5d" is wrong.
>
> **2. Duplicate tiles, and six industries silently missing — latent, and intermittent.**
> `md.sector_daily` is a ReplacingMergeTree, and this handler — unlike `/api/screener` and
> `/api/recommendations` — does **not** dedupe on read. Earlier in this session the table held
> **3 unmerged parts** (60 rows / 20 distinct industries) and the response contained **40 rows
> covering only 14 distinct industries**, with "Financial Services", "Information Technology",
> "Healthcare", "Capital Goods" and "Automobile and Auto Components" each appearing **3×**.
> Because `LIMIT 40` truncates *after* the duplicates, **six real industries never reached the
> client at all** — the treemap drew five oversized duplicate tiles and omitted the tail.
>
> A background merge has since collapsed the table to 1 part, so the response above is now a clean
> 20 rows and the defect is **not currently observable**. The code is unchanged; it will recur on
> the next multi-run night. **A correctness defect whose visibility depends on ClickHouse's merge
> scheduler is worse than a reliable one**, because it will be dismissed as unreproducible. The
> fix is the same `.unique(subset=["industry"], keep="last")` already used in the screener.

---

### `/api/screener`

Ranked candidates with trend state and reason chips, pinned to the latest `run_id`.
`?limit=` 1–200, default 50. `ORDER BY composite DESC`.

Two **separate** PIT-bounded reads joined in polars, not one SQL join — the PIT guard cannot prove
which table a bare `knowledge_time` bound belongs to when two PIT tables are in scope, and it is
right to refuse. Qualifying the bound through subquery aliases would be fighting the guard to reach
the same answer less safely.

| Field | Source | Unit |
| --- | --- | --- |
| `symbol`, `name`, `industry` | `ev.recommendations` | |
| `close` | **`entry_hi`, i.e. `close × 1.005`** | rupees — see warning |
| `composite` | `ev.recommendations` | z-units |
| `sleeve` | | `Sleeve` member |
| `reasons` | | array of `ReasonCode` members, ≥1 |
| `rr` | | reward:risk, ≈1.33 by construction |
| `expectancy_atr` | | **ATR units, after costs**; negative is normal today |
| `action` | | `Action` member |
| `trend_state` | `md.trend_1d.trend_63` | `TrendState` member |
| `rs_63` | **`md.trend_1d.er_63`** | Kaufman efficiency ratio [0,1] — see warning |
| `adx14` | `md.trend_1d` | 0–100 |
| `ret_1d`, `ret_21d`, `rvol_20`, `adtv_cr` | — | **always `null`** (placeholders via `setdefault`) |

```json
{"symbol":"GRASIM","name":"Grasim Industries Ltd.","industry":"Construction Materials",
 "close":2996.31,"composite":1.2848999500274658,"sleeve":"MOMENTUM_BREAKOUT",
 "reasons":["ADX_TRENDING"],"rr":1.3300000429153442,
 "expectancy_atr":-0.23899999260902405,"action":"WATCH",
 "trend_state":"RANGE","rs_63":0.0539005845785141,"adx14":36.21348190307617,
 "ret_1d":null,"ret_21d":null,"rvol_20":null,"adtv_cr":null}
```

> **Two aliasing traps.**
>
> `close` is **`entry_hi`**, which is `close × 1.005` — the top of the entry band, not the session
> close. For GRASIM the real close is 2981.40; the API reports 2996.31. Within 0.5%, so it looks
> right, which is what makes it worth knowing.
>
> `rs_63` is **`er_63`**, the Kaufman efficiency ratio, which measures path straightness in [0,1].
> It is **not** relative strength against anything. A UI labelling it "RS" is wrong.
>
> The four `null` placeholders exist so the client has stable keys; they are never populated.

Deduped on read (`.unique(subset=["symbol"], keep="last")` on both sides) because ReplacingMergeTree
dedupes only on an unscheduled background merge, and joining against an unmerged table multiplies
rows. `test_screener_returns_no_duplicate_symbols` pins it.

---

### `/api/recommendations`

The full trade-plan cards. Pinned to the latest `run_id`,
`ORDER BY action ASC, composite DESC`, `LIMIT 200` then `.head(60)`.

`action ASC` is a lexical sort over the enum's **string values**, so the card order is
`AVOID, BUY, HOLD, SELL, TRIM, WATCH`. That happens to put BUYs near the top today; it is
alphabetical coincidence, not priority ordering. A client wanting priority order must sort itself.

| Field | Type | Unit / meaning |
| --- | --- | --- |
| `id` | string | `rec_id`: `{run_id}-{symbol}`, or `…-ALT` for a displaced alternate |
| `symbol`, `name`, `industry` | string | |
| `action` | `Action` member | see [the Action enum](#the-action-enum) |
| `sleeve` | `Sleeve` member | strategy family |
| `horizon` | `Horizon` member | `SWING` today; determines the time stop (10 sessions) |
| `entry_lo`, `entry_hi` | float | **rupees**. The band to buy inside |
| `stop` | float | **rupees**. `close − 1.5 × ATR14` |
| `target` | float | **rupees**. `close + 2.0 × ATR14` |
| `qty` | int | **shares actually advised**. 0 unless `BUY` |
| `notional` | float | **rupees actually advised**. 0.0 unless `BUY` |
| `would_qty` | int | **affordable** shares against your real book — see [qty vs would_qty](#qty-vs-would_qty) |
| `would_notional` | float | **rupees**. Affordable notional |
| `would_risk_rupees` | float | **rupees** at risk to the stop at `would_qty` |
| `afford_note` | string | why that affordability figure, in words |
| `risk_rupees` | float | **rupees**. `(entry − stop) × qty`; 0 unless `BUY` |
| `risk_pct` | float | **fraction** of equity; 0 unless `BUY`. Capped at 0.008 |
| `rr` | float | reward:risk; 2.0/1.5 = 1.33 by construction |
| `cost_bps` | float | **basis points**. Full Indian delivery round trip on the actual notional |
| `expectancy_atr` | float | **ATR units, after costs**. `2.0·p_up − 1.5·p_dn − cost_atr` |
| `composite` | float | z-units; the sector-neutral ranking score |
| `reasons` | string[] | closed `ReasonCode` members, ≥1 |
| `barrier` | object | see [the barrier block](#the-barrier-block) |

```json
{"as_of":"2026-10-30","rows":[{
  "id":"65bd044013ec-GRASIM",
  "symbol":"GRASIM","name":"Grasim Industries Ltd.","industry":"Construction Materials",
  "action":"WATCH","sleeve":"MOMENTUM_BREAKOUT","horizon":"SWING",
  "entry_lo":2966.49,"entry_hi":2996.31,"stop":2885.92,"target":3108.71,
  "qty":0,"notional":0.0,
  "would_qty":25,"would_notional":74535.0,"would_risk_rupees":2387.04,
  "afford_note":"risk 0.48% of equity, stop 3.2%",
  "risk_rupees":0.0,"risk_pct":0.0,
  "rr":1.3300000429153442,"cost_bps":24.719999313354492,
  "expectancy_atr":-0.23899999260902405,"composite":1.2848999500274658,
  "reasons":["ADX_TRENDING"],
  "barrier":{"state":"OK","p_up":0.2994999885559082,"p_dn":0.4814999997615814,
             "p_vert":0.21899999678134918,"ci_lo":0.2948000133037567,
             "ci_hi":0.3041999936103821,"n_eff":25755.0,
             "reason":"empirical, swing h=10, k_up=2.0 k_dn=1.5. no demonstrated edge: E=-0.239 ATR at base rates (p_up=0.299 p_dn=0.481), costs 24.7 bps = 0.116 ATR. EDGE term is 0.0 until the system has its own track record"}}]}
```

**Live state: 25 rows, all `WATCH`, zero `BUY`** — the best card scores **+0.0455 ATR** against a
**+0.05** bar. That is the honest answer, not a failure. Read
[LLD.md §3.4](LLD.md#risk-p1-as-a-downgrade-not-a-rejection) before overriding it.

Geometry is guaranteed coherent: `test_recommendation_cards_carry_a_complete_trade_plan` asserts
`stop < entry_lo <= entry_hi < target` on every row, plus that all 18 required keys are present and
`reasons` is non-empty. A field that silently disappears here becomes a blank tile in the UI with no
error anywhere — that is exactly what the test exists to catch.

---

### `/api/rejections`

Every rejected candidate with the rule that killed it. `ORDER BY rule_id, symbol LIMIT 500`.

| Field | Type | Meaning |
| --- | --- | --- |
| `symbol` | string | |
| `rule_id` | string | `RISK-01`…`RISK-18`, `TRD-03`, `TRD-05`, `DATA` |
| `reason` | string | closed short code, e.g. `BELOW_MIN_NOTIONAL` |
| `detail` | string | the arithmetic, in words |

```json
{"as_of":"2026-10-30","rows":[
 {"symbol":"CHENNPETRO","rule_id":"RISK-01","reason":"BELOW_MIN_NOTIONAL",
  "detail":"Rs 49,880 < Rs 50,000 floor (DP charge would be 3.1 bps)"},
 {"symbol":"MTARTECH","rule_id":"RISK-01","reason":"BELOW_MIN_NOTIONAL",
  "detail":"Rs 41,028 < Rs 50,000 floor (DP charge would be 3.7 bps)"},
 {"symbol":"STLTECH","rule_id":"RISK-12","reason":"SURVEILLANCE",
  "detail":"series=BE surveillance=False"}]}
```

Live breakdown of the 124 rows returned: `TRD-05 / WHIPSAW` 108, `RISK-01 / BELOW_MIN_NOTIONAL`
12, `RISK-12 / SURVEILLANCE` 4 — summed over six runs, from only **18 distinct
`(symbol, rule_id)` pairs**. The `SURVEILLANCE` rejections are all `series=BE` (trade-to-trade);
**no ASM/GSM data is ingested yet**, so `under_surveillance` is always `False`.

This endpoint exists so the gates are auditable rather than invisible: *"the cap, not the signal,
rejected this"* is a thing the user must be able to see.

> **Defect.** Unlike `/api/screener` and `/api/recommendations`, this handler does **not** call
> `_latest_run()`. `ev.rejections` is `MergeTree` (append-only **by design**, so no background
> merge will ever tidy this up) and currently holds **six** run_ids. The response merges all
> **124** rows, of which only **18** are distinct `(symbol, rule_id)` pairs — some repeated **ten
> times**, and growing by one copy per nightly run. The fix is to add `AND run_id = '<latest>'` to
> the WHERE clause, mirroring the other two handlers.
>
> Note this handler is also the only one that reads an unbounded SQL string without an `as_of` —
> which is legal, because `ev.rejections` is deliberately not in `PIT_TABLES` and has no
> `knowledge_time` column. The guard passes it through correctly.

---

### `/api/portfolio`

The open book and its tiles.

| Field | Type | Unit |
| --- | --- | --- |
| `equity` | float | **rupees**. Hard-coded `EQUITY = 500_000.0` |
| `cash` | float | **rupees**. `equity − deployed` |
| `deployed` | float | **rupees**. Σ `qty × last` |
| `n_positions` | int | |
| `heat_pct` | float | **fraction**. Σ `max(0, (last − stop) × qty) / equity` |
| `drawdown_pct` | float | **fraction** from the watermark |
| `watermark` | float | **rupees**. `max(watermark, equity)` — never reset downward |
| `positions[].symbol/industry/qty/entry/last/stop` | | rupees except `qty` |
| `positions[].target` | | **always `null`** — not read from `ev.positions` |
| `positions[].pnl` | float | **rupees** |
| `positions[].pnl_pct` | float | **fraction** |
| `positions[].risk_rupees` | float | **rupees** to the **current resting stop**, floored at 0 |
| `positions[].opened` | ISO date | |
| `positions[].horizon` | | **always `"SWING"`** — hard-coded, not read |

```json
{"equity":500000.0,"cash":500000.0,"deployed":0,"n_positions":0,
 "heat_pct":0.0,"drawdown_pct":0.0,"watermark":500000.0,"positions":[]}
```

**`risk_rupees` uses the CURRENT resting stop, not the entry stop.** A stop trailed above entry has
locked in a gain and contributes zero heat; using the original would keep the book permanently hot
and block entries it should allow. Floored at zero so a stop above `last` contributes 0, not a
negative.

`ev.positions` is empty and has no writer — the paper broker does not exist. This endpoint will
return a flat ₹5,00,000 book on every call until it does.

---

### `/api/risk`

Every book-level capital-protection rule with its state chip and headroom bar. **The dashboard
displays these; it never enforces them.** Enforcement lives in `ta_ledger.risk.evaluate_entry` and
in the broker, so a dashboard that is down, stale or simply not being looked at cannot change what
the system is allowed to do.

| Field | Type | Meaning |
| --- | --- | --- |
| `regime` | `Regime` member | re-derived from `md.breadth_daily`; falls back to `UNKNOWN` |
| `rules[].id` | string | e.g. `RISK-04` |
| `rules[].label` | string | human name |
| `rules[].state` | `"OK" \| "WARN" \| "BREACH" \| "DISPLAY_ONLY"` | `WARN` at ≥85% of limit |
| `rules[].value` | float \| null | in `unit`. **`null` iff `DISPLAY_ONLY`** |
| `rules[].limit` | float \| null | in `unit` |
| `rules[].unit` | `"pct" \| "pct_floor" \| "count" \| "count_min" \| "beta"` | |
| `rules[].headroom_pct` | float \| null | **fraction**, `clamp(1 − value/limit, 0, 1)` |
| `rules[].detail` | string | always present, never empty |

Twelve rules are returned: `RISK-04` (portfolio heat), `RISK-05` (concurrent positions),
`RISK-05c` (cash floor), `RISK-05g` (gross exposure), `RISK-07` (distinct industries),
`RISK-18` (drawdown ladder), `RISK-14` (daily realised loss), `RISK-16` (rolling 5-session),
`RISK-17` (rolling 21-session), `RISK-20` (consecutive losses), `RISK-22` (expected portfolio
loss, `DISPLAY_ONLY`), `RISK-10` (factor exposure, `DISPLAY_ONLY`).

```json
{"as_of":"2026-10-30","regime":"TREND_DOWN","rules":[
 {"id":"RISK-04","label":"Portfolio heat","state":"OK","value":0.0,"limit":0.03,
  "unit":"pct","headroom_pct":1.0,"detail":"Rs 0 at risk to resting stops"},
 {"id":"RISK-05","label":"Concurrent positions","state":"OK","value":0.0,"limit":7.0,
  "unit":"count","headroom_pct":1.0,"detail":"backstop 9"},
 {"id":"RISK-18","label":"Peak-to-trough drawdown","state":"OK","value":0.0,"limit":0.25,
  "unit":"pct","headroom_pct":1.0,
  "detail":"watermark Rs 500,000; rungs 8%/12%/15%/18%, halt 25%"},
 {"id":"RISK-22","label":"Expected portfolio loss","state":"DISPLAY_ONLY","value":null,
  "limit":0.03,"unit":"pct",
  "detail":"needs calibrated_p with n_eff >= 30; not used for sizing yet"},
 {"id":"RISK-10","label":"Factor exposure","state":"DISPLAY_ONLY","value":null,
  "limit":0.35,"unit":"beta","detail":"USD/RATES/COMMODITY proxies; enforcing only from M10"}]}
```

**`DISPLAY_ONLY` must not render as a green tick.** Several rules are specified before the
statistics that would make them trustworthy exist. Showing such a rule as `OK` would imply an
assurance nobody has earned. `value` is `null` for exactly these rules, and
`test_risk_rules_expose_state_and_never_a_bare_unexplained_number` asserts both the enum membership
and the `DISPLAY_ONLY ⟹ value is null` implication, plus that `detail` is never empty.

Note `RISK-04`'s limit is **0.030, not 0.060** in this response: the base heat cap is 6.0% but the
`TREND_DOWN` regime tightens it to 3.0%, and the endpoint reports the **effective** cap,
`min(cfg.max_portfolio_heat_pct, regime_caps(regime)["heat"])`. Same for `RISK-05g`: 0.50, not 0.92.

`unit` semantics the UI must respect:
- `pct` — fraction; `value > limit` is bad.
- `pct_floor` — fraction, but it is a **floor**: `value` *below* `limit` is the breach
  (`RISK-05c` cash floor). `headroom_pct` is therefore meaningless for this row (it reads 0.0 at
  100% cash) and should not be drawn as a bar.
- `count` — raw count; `value > limit` is bad.
- `count_min` — raw count, **minimum**; `value < limit` is bad (`RISK-07`). `limit` is `null` below
  5 positions, and `detail` says "(not enforced below 5 positions)".
- `beta` — factor exposure, `DISPLAY_ONLY` only.

---

### `/api/industries`

Per-industry concentration against `RISK-06`. Iterates **open positions only**, so it returns
`rows: []` on an empty book.

| Field | Type | Unit |
| --- | --- | --- |
| `industry` | string | |
| `notional` | float | **rupees** held in this industry |
| `pct_equity` | float | **fraction** of equity |
| `n_names` | int | positions in this industry |
| `cap_pct` | float | **fraction**. 0.25, or **0.30 for `Financial Services`** |
| `headroom_rupees` | float | **rupees**. `equity × cap − notional` |
| `headroom_frac` | float \| null | **fraction** of the cap still free |
| `headroom` | float | **deprecated alias** for `headroom_rupees` |

```json
{"as_of":"2026-10-30","rows":[]}
```

Financial Services gets 30% / 3 names rather than 25% / 2, because it is roughly a quarter of the
Nifty 200 by count and a 2-name cap would make the book structurally unable to track the index's
largest sector. `test_risk_06_allows_a_third_financial_services_name` pins it.

The `headroom_rupees` / `headroom_frac` split is the fix for a real ambiguity — see
[Units](#the-other-suffixes).

---

### `/api/trend/{symbol}`

Per-symbol trend states plus up to 260 daily bars for the candle chart. 404 if the symbol has
neither a trend row nor bars.

| Field | Type | Unit |
| --- | --- | --- |
| `symbol` | string | sanitised, uppercased |
| `states.t20` / `.t63` / `.t252` | `TrendState` member | 8 members; `UNDEFINED ≠ RANGE` |
| `er_63` | float | Kaufman efficiency ratio, [0,1] |
| `adx14` | float | 0–100 |
| `slope_t_63` | float | **the Newey-West HAC t-stat** of mean daily log return over 63 sessions — *not* an OLS slope. The field name is misleading |
| `flips_63d` | int | `trend_63` state changes in 63 sessions; ≥6 excludes from trend-following sleeves |
| `rs_vs_bench_63` | | **always `null`** — the column is written as a literal 0.0 and no benchmark exists |
| `bars[]` | `{d, o, h, l, c, v}` | rupees except `v` (shares) and `d` (ISO date). Ascending |

```json
{"symbol":"RELIANCE",
 "states":{"t20":"RANGE","t63":"RANGE","t252":"RANGE"},
 "er_63":0.15105026960372925,"adx14":27.311336517333984,
 "slope_t_63":-1.1100506782531738,"flips_63d":0,"rs_vs_bench_63":null,
 "bars":[{"d":"2025-10-10","o":1377.8,"h":1388.0,"l":1375.1,"c":1381.7,"v":6373084},
         {"d":"2025-10-13","o":1376.9,"h":1377.7,"l":1367.8,"c":1375.0,"v":7600682}]}
```

Verified: 260 bars, 260 distinct dates (no duplicates today — but this handler does **not** dedupe
on read, so an unmerged `md.bars_1d` part would produce repeated candles).

**`UNDEFINED` is not `RANGE`.** Too little history, or a failed liquidity gate, means the trend was
never measured. Render it as "not enough history to say", never as "sideways" — the glossary entry
says exactly that.

**Bars are raw and unadjusted.** No corporate-action adjustment exists, so a split shows as a gap.

---

### `/api/calibration`

The refusal panel. *"Honest by construction: nothing has resolved yet, so there is nothing to
calibrate, and the endpoint says so rather than drawing an empty chart that implies calibration is
known and perfect."*

| Field | Type | Meaning |
| --- | --- | --- |
| `state` | `"OK" \| "INSUFFICIENT"` | `OK` iff `count(ev.outcomes) >= 30` |
| `n_eff` | float | resolved recommendation count |
| `needed` | int | 30 |
| `bins` | array | always `[]` today |
| `resolution`, `bss` | null | Murphy resolution and Brier Skill Score |
| `note` | string | states the shortfall in words |

```json
{"state":"INSUFFICIENT","n_eff":0.0,"needed":30,"bins":[],"resolution":null,"bss":null,
 "note":"calibration needs at least 30 resolved recommendations; 0 have resolved so far. Until then the system cannot say how well its stated probabilities match reality."}
```

**Calibration is the primary KPI of the whole project.** The system's job is not to be right, it is
to know how right it is. Render the shortfall, never an empty chart.

---

### `/api/simulation`

The second refusal panel: forward Monte Carlo.

| Field | Type | Meaning |
| --- | --- | --- |
| `status` | `"OK" \| "INSUFFICIENT"` | `OK` iff `n_days ≥ 750 ∧ n_trades ≥ 200` |
| `n_days` | int | `count(ev.equity_daily)` |
| `n_trades` | int | `count(ev.outcomes)` |
| `needed_days` / `needed_trades` | int | 750 / 200 |
| `p_lo`, `p_hi`, `block_len` | null | percentile band, block-bootstrap length |
| `histogram` | array | always `[]` today |
| `note` | string | |

```json
{"status":"INSUFFICIENT","n_days":0,"n_trades":0,"needed_days":750,"needed_trades":200,
 "p_lo":null,"p_hi":null,"block_len":null,"histogram":[],
 "note":"forward Monte Carlo needs 750 engine-days and 200 closed trades; have 0 and 0. A distribution fitted to this little history would be a picture of noise."}
```

`test_calibration_and_simulation_refuse_rather_than_guess` asserts that when either is
`INSUFFICIENT` its numeric payload is entirely `null` and its `note` is populated.

---

### `/api/glossary`

47 plain-English definitions, served from the backend so a tooltip cannot drift from the rule that
actually fires. The UI renders these verbatim and writes none of its own.

```jsonc
{"terms": {
  "WATCH": {
    "term": "WATCH",
    "short": "Ranked, but do not buy",
    "plain": "This looked good enough to rank, but the system is NOT telling you to buy it. The full plan is shown so you can see what the trade would be.",
    "why": "Either the edge is not proven yet, or a safety cap used up the room it needed. Quantity is zero on purpose.",
    "group": "action"
  }
}}
```

| Field | Meaning |
| --- | --- |
| `short` | 2–6 words; fits in a chip tooltip |
| `plain` | 1–2 sentences, no jargon, **no second acronym** |
| `why` | why it is evidence *for a trade* — the part a glossary usually omits and the part that actually helps. May be `""` |
| `group` | `action` (6) \| `reason` (14) \| `sleeve` (6) \| `trend` (8) \| `metric` (13) |

Two properties are machine-checked. Every `action`, `sleeve` and `reasons[]` code the API emits
must have an entry (*a chip the user cannot hover for a meaning is a wall of acronyms*), and no
entry's `plain` text may contain another `UPPER_SNAKE` code (*if an explanation needs a second
acronym explained, it is not plain yet*).

Served from an in-process constant — **0.3 ms p50**, the only endpoint that does not touch
ClickHouse.

---

## Error behaviour

| Condition | Response |
| --- | --- |
| ClickHouse unreachable | `503` with `"ClickHouse unavailable: <err>"` from `_ch()` |
| No market data ingested | `/api/context` → `503 "no market data ingested yet"` |
| Unknown symbol | `/api/trend/{symbol}` → `404 "no data for <SYM>"` |
| `ev.positions` / `ev.outcomes` / `ev.equity_daily` missing or erroring | swallowed; the endpoint degrades to a flat book / zero counts |
| Any service down | `/api/health` still returns `200` with `status: "degraded"` |

`test_an_unknown_symbol_404s_rather_than_500s` accepts `{404, 503}`.

CORS: `GET` only, from `http://127.0.0.1:5173` and `http://localhost:5173` (the Vite dev server).
Same-origin requests from the built SPA at `:8000` need no CORS.

There is **no authentication and no rate limiting**. The server binds `127.0.0.1` only. Do not
expose it.

---

## Known defects, collected

| Endpoint | Defect |
| --- | --- |
| `/api/heatmap` | Missing the dedupe-on-read used elsewhere. Whenever `md.sector_daily` has >1 live part, returns duplicate industry rows and silently drops industries because `LIMIT 40` truncates after the duplicates (observed: 40 rows / 14 distinct, 6 industries missing). Hidden again by a background merge. |
| `/api/heatmap` | `ret_5d` is actually `median_ret`, the **1-day** median. Mislabelled. |
| `/api/rejections` | Does not pin `run_id`; merges all six historical runs — 124 rows from 18 distinct facts, repeats up to 10×, growing every night. `MergeTree`, so no merge will ever hide this. |
| `/api/screener` | `close` is actually `entry_hi` = `close × 1.005`. |
| `/api/screener` | `rs_63` is actually `er_63`, the efficiency ratio — not relative strength. |
| `/api/screener` | `ret_1d`, `ret_21d`, `rvol_20`, `adtv_cr` are permanent `null` placeholders. |
| `/api/trend/{symbol}` | `slope_t_63` is a HAC t-stat of mean log return, not a slope. |
| `/api/trend/{symbol}` | Bars not deduped on read; an unmerged part would produce repeated candles. |
| `/api/context`, `/api/risk` | Read `md.breadth_daily` with `limit=1`, picking an arbitrary one of 3 identical unmerged rows. |
| all DB endpoints | ~38 ms per-request ClickHouse connection handshake; `/api/heatmap` p95 is 58 ms against a 30 ms gate while the query itself is 2.64 ms. |
| `barrier` | `BarrierEstimate.wide` (interval > 15 pp ⟹ suppress the point estimate) is computed server-side and **not serialised**. |
