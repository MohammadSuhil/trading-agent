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
| `barriers.py` | `build_path_library`, `estimate_barriers`, `BarrierEstimate` | Empirical, one Dirichlet over target/stop/time; pessimistic same-bar → DOWN; refuses when thin |

**Reads:** `md.bars_1d`, `md.index_members`, `ev.positions` (open book). **Writes:** `md.sector_daily`,
`md.trend_1d`, `md.breadth_daily`, `ev.recommendations`, `ev.rejections`.

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
