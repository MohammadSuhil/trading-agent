/**
 * The API contract, transcribed field-for-field from the spec.
 *
 * Nullability note — this is deliberate and it is the whole reason the UI does
 * not crash. The spec declares `ret_1d: number`, but the API is young and the
 * pipeline behind it degrades (`data_quality: "PARTIAL"`, `barrier.state:
 * "UNAVAILABLE"`, a benchmark TRI that had to be reconstructed). A field that
 * the server *intends* to always send is still absent on the wire the day a
 * join misses. So every scalar is widened to `| null` and every collection to
 * `| null | undefined`: that makes TypeScript force an optional-chain or a
 * fallback at each use site instead of letting a `.toFixed()` on undefined take
 * the whole page down. Field NAMES and the string enums are exact.
 */

/** A number the server may not have. */
export type N = number | null;
/** A string the server may not have. */
export type S = string | null;
/** A list the server may not have. */
export type List<T> = T[] | null | undefined;

export type Health = {
  status?: 'ok' | 'degraded' | null;
  as_of?: S;
  data_quality?: 'OK' | 'PARTIAL' | string | null;
  services?: {
    clickhouse?: boolean | null;
    mongo?: boolean | null;
    redis?: boolean | null;
  } | null;
};

export type Regime = 'TREND_UP' | 'CHOP' | 'TREND_DOWN' | 'HIGH_VOL' | 'UNKNOWN';

export type Context = {
  as_of?: S;
  regime?: Regime | string | null;
  regime_note?: S;
  breadth?: {
    advancers?: N;
    decliners?: N;
    pct_above_sma50?: N;
    universe?: S;
  } | null;
  benchmark?: {
    name?: S;
    close?: N;
    ret_1d?: N;
    ret_21d?: N;
    tri_source?: 'PUBLISHED' | 'RECONSTRUCTED' | string | null;
  } | null;
  data_quality?: S;
};

export type HeatmapRow = {
  industry?: S;
  n?: N;
  ret_1d?: N;
  ret_5d?: N;
  ret_21d?: N;
  turnover_cr?: N;
  breadth_pct?: N;
};

export type Heatmap = { as_of?: S; rows?: List<HeatmapRow> };

export type ScreenerRow = {
  symbol?: S;
  name?: S;
  industry?: S;
  close?: N;
  ret_1d?: N;
  ret_21d?: N;
  rvol_20?: N;
  adtv_cr?: N;
  trend_state?: S;
  rs_63?: N;
  adx14?: N;
  composite?: N;
  sleeve?: S;
  rr?: N;
  expectancy_atr?: N;
  action?: string | null;
  reasons?: List<string>;
};

export type Screener = { as_of?: S; rows?: List<ScreenerRow> };

/* ------------------------------------------------------------- glossary */

/**
 * `GET /api/glossary`.
 *
 * The single source of truth for what every code on this dashboard means. The
 * UI writes no definition of its own — if a tooltip said something the server
 * did not, the explanation and the rule that fired could drift apart, and the
 * reader would have no way to tell which one was lying.
 *
 * `short` is a 2–6 word headline for a chip tooltip; `plain` is one or two
 * jargon-free sentences; `why` (sometimes empty) is why it counts as evidence.
 */
export type GlossaryTerm = {
  term?: S;
  short?: S;
  plain?: S;
  why?: S;
  group?: 'action' | 'reason' | 'sleeve' | 'trend' | 'regime' | 'horizon' | 'metric' | 'risk' | 'audit' | string | null;
};

export type Glossary = { terms?: Record<string, GlossaryTerm> | null };

/**
 * The barrier block. `state: "UNAVAILABLE"` is a first-class answer, not an
 * error — see components/BarrierBlock.tsx for the rendering rules, which are
 * the product requirement rather than a styling choice.
 */
export type Barrier = {
  state?: 'OK' | 'UNAVAILABLE' | string | null;
  p_up?: N;
  p_dn?: N;
  p_vert?: N;
  ci_lo?: N;
  ci_hi?: N;
  n_eff?: N;
  /**
   * The server's own verdict on whether the interval is too wide to quote a
   * midpoint from. It is authoritative: the UI used to apply the 15pp rule
   * itself, which meant two copies of one threshold that could drift apart.
   * Absent → fall back to the documented 15pp test rather than showing a
   * midpoint the server might have withheld.
   */
  wide?: boolean | null;
  reason?: S;
};

/** The trend classifier's closed vocabulary — see components/trend.tsx. */
export type TrendState =
  | 'UP_STRONG'
  | 'UP'
  | 'UP_DECAYING'
  | 'RANGE'
  | 'DOWN_DECAYING'
  | 'DOWN'
  | 'DOWN_STRONG'
  | 'UNDEFINED';

/**
 * Six members, and the split down the middle is the product.
 *
 * BUY / WATCH / AVOID are things the screener can say about a name you do NOT
 * own. HOLD / TRIM / SELL are things only a position can be in. The backend
 * used to return HOLD for a screened candidate, which told the reader they
 * owned something they had never bought; that is fixed at the source, and the
 * UI's job now is to never let the two families look alike.
 */
export type Action = 'BUY' | 'WATCH' | 'HOLD' | 'TRIM' | 'SELL' | 'AVOID';

export type RecommendationRow = {
  id?: S;
  symbol?: S;
  name?: S;
  industry?: S;
  action?: Action | string | null;
  sleeve?: S;
  horizon?: S;
  entry_lo?: N;
  entry_hi?: N;
  stop?: N;
  target?: N;
  /** The size you are actually being ADVISED to take now. Zero on a WATCH. */
  qty?: N;
  notional?: N;
  risk_rupees?: N;
  risk_pct?: N;
  /**
   * Affordability, not advice: the MOST you could put into this name today
   * given risk appetite, cash on hand and the headroom left in its industry.
   * Computed by the backend against the real portfolio, so it is stable per
   * name and does not shrink because something else ranked above it.
   *
   * It is not an instruction and must never be rendered as one — subordinate
   * type, under a label that says what it is.
   */
  would_qty?: N;
  would_notional?: N;
  would_risk_rupees?: N;
  /** The server's own one-line account of that figure, or the rule that capped it. */
  afford_note?: S;
  rr?: N;
  reasons?: List<string>;
  cost_bps?: N;
  expectancy_atr?: N;
  composite?: N;
  /**
   * The stock's own trend state at three lookbacks, and its INDUSTRY's state
   * from the same classifier at the same lookbacks — so the two are directly
   * comparable. The comparison is the signal: sideways inside a strong industry
   * is a different trade from sideways inside a decaying one.
   */
  trend_20?: TrendState | string | null;
  trend_63?: TrendState | string | null;
  trend_252?: TrendState | string | null;
  ind_trend_20?: TrendState | string | null;
  ind_trend_63?: TrendState | string | null;
  ind_trend_252?: TrendState | string | null;
  barrier?: Barrier | null;
};

export type Recommendations = { as_of?: S; rows?: List<RecommendationRow> };

export type RejectionRow = {
  symbol?: S;
  rule_id?: S;
  reason?: S;
  detail?: S;
};

export type Rejections = { as_of?: S; rows?: List<RejectionRow> };

export type PositionRow = {
  symbol?: S;
  industry?: S;
  qty?: N;
  entry?: N;
  last?: N;
  stop?: N;
  target?: N;
  pnl?: N;
  pnl_pct?: N;
  risk_rupees?: N;
  opened?: S;
  horizon?: S;
};

export type EquityPoint = {
  d?: S;
  arm?: S;
  equity?: N;
  cash?: N;
  deployed?: N;
  heat_pct?: N;
  drawdown_pct?: N;
};

export type BenchmarkPoint = {
  d?: S;
  index_id?: S;
  series_kind?: S;
  close?: N;
  ret_1d?: N;
  tri_source?: S;
};

export type TradeEvent = {
  seq?: N;
  ts?: S;
  d?: S;
  symbol?: S;
  kind?: S;
  side?: S;
  qty?: N;
  price?: N;
  notional?: N;
  cost_rupees?: N;
  pnl_rupees?: N;
  note?: S;
};

export type Portfolio = {
  equity?: N;
  cash?: N;
  deployed?: N;
  positions?: List<PositionRow>;
  heat_pct?: N;
  drawdown_pct?: N;
  watermark?: N;
  n_positions?: N;
  equity_curve?: List<EquityPoint>;
  benchmark_curve?: List<BenchmarkPoint>;
  trades?: List<TradeEvent>;
};

export type Backtest = {
  state?: S;
  arm?: S;
  weights_version?: S;
  weights_verified?: boolean | null;
  weights_sha256?: S;
  ledger_verified?: boolean | null;
  stats_state?: 'OK' | 'INSUFFICIENT' | string | null;
  n_sessions?: N;
  needed_sessions?: N;
  final_equity?: N;
  total_return_pct?: N;
  sharpe?: N;
  n_trades?: N;
  win_rate?: N;
  psr?: N;
  dsr?: N;
  min_trl?: N;
  note?: S;
  max_drawdown?: N;
  equity_curve?: List<EquityPoint>;
  benchmark_curve?: List<BenchmarkPoint>;
  comparison?: BacktestComparison | null;
  /** Window (in sessions) this result belongs to. */
  sessions?: N;
  /** The stored run being shown; null when the window has never been run. */
  run?: { run_id?: S; run_ts?: S; start_d?: S; end_d?: S } | null;
  /** Every window that has a stored run, for the selector. */
  windows?: List<BacktestWindow>;
  /** ADR-0005: the index this run is judged against, over the run's own dates. */
  benchmark?: {
    index?: S;
    series?: S;
    return_pct?: N;
    excess_return_pct?: N;
    beats_benchmark?: boolean | null;
  } | null;
};

export type BacktestWindow = {
  sessions?: N;
  last_run?: S;
  start_d?: S;
  end_d?: S;
  total_return_pct?: N;
};

/** A report-only rule variant backtested over the control's window (ADR-0004). */
export type BacktestComparison = {
  variant?: S;
  same_window?: boolean | null;
  start_d?: S;
  end_d?: S;
  n_sessions?: N;
  n_trades?: N;
  final_equity?: N;
  total_return_pct?: N;
  sharpe?: N;
  max_drawdown?: N;
  win_rate?: N;
  excess_return_pct?: N;
  curve?: List<{ d?: S; equity?: N; regime?: S; index_regime?: S }>;
};

export type RuleState = 'OK' | 'WARN' | 'BREACH' | 'DISPLAY_ONLY';

export type RiskRule = {
  id?: S;
  label?: S;
  state?: RuleState | string | null;
  value?: N;
  limit?: N;
  unit?: S;
  headroom_pct?: N;
  detail?: S;
};

export type Risk = { as_of?: S; rules?: List<RiskRule> };

export type IndustryRow = {
  industry?: S;
  notional?: N;
  pct_equity?: N;
  n_names?: N;
  cap_pct?: N;
  headroom?: N;
};

export type Industries = { as_of?: S; rows?: List<IndustryRow> };

export type Bar = { d?: S; o?: N; h?: N; l?: N; c?: N; v?: N };

export type Trend = {
  symbol?: S;
  states?: { t20?: S; t63?: S; t252?: S } | null;
  er_63?: N;
  adx14?: N;
  slope_t_63?: N;
  flips_63d?: N;
  rs_vs_bench_63?: N;
  bars?: List<Bar>;
};

export type CalibrationBin = { p_stated?: N; p_realised?: N; n?: N };

export type Calibration = {
  state?: 'INSUFFICIENT' | 'NOT_BUILT' | 'OK' | string | null;
  n_eff?: N;
  n_raw?: N;
  needed?: N;
  bins?: List<CalibrationBin>;
  resolution?: N;
  bss?: N;
  note?: S;
};

export type SimBucket = { bucket?: N; count?: N };

export type Simulation = {
  status?: 'INSUFFICIENT' | 'OK' | string | null;
  n_days?: N;
  n_trades?: N;
  needed_days?: N;
  needed_trades?: N;
  p_lo?: N;
  p_hi?: N;
  block_len?: N;
  note?: S;
  histogram?: List<SimBucket>;
};
