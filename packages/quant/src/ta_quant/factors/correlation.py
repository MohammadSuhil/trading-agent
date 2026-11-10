"""Pairwise Residual Correlation for RISK-08.

Raw returns are dominated by market beta: during a strong trend or broad sell-off,
almost every liquid stock moves in the same direction. RISK-08's purpose is not to
reject stocks simply because they have positive market beta, but to prevent
concentrating idiosyncratic risk into two stocks whose excess returns move together
(e.g., two private banks or two cement manufacturers).

We compute the market-residual return:
    res_ret(s, t) = ret_1d(s, t) - mean_{s'}(ret_1d(s', t))
and compute the trailing Pearson correlation of res_ret between each screened candidate
and each open position over the overlapping sessions in the trailing window.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date

import polars as pl

__all__ = ["compute_pair_correlations"]


def compute_pair_correlations(
    panel: pl.DataFrame,
    d: date,
    candidate_symbols: Iterable[str],
    open_symbols: Iterable[str],
    *,
    window: int = 63,
    min_periods: int = 20,
) -> dict[str, dict[str, float]]:
    """Compute trailing pairwise residual correlation for RISK-08.

    To isolate idiosyncratic risk from broad market beta, this computes the
    cross-sectionally demeaned return on each date:
        res_ret(s, t) = ret_1d(s, t) - mean_{s'}(ret_1d(s', t))
    and computes the Pearson correlation of res_ret between candidate and open
    position symbols over the overlapping sessions in the trailing window.

    >>> import polars as pl
    >>> from datetime import date, timedelta
    >>> dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(25)]
    >>> rows = []
    >>> for i, dt in enumerate(dates):
    ...     m = 0.01 if i % 2 == 0 else -0.01
    ...     shock = 0.02 if i % 3 == 0 else -0.02
    ...     rows.append({"d": dt, "symbol": "A", "ret_1d": m + shock})
    ...     rows.append({"d": dt, "symbol": "B", "ret_1d": m + shock})
    ...     rows.append({"d": dt, "symbol": "C", "ret_1d": m - shock})
    ...     rows.append({"d": dt, "symbol": "M", "ret_1d": m})
    >>> panel = pl.DataFrame(rows)
    >>> corrs = compute_pair_correlations(
    ...     panel, date(2026, 1, 25), ["A"], ["B", "C"], min_periods=20
    ... )
    >>> corrs["A"]["B"] > 0.65
    True
    >>> corrs["A"]["C"] < 0.0
    True
    """
    cand_list = list(candidate_symbols)
    open_list = list(open_symbols)

    out: dict[str, dict[str, float]] = {c: {p: 0.0 for p in open_list} for c in cand_list}
    if not cand_list or not open_list or panel.is_empty():
        return out

    # Strictly trailing data up to d
    trailing = panel.filter(pl.col("d") <= d)
    if trailing.is_empty():
        return out

    window_dates = (
        trailing.select("d")
        .unique()
        .sort("d")
        .tail(window)["d"]
        .to_list()
    )
    if len(window_dates) < min_periods:
        return out

    sub = trailing.filter(pl.col("d").is_in(window_dates))

    if "ret_1d" not in sub.columns:
        if "close" in sub.columns:
            sub = sub.sort(["symbol", "d"]).with_columns(
                (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret_1d")
            )
        else:
            return out

    # Cross-sectionally demean return on each session d
    sub = sub.with_columns(
        (pl.col("ret_1d") - pl.col("ret_1d").mean().over("d")).alias("res_ret")
    )

    needed_syms = set(cand_list) | set(open_list)
    sub = sub.filter(pl.col("symbol").is_in(list(needed_syms)) & pl.col("res_ret").is_not_null())

    sym_series: dict[str, dict[date, float]] = {}
    for row in sub.select(["symbol", "d", "res_ret"]).iter_rows():
        s, dt, r = row[0], row[1], row[2]
        sym_series.setdefault(s, {})[dt] = float(r)

    def _pearson(x: list[float], y: list[float]) -> float:
        n = len(x)
        if n < min_periods:
            return 0.0
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y, strict=True))
        var_x = sum((xi - mean_x) ** 2 for xi in x)
        var_y = sum((yi - mean_y) ** 2 for yi in y)
        if var_x <= 1e-12 or var_y <= 1e-12:
            return 0.0
        r = cov / math.sqrt(var_x * var_y)
        return max(-1.0, min(1.0, float(r)))

    for cand in cand_list:
        c_data = sym_series.get(cand)
        if not c_data:
            continue
        for pos_sym in open_list:
            if cand == pos_sym:
                out[cand][pos_sym] = 1.0
                continue
            p_data = sym_series.get(pos_sym)
            if not p_data:
                continue
            common_dates = sorted(set(c_data.keys()) & set(p_data.keys()))
            if len(common_dates) < min_periods:
                continue
            x = [c_data[dt] for dt in common_dates]
            y = [p_data[dt] for dt in common_dates]
            out[cand][pos_sym] = _pearson(x, y)

    return out
