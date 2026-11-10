-- Trading Agent — schema additions for M1/M2: trend states, the nightly
-- recommendation run, its rejections, breadth, and the hash-chained ledger.
--
-- Everything with a knowledge_time is registered in ta_core.asof.PIT_TABLES,
-- so reads of these tables are build-failing unless they carry an as-of bound.

-- ------------------------------------------------------------- trend states
CREATE TABLE IF NOT EXISTS md.trend_1d (
    d                   Date,
    symbol              LowCardinality(String),
    trend_20            LowCardinality(String),
    trend_63            LowCardinality(String),
    trend_252           LowCardinality(String),
    t_drift_20          Float32,
    t_drift_63          Float32,
    t_drift_252         Float32,
    er_20               Float32,
    er_63               Float32,
    er_252              Float32,
    adx14               Float32,
    adx_delta_10        Float32,
    flips_63d           UInt16,
    rs_vs_bench_63      Float32,
    trend_enum_version  UInt16,
    knowledge_time      DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(d)
ORDER BY (symbol, d);

-- ----------------------------------------------------------------- breadth
-- Universe is a KEY column, not an attribute: breadth over the Nifty 200 and
-- breadth over the whole market are different facts and must never be mixed.
CREATE TABLE IF NOT EXISTS md.breadth_daily (
    d                 Date,
    universe          LowCardinality(String),
    n_symbols         UInt16,
    advancers         UInt16,
    decliners         UInt16,
    pct_above_sma50   Float32,
    pct_above_sma200  Float32,
    ret_dispersion_xs Float32,
    median_ret        Float32,
    knowledge_time    DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree()
ORDER BY (d, universe);

-- -------------------------------------------------------------- benchmark
CREATE TABLE IF NOT EXISTS md.benchmark_1d (
    d               Date,
    index_id        LowCardinality(String),
    series_kind     LowCardinality(String),   -- TRI | PRICE
    close           Float64,
    ret_1d          Float32,
    tri_source      LowCardinality(String),   -- PUBLISHED | RECONSTRUCTED
    knowledge_time  DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree()
ORDER BY (index_id, series_kind, d);

-- ------------------------------------------------------- recommendations
-- One row per candidate that survived every gate. The trade plan is stored
-- whole — entry band, stop, target, size — because a recommendation whose
-- numbers are recomputed on read is not auditable.
CREATE TABLE IF NOT EXISTS ev.recommendations (
    as_of           DateTime64(3, 'UTC'),
    run_id          String,
    rec_id          String,
    d               Date,
    symbol          LowCardinality(String),
    name            String,
    industry        LowCardinality(String),
    arm             LowCardinality(String),
    action          LowCardinality(String),
    sleeve          LowCardinality(String),
    horizon         LowCardinality(String),
    entry_lo        Float64,
    entry_hi        Float64,
    stop            Float64,
    target          Float64,
    -- ACTUAL size being advised. Zero on a WATCH card: you are not being
    -- told to buy it, and a card that shows both "do not buy" and a share
    -- count is the exact ambiguity this split exists to remove.
    qty             UInt32,
    notional        Float64,
    -- What the size WOULD have been had it been actionable. Kept so a WATCH
    -- card can still show the shape of the trade, in muted type, without
    -- implying an instruction.
    -- The most you could put into this name TODAY given your real portfolio:
    -- risk appetite, cash on hand, and the headroom left in its industry.
    -- Unlike `qty` this does not shrink because something else ranked above
    -- it, so "how much of this could I buy?" has a stable answer.
    would_qty          UInt32 DEFAULT 0,
    would_notional     Float64 DEFAULT 0,
    would_risk_rupees  Float64 DEFAULT 0,
    afford_note        String DEFAULT '',
    risk_rupees     Float64,
    risk_pct        Float32,
    rr              Float32,
    atr14           Float64,
    cost_bps        Float32,
    expectancy_atr  Float32,
    composite       Float32,
    reasons         Array(String),
    trend_20        LowCardinality(String) DEFAULT 'UNDEFINED',
    trend_63        LowCardinality(String) DEFAULT 'UNDEFINED',
    trend_252       LowCardinality(String) DEFAULT 'UNDEFINED',
    ind_trend_20    LowCardinality(String) DEFAULT 'UNDEFINED',
    ind_trend_63    LowCardinality(String) DEFAULT 'UNDEFINED',
    ind_trend_252   LowCardinality(String) DEFAULT 'UNDEFINED',
    barrier_wide    UInt8 DEFAULT 0,
    -- barrier triad, stored as RECORDED INPUTS so a replay cannot recompute
    barrier_state   LowCardinality(String),
    p_up            Nullable(Float32),
    p_dn            Nullable(Float32),
    p_vert          Nullable(Float32),
    ci_lo           Nullable(Float32),
    ci_hi           Nullable(Float32),
    n_eff           Float32,
    barrier_reason  String,
    knowledge_time  DateTime64(3, 'UTC')
)
-- MergeTree, NOT Replacing, and run_id is IN THE SORT KEY.
-- With ORDER BY (d, arm, symbol) under ReplacingMergeTree, re-running the
-- nightly job REPLACES the previous run's row for the same symbol instead of
-- appending a new one. The history of what the system recommended then is
-- destroyed by the act of recommending again -- which is precisely the
-- audit property this table exists to provide.
ENGINE = MergeTree()
PARTITION BY toYYYYMM(d)
ORDER BY (d, arm, run_id, symbol);

-- ------------------------------------------------------------- rejections
-- Every rejected candidate with the rule that killed it. This exists so the
-- gates are auditable rather than invisible: "the cap, not the signal,
-- rejected this" is a thing the user must be able to see.
CREATE TABLE IF NOT EXISTS ev.rejections (
    as_of    DateTime64(3, 'UTC'),
    run_id   String,
    d        Date,
    symbol   LowCardinality(String),
    rule_id  LowCardinality(String),
    reason   LowCardinality(String),
    detail   String
)
-- Same reasoning: a rejection is a historical fact about one run.
ENGINE = MergeTree()
PARTITION BY toYYYYMM(d)
ORDER BY (d, run_id, symbol, rule_id);

-- ---------------------------------------------------------------- ledger
-- Append-only, hash-chained. `prev_hash` is the previous row's `hash`, so any
-- edit to history invalidates every row after it. Canonical JSON, never
-- pickle: a record that must load in 2027 cannot depend on today's classes.
CREATE TABLE IF NOT EXISTS ev.ledger_events (
    seq        UInt64,
    ts         DateTime64(3, 'UTC'),
    run_id     String,
    kind       LowCardinality(String),
    payload    String,
    prev_hash  String,
    hash       String
) ENGINE = ReplacingMergeTree()
ORDER BY seq;

-- -------------------------------------------------------------- positions
CREATE TABLE IF NOT EXISTS ev.positions (
    as_of       DateTime64(3, 'UTC'),
    symbol      LowCardinality(String),
    industry    LowCardinality(String),
    qty         UInt32,
    entry       Float64,
    last        Float64,
    stop        Float64,
    target      Float64,
    opened      Date,
    horizon     LowCardinality(String),
    sleeve      LowCardinality(String),
    status      LowCardinality(String),
    pnl         Float64
) ENGINE = ReplacingMergeTree(as_of)
ORDER BY (symbol, opened);

-- --------------------------------------------------------- equity curve
CREATE TABLE IF NOT EXISTS ev.equity_daily (
    d          Date,
    arm        LowCardinality(String),
    equity     Float64,
    cash       Float64,
    deployed   Float64,
    heat_pct   Float32,
    drawdown   Float32,
    watermark  Float64
) ENGINE = ReplacingMergeTree()
ORDER BY (arm, d);
