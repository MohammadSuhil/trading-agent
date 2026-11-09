# ta_ledger — orientation

Capital protection today; the hash-chained ledger and PaperBroker next (M2).
**Pure functions, no I/O**, so every rule has a breaching-portfolio test.

Signatures: [docs/CODEMAP.md](../../docs/CODEMAP.md) § packages/ledger.

## `risk.py` (the only module so far)
- `evaluate_entry(pf, candidate, regime=, cfg=, halted=) -> Decision` — may it open, at what size.
- `rule_states(pf, regime=, cfg=)` — book-level rule status for the dashboard.
- `RiskConfig` holds **every** threshold (dashboard and gate read the same object).
  Key numbers: ₹5L equity; ₹50k min / ₹1L max notional; 0.8% risk/trade; 6.0% heat
  cap; **7 positions, 9 backstop**; industry ≤ 25% / 2 names (FinServ 30% / 3);
  pair residual corr ≤ 0.65; ADTV ≥ ₹5 cr; drawdown rungs 8/12/15/18%, halt 25%.
- Rules present: RISK-01…08, 10–14, 16–18, 20, 22, 25, 26.

**Why 7, not 10:** the flat ₹15.34 DP charge forces a ₹50k floor; with 0.8% risk
and the 6% heat cap, seven is what satisfies both. The floor also caps stops at
12.5%, biasing the book to correlated large caps — which is what RISK-08 is for.

## Known gaps
- **RISK-08 never fires:** `Candidate.pair_corr` is always `{}`; nothing computes it.
- **RISK-12** only catches the series check; `md.surveillance` is empty.
- `broker/` is empty: no PaperBroker, no fills, so nothing resolves.

## To build here (M2 → M8)
`events.py` (hash chain), `manifest.py`, `broker/{base,paper}.py` with **pure**
`simulate_fill()`/`apply_costs()` that M3 imports literally, `rebuild.py`; later
`outcomes`, `attribute`, `posteriors`, `calibration`, `bandit`, `kpis` (M8).

**Tests:** `tests/test_risk_gates.py`, `tests/test_market_regime.py`. A rule without
a failing-case test is a comment.
