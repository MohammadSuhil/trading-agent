# ta_quant — orientation

Bars in, ranked trade plans out. **Zero LLM calls.** Every number on a card is
computed here; the LLM layer (M5) only selects among these and explains them.

Signatures: [docs/CODEMAP.md](../../docs/CODEMAP.md) § packages/quant.

| Module | Owns | Notes |
| --- | --- | --- |
| `pipeline.py` | `run_nightly`, `build_panel`, `score_universe`, `infer_regime`, `sector_aggregates` | **Pure** (frames in, frames out). Order: factors+trend → sector aggregates → hard gates → composite → barriers → risk gates + sizing → record |
| `nightly.py` | `execute_nightly`, `load_panel_inputs`, `persist`, `as_of_for` | All DB I/O for the run. Dedupes bars on `(symbol, d)`. `LOOKBACK_SESSIONS = 420`. `current_portfolio()` is empty until the M2 ledger exists |
| `factors/price.py` | `add_price_factors` | ~20 factors (target ~70). Polars `over("symbol")`, strictly trailing windows — a centred window would leak |
| `trend.py` | `add_trend_states`, `nw_lag` | Drift = HAC (Newey-West) t-stat of mean log return, not OLS slope |
| `regime.py` | `regime_series`, `regime_on`, `classify`, `apply_hysteresis` | Index rules on **Nifty 500** (ADR-0004): BEAR / HIGH_VOL / BULL / SIDEWAYS → TREND_DOWN / HIGH_VOL / TREND_UP / CHOP; RISK-26 hysteresis (tighten now, loosen after 3). **Informational only** — ADR-0004 rejected it as a gate (lost to control OOS); `nightly.REG01_ENABLED = False`. Gating stays on `pipeline.infer_regime` (breadth) |
| `barriers.py` | `build_path_library`, `estimate_barriers`, `BarrierModel`, `BarrierEstimate`, `known_paths` | `known_paths` = the one PIT cut (`exit_d <= d`, last 252 exit sessions) shared by live and backtest (ADR-0003). **Build one `BarrierModel` per session** and call `.estimate(atr_pct)` per candidate — per-candidate `estimate_barriers` was 92% of backtest time; a test caps cells computed per session |
| `barriers.py` (cont.) | `build_path_library`, `estimate_barriers`, `BarrierEstimate` | Empirical, one Dirichlet over target/stop/time; pessimistic same-bar → DOWN; refuses when thin |

**Reads:** `md.bars_1d`, `md.index_members`, `md.index_1d` (Nifty 500 OHLC, via `query_pit`), `ev.positions`
(open book). **Writes:** `md.sector_daily`, `md.trend_1d`, `md.breadth_daily`, `ev.recommendations`,
`ev.rejections`, `md.regime_daily` (nightly, non-replay: the informational index regime), `ev.backtest_runs`
(every control or regime-filter backtest, one row per line, keyed by window — stats + simulation + curve,
I-29), `ev.equity_daily` / `ev.outcomes` (latest FROZEN_CONTROL backtest only — regime-filter and
DETERMINISTIC backtests never write them, I-28). `ev.simulation` / `ev.backtest_variants` are deprecated.

**Replay caveat:** `md.index_members` holds dated snapshots from 2026-10-31 on, so a PIT replay of an
earlier session sees no membership and yields no cards. Backtests use the current snapshot for all
dates instead (survivorship — see M3 known traps).

**Empty placeholders:** `backtest/` (M3), `ml/` (M10), `screener/`.

**Known state:** the per-candidate EDGE term is 0.0 until ≥ 30 own trades resolve,
so tonight's cards are honestly "no demonstrated edge". `infer_regime` is a
four-threshold stand-in for the M10 HMM.

**Tests:** `tests/test_market_regime.py`, `tests/test_action_semantics.py`. **No
golden vectors for factors or trend yet** — the biggest hole in the suite (M2).

**Before changing:**
- Barrier geometry must match the trade (stop at −1.5 ATR ⇒ measure P(touch −1.5 ATR)).
- Gate candidates sequentially against a growing book, never the starting one.
- Corporate actions are not adjusted yet: any return/MA/ATR crossing a split is wrong.
