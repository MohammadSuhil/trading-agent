-- Trading Agent — ClickHouse schema.
-- Every point-in-time table carries knowledge_time: the instant the row became
-- knowable. Reads go through ta_core.asof.build_pit_query, which injects the
-- bound. Without this, backtests silently use data that did not exist yet.

CREATE DATABASE IF NOT EXISTS md;   -- market data
CREATE DATABASE IF NOT EXISTS nx;   -- news / events
CREATE DATABASE IF NOT EXISTS fn;   -- fundamentals
CREATE DATABASE IF NOT EXISTS ev;   -- evaluation / learning
CREATE DATABASE IF NOT EXISTS ref;  -- reference data (no knowledge_time)

-- ---------------------------------------------------------------- reference
CREATE TABLE IF NOT EXISTS ref.symbols (
    symbol      LowCardinality(String),
    isin        String,
    name        String,
    series      LowCardinality(String),
    listed_date Date DEFAULT toDate('1990-01-01'),
    updated_at  DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at) ORDER BY symbol;

-- ------------------------------------------------------------- daily bars
-- Raw, UNADJUSTED prices. Adjustment factors live separately so a late-arriving
-- corporate action never silently rewrites history we already trained on.
CREATE TABLE IF NOT EXISTS md.bars_1d (
    d               Date,
    symbol          LowCardinality(String),
    isin            String,
    series          LowCardinality(String),
    open            Float64 CODEC(Delta, ZSTD(3)),
    high            Float64 CODEC(Delta, ZSTD(3)),
    low             Float64 CODEC(Delta, ZSTD(3)),
    close           Float64 CODEC(Delta, ZSTD(3)),
    prev_close      Float64 CODEC(Delta, ZSTD(3)),
    last_price      Float64 CODEC(Delta, ZSTD(3)),
    volume          UInt64  CODEC(T64, ZSTD(3)),
    turnover        Float64 CODEC(Gorilla, ZSTD(3)),
    n_trades        UInt32  CODEC(T64, ZSTD(3)),
    deliv_qty       UInt64  DEFAULT 0 CODEC(T64, ZSTD(3)),
    deliv_pct       Float32 DEFAULT 0,
    knowledge_time  DateTime64(3, 'UTC'),
    source          LowCardinality(String) DEFAULT 'nse_udiff',
    ingested_at     DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(d)
ORDER BY (symbol, d)
SETTINGS index_granularity = 8192;

-- ------------------------------------------------------- minute bars (M10)
CREATE TABLE IF NOT EXISTS md.bars_1m (
    ts              DateTime64(0, 'Asia/Kolkata'),
    symbol          LowCardinality(String),
    open            Float64 CODEC(Delta, ZSTD(3)),
    high            Float64 CODEC(Delta, ZSTD(3)),
    low             Float64 CODEC(Delta, ZSTD(3)),
    close           Float64 CODEC(Delta, ZSTD(3)),
    volume          UInt64  CODEC(T64, ZSTD(3)),
    knowledge_time  DateTime64(3, 'UTC'),
    source          LowCardinality(String) DEFAULT 'broker'
) ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(ts)
ORDER BY (symbol, ts);

-- --------------------------------------------- index membership (SNAPSHOTS)
-- Dated snapshots, never "current membership". Applying today's Nifty 200 to a
-- 2024 backtest is survivorship bias and inflates returns badly.
CREATE TABLE IF NOT EXISTS md.index_members (
    snapshot_date   Date,
    index_name      LowCardinality(String),
    symbol          LowCardinality(String),
    isin            String,
    company_name    String,
    industry        LowCardinality(String),
    knowledge_time  DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree()
PARTITION BY toYear(snapshot_date)
ORDER BY (index_name, snapshot_date, symbol);

-- ------------------------------------------------------- corporate actions
CREATE TABLE IF NOT EXISTS md.corporate_actions (
    symbol          LowCardinality(String),
    ex_date         Date,
    action_type     LowCardinality(String),   -- SPLIT | BONUS | DIVIDEND | RIGHTS
    ratio_from      Float64 DEFAULT 1,
    ratio_to        Float64 DEFAULT 1,
    dividend        Float64 DEFAULT 0,
    adj_factor      Float64 DEFAULT 1,
    announce_date   Date,
    knowledge_time  DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree()
ORDER BY (symbol, ex_date, action_type);

-- -------------------------------------------------- surveillance exclusions
CREATE TABLE IF NOT EXISTS md.surveillance (
    d               Date,
    symbol          LowCardinality(String),
    list_type       LowCardinality(String),   -- ASM | GSM | ESM | T2T | BAN
    stage           String DEFAULT '',
    knowledge_time  DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(d)
ORDER BY (d, symbol, list_type);

-- ------------------------------------------------------------ features
CREATE TABLE IF NOT EXISTS md.features_1d (
    d                   Date,
    symbol              LowCardinality(String),
    industry            LowCardinality(String),
    close               Float64,
    ret_1d              Float32, ret_5d Float32, ret_21d Float32,
    ret_63d             Float32, ret_126d Float32, ret_252d Float32,
    mom_12_1            Float32,
    sma20 Float32, sma50 Float32, sma200 Float32,
    ma_stack            Int8,
    atr14               Float32,
    realised_vol_21d    Float32,
    rsi14               Float32,
    adx14               Float32,
    bb_z                Float32,
    dist_52w_high       Float32,
    dist_52w_low        Float32,
    rvol_20             Float32,
    adtv_20             Float64,
    turnover            Float64,
    deliv_pct           Float32,
    rs_vs_nifty_21d     Float32,
    rs_vs_sector_21d    Float32,
    liquidity_ok        UInt8,
    knowledge_time      DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(d)
ORDER BY (symbol, d);

-- ----------------------------------------------- sector daily (heat map)
-- Pre-aggregated so /api/heatmap is ONE select, not a 500-stock computation
-- per page load. The M1 gate is p95 < 30 ms.
CREATE TABLE IF NOT EXISTS md.sector_daily (
    d                 Date,
    industry          LowCardinality(String),
    n_symbols         UInt16,
    turnover          Float64,
    cap_weighted_ret  Float32,
    median_ret        Float32,
    breadth_pct       Float32,
    adv_count         UInt16,
    dec_count         UInt16,
    move_z            Float32,
    rs_21d            Float32,
    knowledge_time    DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree()
PARTITION BY toYear(d)
ORDER BY (d, industry);

-- ------------------------------------------------------------ news
CREATE TABLE IF NOT EXISTS nx.news_features (
    ts              DateTime64(3, 'UTC'),   -- DISSEMINATION time, never crawl time
    symbol          LowCardinality(String),
    doc_id          String,
    source          LowCardinality(String),
    source_tier     UInt8,
    event_type      LowCardinality(String),
    sentiment       Float32,
    relevance       Float32,
    novelty         Float32,
    is_duplicate    UInt8,
    headline        String,
    url             String,
    latency_sec     Int32,
    knowledge_time  DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(ts)
ORDER BY (symbol, ts, doc_id);

-- ------------------------------------------------------- fundamentals
CREATE TABLE IF NOT EXISTS fn.statements (
    symbol          LowCardinality(String),
    period_end      Date,
    period_type     LowCardinality(String),  -- Q | H | A
    consolidated    UInt8,
    revenue         Float64, other_income Float64, total_income Float64,
    ebitda          Float64, depreciation Float64, interest Float64,
    pbt             Float64, tax Float64, pat Float64, eps Float64,
    equity          Float64, reserves Float64, borrowings Float64,
    total_assets    Float64, cfo Float64, capex Float64,
    filed_at        DateTime64(3, 'UTC'),    -- when it became PUBLIC
    knowledge_time  DateTime64(3, 'UTC'),
    source          LowCardinality(String)
) ENGINE = ReplacingMergeTree()
ORDER BY (symbol, period_end, consolidated);

CREATE TABLE IF NOT EXISTS fn.shareholding (
    symbol            LowCardinality(String),
    period_end        Date,
    promoter_pct      Float32,
    promoter_pledge   Float32,
    fii_pct           Float32,
    dii_pct           Float32,
    public_pct        Float32,
    filed_at          DateTime64(3, 'UTC'),
    knowledge_time    DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree() ORDER BY (symbol, period_end);

-- ------------------------------------------------- evaluation / learning
CREATE TABLE IF NOT EXISTS ev.outcomes (
    rec_id          String,
    symbol          LowCardinality(String),
    arm             LowCardinality(String),
    horizon         LowCardinality(String),
    entry_date      Date,
    entry_price     Float64,
    exit_date       Date,
    exit_price      Float64,
    barrier_hit     LowCardinality(String),
    r_multiple      Float32,
    pnl_rupees      Float64,
    mae_r           Float32,
    mfe_r           Float32,
    holding_days    UInt16,
    benchmark_ret   Float32,
    stated_conf     Float32,
    calibrated_p    Float32,
    uniqueness_w    Float32,
    regime          LowCardinality(String),
    degraded_mode   UInt8 DEFAULT 0,
    knowledge_time  DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree() ORDER BY (rec_id);

CREATE TABLE IF NOT EXISTS ev.attribution (
    rec_id          String,
    reason_code     LowCardinality(String),
    arm             LowCardinality(String),
    industry        LowCardinality(String),
    regime          LowCardinality(String),
    sleeve          LowCardinality(String),
    horizon         LowCardinality(String),
    r_multiple      Float32,
    weight          Float32,
    resolved_date   Date,
    knowledge_time  DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree() ORDER BY (reason_code, resolved_date, rec_id);

CREATE TABLE IF NOT EXISTS ev.posteriors (
    as_of           Date,
    reason_code     LowCardinality(String),
    regime          LowCardinality(String),
    horizon         LowCardinality(String),
    alpha           Float64,
    beta            Float64,
    p_hat           Float32,
    n_eff           Float32,
    gated           UInt8,
    expectancy_r    Float32,
    knowledge_time  DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree() ORDER BY (as_of, reason_code, regime, horizon);
