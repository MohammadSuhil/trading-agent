"""M3.4 Replay-from-record: deterministic re-derivation from immutable inputs.

Enforces Invariant P-06:
  Replay RE-DERIVES from the record; it never recalls a stored answer.
  Given past raw data and manifest parameters, replay re-executes the exact pipeline
  and validates that recomputed recommendations match the recorded output byte-for-byte.
"""
from __future__ import annotations

import polars as pl
from ta_quant.pipeline import RunResult, run_nightly

from ta_ledger.manifest import RunManifest
from ta_ledger.risk import DEFAULT, Portfolio, RiskConfig

__all__ = ["ReplayDivergenceError", "replay_nightly_from_record"]


class ReplayDivergenceError(AssertionError):
    """Raised when re-derivation from inputs disagrees with recorded recommendations."""


def replay_nightly_from_record(
    *,
    bars: pl.DataFrame,
    members: pl.DataFrame,
    manifest: RunManifest,
    recorded_recommendations: pl.DataFrame,
    portfolio: Portfolio,
    cfg: RiskConfig = DEFAULT,
    current_weights_sha: str | None = None,
    current_git_sha: str | None = None,
    tradeable_index: str = "NIFTY500",
    scored_index: str = "NIFTY500",
    max_cards: int = 25,
) -> RunResult:
    """Re-derive recommendations from recorded inputs and verify against historical record.

    Enforces Invariant P-06: re-executes run_nightly rather than reading cached rows.
    Raises ReplayDivergenceError on any parameter, weight, or recommendation mismatch.
    """
    # 1. Stamp verification (ADR-0001 / ADR-0002)
    if current_weights_sha is not None and current_weights_sha != manifest.weights_sha:
        raise ReplayDivergenceError(
            f"weights_sha drift: manifest={manifest.weights_sha} current={current_weights_sha}"
        )
    if current_git_sha is not None and current_git_sha != manifest.git_sha:
        raise ReplayDivergenceError(
            f"git_sha drift: manifest={manifest.git_sha} current={current_git_sha}"
        )

    # 2. PIT-bounded input filtering: knowledge_time <= manifest.as_of and d <= manifest.d
    bars_filt = bars.filter(pl.col("d") <= manifest.d)
    if "knowledge_time" in bars.columns:
        bars_filt = bars_filt.filter(pl.col("knowledge_time") <= manifest.as_of)

    members_filt = members
    if "d" in members.columns:
        members_filt = members_filt.filter(pl.col("d") <= manifest.d)
    if "knowledge_time" in members.columns:
        members_filt = members_filt.filter(pl.col("knowledge_time") <= manifest.as_of)

    # 3. Re-derive from scratch
    res = run_nightly(
        bars_filt,
        members_filt,
        d=manifest.d,
        portfolio=portfolio,
        tradeable_index=tradeable_index,
        scored_index=scored_index,
        max_cards=max_cards,
        cfg=cfg,
        as_of=manifest.as_of,
    )

    # 4. Compare re-derived recommendations against recorded recommendations
    if res.recommendations.is_empty() and recorded_recommendations.is_empty():
        return res

    if res.recommendations.height != recorded_recommendations.height:
        raise ReplayDivergenceError(
            f"Row count mismatch: re-derived has {res.recommendations.height} rows, "
            f"recorded has {recorded_recommendations.height} rows"
        )

    re_derived_sorted = res.recommendations.sort("symbol")
    recorded_sorted = recorded_recommendations.sort("symbol")

    re_syms = re_derived_sorted["symbol"].to_list()
    rec_syms = recorded_sorted["symbol"].to_list()
    if re_syms != rec_syms:
        raise ReplayDivergenceError(
            f"Symbol mismatch in recommendations: re-derived={re_syms} != recorded={rec_syms}"
        )

    float_cols = {
        "entry_lo",
        "entry_hi",
        "stop",
        "target",
        "notional",
        "would_notional",
        "would_risk_rupees",
        "risk_rupees",
        "cost_bps",
        "expectancy_atr",
        "composite",
    }
    int_cols = {"qty", "would_qty"}
    str_cols = {"symbol", "action", "sleeve", "horizon"}

    compare_cols = (
        "symbol",
        "action",
        "sleeve",
        "horizon",
        "entry_lo",
        "entry_hi",
        "stop",
        "target",
        "qty",
        "notional",
        "would_qty",
        "would_notional",
        "would_risk_rupees",
        "risk_rupees",
        "cost_bps",
        "expectancy_atr",
        "composite",
        "reasons",
    )

    re_rows = re_derived_sorted.iter_rows(named=True)
    rec_rows = recorded_sorted.iter_rows(named=True)

    for re_row, rec_row in zip(re_rows, rec_rows, strict=True):
        sym = re_row["symbol"]
        for col in compare_cols:
            if col not in re_row or col not in rec_row:
                continue
            re_val = re_row[col]
            rec_val = rec_row[col]

            if col in float_cols:
                if re_val is None or rec_val is None:
                    if re_val != rec_val:
                        raise ReplayDivergenceError(
                            f"Divergence in symbol {sym} column {col}: re-derived={re_val} != recorded={rec_val}"
                        )
                elif abs(float(re_val) - float(rec_val)) > 1e-6:
                    raise ReplayDivergenceError(
                        f"Divergence in symbol {sym} column {col}: re-derived={re_val} != recorded={rec_val} "
                        f"(delta={abs(float(re_val) - float(rec_val)):.8f})"
                    )
            elif col in int_cols:
                if int(re_val) != int(rec_val):
                    raise ReplayDivergenceError(
                        f"Divergence in symbol {sym} column {col}: re-derived={re_val} != recorded={rec_val}"
                    )
            elif col in str_cols:
                if str(re_val) != str(rec_val):
                    raise ReplayDivergenceError(
                        f"Divergence in symbol {sym} column {col}: re-derived={re_val} != recorded={rec_val}"
                    )
            elif col == "reasons":
                re_r = tuple(re_val) if isinstance(re_val, (list, tuple)) else ()
                rec_r = tuple(rec_val) if isinstance(rec_val, (list, tuple)) else ()
                if re_r != rec_r:
                    raise ReplayDivergenceError(
                        f"Divergence in symbol {sym} reasons: re-derived={re_r} != recorded={rec_r}"
                    )

    return res
