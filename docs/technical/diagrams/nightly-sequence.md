# The nightly run

`ta daily --days 7`, fired by `com.tradingagent.nightly` at **18:45 local**. Four stages. Typical
incremental runtime **~10 s**.

```mermaid
sequenceDiagram
    autonumber
    participant LD as launchd<br/>18:45
    participant CLI as ta daily
    participant HTTP as PoliteClient
    participant RAW as data/raw/
    participant CH as ClickHouse
    participant PIPE as run_nightly
    participant RISK as evaluate_entry

    LD->>CLI: uv run --frozen ta daily --days 7

    rect rgba(45,122,148,0.14)
    Note over CLI,CH: [1/4] ingest — affects exit code
    loop each weekday in [end-7, end]
        CLI->>HTTP: fetch_session(d)
        alt body already cached
            HTTP->>RAW: read (bhavcopy never changes)
        else
            HTTP->>HTTP: token bucket — 1 req/s per host
            HTTP->>RAW: write bytes BEFORE parsing
        end
        alt HTTP 404
            HTTP-->>CLI: was_session=False
            Note right of HTTP: a holiday. NSE's own output<br/>is the trading calendar.
        else HTTP 200
            HTTP->>HTTP: parse_udiff_bhavcopy<br/>EQ+BE only, columns by NAME
            Note right of HTTP: knowledge_time = 18:30 IST on d
            HTTP->>CH: insert_arrow -> md.bars_1d
        end
    end
    end

    rect rgba(74,60,31,0.2)
    Note over CLI,CH: [2/4] symbol master — failure tolerated
    CLI->>HTTP: EQUITY_L.csv
    Note right of HTTP: headers ship with LEADING SPACES
    HTTP->>CH: ref.symbols
    end

    rect rgba(74,60,31,0.2)
    Note over CLI,CH: [3/4] index membership — failure tolerated
    loop NIFTY50/100/200/500
        CLI->>HTTP: reuse=False, archive=True
        Note right of HTTP: DATED snapshot, day-granular dir.<br/>Overwriting one reintroduces<br/>survivorship bias.
        HTTP->>CH: md.index_members
    end
    end

    rect rgba(45,122,148,0.14)
    Note over CLI,RISK: [4/4] screen + size + record — affects exit code
    CLI->>CH: latest_session()
    CLI->>CLI: as_of = now for a live run, else 18:30 IST on d
    CLI->>CH: query_pit md.bars_1d, 420 sessions
    CLI->>CH: query_pit md.index_members
    Note over CH: every read carries<br/>knowledge_time <= literal
    CLI->>CLI: dedupe (symbol,d) in polars
    Note right of CLI: ReplacingMergeTree merges on an<br/>UNSCHEDULED background pass.<br/>One dup shifts every rolling window.
    CLI->>CH: ev.positions WHERE status='OPEN'
    Note right of CH: empty -> flat book, Rs 5,00,000

    CLI->>PIPE: run_nightly(bars, members, portfolio, as_of)
    PIPE->>PIPE: add_price_factors — 20 factors, whole panel
    PIPE->>PIPE: add_trend_states — HAC t-stat, ER, ADX delta, hysteresis
    PIPE->>PIPE: sector_aggregates — the heat map, precomputed
    PIPE->>PIPE: breadth_row -> infer_regime
    PIPE->>PIPE: score_universe — composite, z-scored WITHIN industry
    PIPE->>PIPE: build_path_library ONCE<br/>k_up=2.0 k_dn=1.5 = the live geometry

    loop each candidate, ranked, until 25 cards
        PIPE->>PIPE: TRD-03 decaying trend? TRD-05 whipsaw?
        PIPE->>RISK: evaluate_entry(pf, cand, regime)
        Note right of RISK: pf is the GROWING book.<br/>Against a constant empty book<br/>no cap ever binds.
        alt rejected
            RISK-->>PIPE: Decision(REJECT, rule_id)
            PIPE->>PIPE: -> ev.rejections
            opt rule in RISK-04/05/06/08
                PIPE->>PIPE: keep as ranked alternate
            end
        else allowed
            RISK-->>PIPE: Decision(ALLOW|RESIZE, qty, notional)
            PIPE->>PIPE: compute_costs on the ACTUAL notional
            PIPE->>PIPE: estimate_barriers(atr_pct) — or UNAVAILABLE
            PIPE->>PIPE: E = 2.0*p_up - 1.5*p_dn - cost_atr
            alt E >= 0.05 ATR
                PIPE->>PIPE: BUY · qty>0 · book GROWS
            else
                PIPE->>PIPE: WATCH · qty=0 · would_qty kept<br/>labelled 'no demonstrated edge' + arithmetic
                Note right of PIPE: a WATCH card consumes no capital,<br/>so the book must NOT grow
            end
        end
    end

    PIPE-->>CLI: RunResult(run_id, 25 cards, 106 rejections)
    CLI->>CH: persist — each table in its own try
    Note over CH: md.sector_daily · md.breadth_daily<br/>md.trend_1d · ev.recommendations<br/>ev.rejections
    end

    CLI-->>LD: rc=0
```

## Stages and failure behaviour

| Stage | Writes | Failure |
| --- | --- | --- |
| `[1/4] ingest` | `md.bars_1d` | **non-zero exit** |
| `[2/4] symbol master` | `ref.symbols` | logged, run continues |
| `[3/4] index membership` | `md.index_members` | logged, run continues |
| `[4/4] screen + record` | 5 tables | **non-zero exit** |

Each stage is independent and a failure is reported rather than raised: *a missing constituents
refresh must not cost you the day's bars, and a screen that fails must not hide the fact that
ingestion succeeded.* The exit code tracks the screen because that is the stage whose silence
would otherwise look like "no trades today".

`persist` wraps each table write in its own `try` for the same reason — *a missing heat map is
better than a missing run.*

## What the live run actually produces

Session 2026-10-30, latest run `65bd044013ec`:

```
regime   TREND_DOWN  ("only 23% above 50-DMA")
breadth  200 names, 66 advancing, 134 declining, 23.5% above the 50-DMA
cards    25   (BUY=0, WATCH=25)
rejected 18 this run; ev.rejections holds 124 rows across 6 runs:
         TRD-05  WHIPSAW              108 rows / 12 distinct symbols
         RISK-01 BELOW_MIN_NOTIONAL    12 rows /  2 distinct symbols
         RISK-12 SURVEILLANCE           4 rows /  4 distinct symbols
                 (all series=BE; no ASM/GSM data is ingested, so
                  under_surveillance is always False)
barrier  p_up=0.2995  p_dn=0.4815  p_vert=0.2190  n_eff=25,755
best E   +0.0455 ATR  (CPPLUS, ACMESOLAR)  against a +0.05 bar
```

**Zero BUYs is the correct answer, not a failure.** The barrier estimate is the arm-invariant base
rate: the EDGE term that would differentiate a candidate is identically `0.0` until the system has
resolved ≥30 of its own trades, and the ledger that would resolve them does not exist. A base rate
against a 2.0 ATR target and a 1.5 ATR stop is ≈ zero expectancy before costs and negative after
them.

A hard rejection gate on that basis would reject 100% of candidates every night forever — *not a
risk control, a shutdown with extra steps*. So `RISK-P1` is applied as a **downgrade**: the card is
published with a full trade plan, labelled `no demonstrated edge`, with its arithmetic printed.

## Sleep, wake and idempotency

launchd **does not wake a sleeping Mac**. A `StartCalendarInterval` window that passes while
asleep fires at the **next wake**. That is safe here — bhavcopies are immutable, cached bodies are
reused, and `md.bars_1d` is keyed on `(symbol, d)` — but it is not "ran at 18:45".

To wake the machine (needs sudo, so `install_launchd.sh` prints it rather than running it):

```bash
sudo pmset repeat wakeorpoweron MTWRF 18:40:00
```

**Verified not currently set** — `pmset -g sched` shows only macOS-internal alarms.

## What PLAN.md describes and this is not

PLAN.md §Daily schedule specifies **eleven** Prefect-orchestrated stages from 15:45 to 22:35 IST:
`eod_mark`, `ingest_archive`, `validate_eod`, `build_features`, `evaluate_and_learn`, `nlp_enrich`,
`screen_and_dossier`, `committee`, `red_team`, `publish`, `maintain`. None of that exists. Prefect
is not installed. There is no freshness gate, no feature persistence, no evaluation stage, no
committee and no maintenance stage. The four stages above are the system.
