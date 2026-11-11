"""P(target hit), P(stop hit), P(time-stop) -- the most dangerous numbers here.

A percentage looks authoritative whether or not it is earned, so this module is
built to REFUSE more readily than it answers.

Design, and why each piece is not the obvious one:

* **Empirical, not analytic.** A Brownian/lognormal kernel systematically
  understates P(stop) on Indian equities: it has no gaps, no circuit limits and
  no fat tails, and all three of those hit the stop, not the target. We instead
  resample real forward paths, ATR-standardised at t0, so gaps and circuits are
  in the sample by construction.

* **Competing risks, one Dirichlet.** The three outcomes are mutually exclusive
  and exhaustive over the horizon, so they come from ONE Dirichlet posterior
  and sum to 1 by construction. Three independently-fitted Betas would not sum
  to 1 and the error would be invisible.

* **Pessimistic same-bar resolution.** When a single bar's range spans both
  barriers, daily data cannot say which came first. We record DOWN. Choosing UP
  would flatter every backtest in exactly the cases that hurt most.

* **n_eff, never a row count.** Overlapping forward windows are not independent
  observations. Kish's effective sample size on uniqueness weights is what
  gates display, and a raw count may not appear in any UI element.

The headline behaviour: below the n_eff floor this returns `UNAVAILABLE` with a
reason string, and the UI renders an em-dash. A `73%` with four observations
behind it is not a weak estimate -- it is a different kind of object, and the
two must not share a visual language.
"""
from __future__ import annotations

from dataclasses import dataclass

import polars as pl
from ta_core.enums import HORIZON_SESSIONS, BarrierState, Horizon

__all__ = [
    "MIN_N_EFF",
    "WIDE_INTERVAL_PP",
    "BarrierEstimate",
    "build_path_library",
    "estimate_barriers",
]

#: Effective sample size below which no percentage is shown. Deliberately high:
#: the cost of a confident wrong number here is a mis-sized trade.
MIN_N_EFF: float = 200.0

#: Dirichlet concentration for the shrink toward the horizon's global base
#: rate. kappa=30 means a cell needs ~30 effective observations before it
#: meaningfully departs from the pooled estimate.
KAPPA: float = 30.0

#: A 90% interval wider than this (in percentage points) suppresses the point
#: estimate entirely -- the range is the honest answer.
WIDE_INTERVAL_PP: float = 0.15


@dataclass(frozen=True, slots=True)
class BarrierEstimate:
    """The triad plus everything needed to judge whether to believe it."""

    state: BarrierState
    p_up: float | None
    p_dn: float | None
    p_vert: float | None
    ci_lo: float | None
    ci_hi: float | None
    n_eff: float
    reason: str
    horizon: Horizon = Horizon.SWING
    k_up: float = 2.0
    k_dn: float = 1.0

    @property
    def wide(self) -> bool:
        """True when the interval is too wide to quote a point estimate."""
        if self.ci_lo is None or self.ci_hi is None:
            return True
        return (self.ci_hi - self.ci_lo) > WIDE_INTERVAL_PP

    def as_dict(self) -> dict:
        return {
            "state": self.state.value,
            "p_up": self.p_up, "p_dn": self.p_dn, "p_vert": self.p_vert,
            "ci_lo": self.ci_lo, "ci_hi": self.ci_hi,
            "n_eff": round(self.n_eff, 1), "reason": self.reason,
            # Authoritative: the midpoint must not be rendered when this is
            # true. Computed here rather than re-derived in the client, so the
            # threshold cannot drift between the two.
            "wide": self.wide,
        }


def _unavailable(reason: str, n_eff: float = 0.0, **kw) -> BarrierEstimate:
    return BarrierEstimate(
        BarrierState.UNAVAILABLE, None, None, None, None, None, n_eff, reason, **kw
    )


def build_path_library(
    bars: pl.DataFrame,
    *,
    horizon: Horizon = Horizon.SWING,
    k_up: float = 2.0,
    k_dn: float = 1.0,
) -> pl.DataFrame:
    """Resolve the triple barrier for every (symbol, start) in `bars`.

    `bars` needs `symbol, d, close, high, low, atr14` sorted by (symbol, d).
    Barriers are ATR-scaled: fixed-percentage barriers make high-volatility
    stocks look predictable, because a 5% target is a short walk for a 4%-ATR
    stock and a marathon for a 0.8%-ATR one.

    Returns one row per resolvable start with `outcome` in {UP, DOWN, VERTICAL}
    and the uniqueness weight used for n_eff.

    >>> import polars as pl
    >>> # A clean run-up: the target is reached, the stop never is.
    >>> n = 30
    >>> df = pl.DataFrame({
    ...     "symbol": ["A"] * n,
    ...     "d": pl.date_range(pl.date(2026, 1, 1), pl.date(2026, 1, 30),
    ...                        "1d", eager=True),
    ...     "close": [100.0 + i for i in range(n)],
    ...     "high":  [101.0 + i for i in range(n)],
    ...     "low":   [ 99.0 + i for i in range(n)],
    ...     "atr14": [2.0] * n,
    ... })
    >>> lib = build_path_library(df, horizon=Horizon.SWING)
    >>> lib["outcome"].unique().to_list()
    ['UP']
    """
    h = HORIZON_SESSIONS[horizon]
    s = pl.col("symbol")

    df = bars.sort(["symbol", "d"]).with_columns(
        (pl.col("close") + k_up * pl.col("atr14")).alias("_up"),
        (pl.col("close") - k_dn * pl.col("atr14")).alias("_dn"),
        pl.int_range(pl.len()).over(s).alias("_i"),
        pl.len().over(s).alias("_n"),
        # The session on which this path's window closes. A path is only
        # *known* once its window has elapsed, so a backtest that builds the
        # library once over the whole panel must filter on this, not on the
        # start date `d` -- filtering on `d < session` admits the ~h starts
        # whose outcomes are decided by bars after the session (look-ahead).
        pl.col("d").shift(-h).over(s).alias("exit_d"),
    )

    # Forward scan, one shifted column per step. The first step whose bar
    # touches a barrier wins; ties within a bar resolve DOWN (pessimistic).
    hit_up = pl.lit(None, dtype=pl.Int32)
    hit_dn = pl.lit(None, dtype=pl.Int32)
    for step in range(1, h + 1):
        fh = pl.col("high").shift(-step).over(s)
        fl = pl.col("low").shift(-step).over(s)
        hit_up = pl.when(hit_up.is_null() & (fh >= pl.col("_up"))).then(step).otherwise(hit_up)
        hit_dn = pl.when(hit_dn.is_null() & (fl <= pl.col("_dn"))).then(step).otherwise(hit_dn)

    df = df.with_columns(hit_up.alias("_hu"), hit_dn.alias("_hd"))

    df = df.filter(
        (pl.col("_i") + h < pl.col("_n"))          # the full window must exist
        & pl.col("atr14").is_not_null()
        & (pl.col("atr14") > 0)
    ).with_columns(
        pl.when(pl.col("_hd").is_null() & pl.col("_hu").is_null())
        .then(pl.lit("VERTICAL"))
        # <= makes the same-bar tie resolve DOWN
        .when(pl.col("_hu").is_null())
        .then(pl.lit("DOWN"))
        .when(pl.col("_hd").is_null())
        .then(pl.lit("UP"))
        .when(pl.col("_hd") <= pl.col("_hu"))
        .then(pl.lit("DOWN"))
        .otherwise(pl.lit("UP"))
        .alias("outcome"),
        # Uniqueness weight: windows of length h starting on consecutive
        # sessions overlap almost completely, so each start carries ~1/h of an
        # independent observation. Treating them as h independent draws would
        # inflate n_eff by an order of magnitude.
        pl.lit(1.0 / h).alias("w"),
        (pl.col("atr14") / pl.col("close")).alias("atr_pct"),
    )

    return df.select(["symbol", "d", "exit_d", "outcome", "w", "atr_pct"])


#: How many sessions of RESOLVED paths feed the base rates (ADR-0003).
#: With an expanding library the base rate -- and so whether the frozen
#: control trades at all -- depended on how much history happened to be
#: loaded: 420 sessions gave p_up 0.337 and 95 trades, 580 gave 0.313 and
#: zero. A fixed window makes the estimate a function of the session alone.
BASE_RATE_WINDOW_SESSIONS = 252


def known_paths(
    lib: pl.DataFrame, d, *, window: int | None = BASE_RATE_WINDOW_SESSIONS
) -> pl.DataFrame:
    """The paths a decision on session `d` may learn from.

    Two cuts, both by `exit_d` (when the outcome became knowable), never by
    start date: (1) `exit_d <= d`, so no path decided by bars after `d` leaks
    in; (2) only the last `window` distinct exit sessions, so the base rate
    does not drift with how much history was backfilled. The live nightly and
    the backtest both call this, which is what keeps them equal.

    >>> import polars as pl
    >>> from datetime import date
    >>> lib = pl.DataFrame({"exit_d": [date(2026, 1, i) for i in range(1, 11)]})
    >>> known_paths(lib, date(2026, 1, 8), window=3)["exit_d"].dt.day().to_list()
    [6, 7, 8]
    >>> known_paths(lib, date(2026, 1, 8), window=None).height
    8
    """
    if lib.is_empty():  # a too-short panel yields a column-less frame
        return lib
    known = lib.filter(pl.col("exit_d") <= d)
    if window is None or known.is_empty():
        return known
    days = known["exit_d"].unique().sort()
    if days.len() <= window:
        return known
    return known.filter(pl.col("exit_d") >= days[-window])


def _vol_tercile(lib: pl.DataFrame) -> pl.DataFrame:
    """Split the library by ATR/price terciles.

    Volatility is the strongest conditioner of barrier outcomes, and it is
    observable at t0, so conditioning on it leaks nothing.
    """
    qs = lib["atr_pct"].quantile(1 / 3), lib["atr_pct"].quantile(2 / 3)
    lo, hi = (qs[0] or 0.0), (qs[1] or 0.0)
    return lib.with_columns(
        pl.when(pl.col("atr_pct") <= lo).then(0)
        .when(pl.col("atr_pct") <= hi).then(1)
        .otherwise(2).cast(pl.Int8).alias("vol_tercile")
    )


def estimate_barriers(
    lib: pl.DataFrame,
    *,
    atr_pct: float | None = None,
    horizon: Horizon = Horizon.SWING,
    k_up: float = 2.0,
    k_dn: float = 1.0,
) -> BarrierEstimate:
    """Posterior triad for a candidate, conditioned on its volatility tercile.

    Returns `UNAVAILABLE` -- never a number -- when the conditioned cell has
    too little effective evidence. The caller must render the reason, not a
    fallback percentage.

    >>> import polars as pl
    >>> thin = pl.DataFrame({"outcome": ["UP", "DOWN"], "w": [0.1, 0.1],
    ...                      "atr_pct": [0.02, 0.02]})
    >>> e = estimate_barriers(thin)
    >>> e.state.value, e.p_up
    ('UNAVAILABLE', None)
    >>> e.reason
    'library too small: n_eff 0.2 of 200 required for swing'
    """
    return BarrierModel(lib, horizon=horizon, k_up=k_up, k_dn=k_dn).estimate(atr_pct)


class BarrierModel:
    """`estimate_barriers` for one library, computed once per cell.

    Within a session every candidate is estimated against the SAME library;
    the only input that varies is `atr_pct`, and it matters only through the
    volatility tercile it falls in. So there are at most four distinct answers
    (three terciles + unconditioned) -- yet the per-candidate call recomputed
    two quantiles and six filtered sums over ~100k rows for each of ~90
    candidates. Profiled at 92% of backtest time (45 of 49 s for 126
    sessions). Build one model per session and call `estimate()` per candidate.

    The arithmetic is unchanged -- same filters, same sums, same order -- so
    results are bit-identical to the old per-call path (tested), which the
    backtest == paper invariant depends on.

    >>> import polars as pl
    >>> lib = pl.DataFrame({"outcome": ["UP", "DOWN", "VERTICAL"] * 400,
    ...                     "w": [0.1] * 1200, "atr_pct": [0.01, 0.02, 0.03] * 400})
    >>> m = BarrierModel(lib)
    >>> m.estimate(0.015) is m.estimate(0.016)     # same tercile -> same cached object
    True
    """

    def __init__(self, lib: pl.DataFrame, *, horizon: Horizon = Horizon.SWING,
                 k_up: float = 2.0, k_dn: float = 1.0) -> None:
        self.horizon, self.k_up, self.k_dn = horizon, k_up, k_dn
        self._cache: dict[int | None, BarrierEstimate] = {}
        self._lib = _vol_tercile(lib) if lib.height else lib
        if lib.height:
            qs = self._lib["atr_pct"].quantile(1 / 3), self._lib["atr_pct"].quantile(2 / 3)
            self._q = ((qs[0] or 0.0), (qs[1] or 0.0))
            pooled_tot = float(self._lib["w"].sum()) or 1.0
            self._prior = [self._wsum(self._lib, c) / pooled_tot for c in _CLASSES]

    @staticmethod
    def _wsum(frame: pl.DataFrame, oc: str) -> float:
        return float(frame.filter(pl.col("outcome") == oc)["w"].sum())

    def estimate(self, atr_pct: float | None = None) -> BarrierEstimate:
        horizon, k_up, k_dn = self.horizon, self.k_up, self.k_dn
        if self._lib.height == 0:
            return _unavailable("no resolved paths in the library", 0.0,
                                horizon=horizon, k_up=k_up, k_dn=k_dn)
        key = None if atr_pct is None else (
            0 if atr_pct <= self._q[0] else (1 if atr_pct <= self._q[1] else 2))
        if key in self._cache:
            return self._cache[key]

        lib = self._lib
        cell = lib
        if key is not None:
            cell = lib.filter(pl.col("vol_tercile") == key)
            if cell.height == 0:
                cell = lib

        # Kish effective sample size accounting for overlap concurrency (w ~ 1/h).
        # Raw row counts are deliberately never used.
        h = HORIZON_SESSIONS[horizon]
        w = cell["w"]
        sw, sw2 = float(w.sum()), float((w**2).sum())
        n_eff = (sw * sw) / (h * sw2) if (sw2 > 0 and h > 0) else 0.0

        if n_eff < MIN_N_EFF:
            est = _unavailable(
                f"library too small: n_eff {n_eff:.1f} of {MIN_N_EFF:.0f} required "
                f"for {horizon.value.lower()}",
                n_eff, horizon=horizon, k_up=k_up, k_dn=k_dn,
            )
            self._cache[key] = est
            return est

        # Weighted counts in the cell, shrunk toward the pooled base rate.
        obs = [self._wsum(cell, c) for c in _CLASSES]
        obs_tot = sum(obs) or 1.0
        scale = n_eff / obs_tot          # convert weight mass to effective counts

        alpha = [KAPPA * pr + o * scale for pr, o in zip(self._prior, obs, strict=True)]
        a_tot = sum(alpha)
        p = [a / a_tot for a in alpha]

        # Posterior SD of the UP marginal (Dirichlet marginal is Beta).
        a_up = alpha[0]
        var_up = (a_up * (a_tot - a_up)) / (a_tot * a_tot * (a_tot + 1.0))
        sd = var_up**0.5
        lo = max(0.0, p[0] - 1.645 * sd)
        hi = min(1.0, p[0] + 1.645 * sd)

        est = BarrierEstimate(
            BarrierState.OK,
            p_up=round(p[0], 4), p_dn=round(p[1], 4), p_vert=round(p[2], 4),
            ci_lo=round(lo, 4), ci_hi=round(hi, 4),
            n_eff=n_eff,
            reason=f"empirical, {horizon.value.lower()} h={HORIZON_SESSIONS[horizon]}, "
                   f"k_up={k_up} k_dn={k_dn}",
            horizon=horizon, k_up=k_up, k_dn=k_dn,
        )
        self._cache[key] = est
        return est


_CLASSES = ("UP", "DOWN", "VERTICAL")
