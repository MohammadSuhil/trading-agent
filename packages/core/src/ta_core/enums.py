"""Closed enumerations.

These are CLOSED sets on purpose. The learning loop attributes P&L per reason
code, so a free-text reason would make attribution impossible: you cannot build
a posterior over a category that never repeats. Adding a member is a deliberate
act that invalidates prior statistics for that code, so it is versioned.
"""
from __future__ import annotations

from enum import StrEnum


class Horizon(StrEnum):
    """Holding-period class. Determines barrier width and the time stop."""

    INTRADAY = "INTRADAY"      # same session
    SWING = "SWING"            # 2-20 sessions
    POSITIONAL = "POSITIONAL"  # 1-12 months


# Vertical-barrier length in sessions, per horizon.
# MUST equal the live engine's time stop or labels and execution disagree
# and every model is trained on a counterfactual.
HORIZON_SESSIONS: dict[Horizon, int] = {
    Horizon.INTRADAY: 1,
    Horizon.SWING: 10,
    Horizon.POSITIONAL: 60,
}


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Action(StrEnum):
    """What to actually do. The split between WATCH and HOLD is the point.

    `HOLD` means *you own this and should keep owning it*. It is only ever
    valid for a symbol that is in the portfolio. Using it for a screened
    candidate you have never bought is a real misstatement, not a labelling
    quibble: it reads as advice about an existing position and hides the fact
    that the system is not telling you to do anything.

    `WATCH` is the honest label for "this ranked well but is not actionable" --
    because the edge is not demonstrated, or because a capital-protection cap
    displaced it. A WATCH card carries a full trade plan so you can see what
    the trade WOULD be, with qty 0, because you are not being told to take it.
    """

    BUY = "BUY"        # open a new position now, at the stated size
    WATCH = "WATCH"    # ranked, not actionable; NOT owned; qty is 0
    HOLD = "HOLD"      # owned -> keep. Portfolio positions only.
    TRIM = "TRIM"      # owned -> reduce
    SELL = "SELL"      # owned -> exit in full
    AVOID = "AVOID"    # explicitly screened out


#: Actions that are only meaningful for a symbol you already own. Emitting one
#: of these for a name that is not in the portfolio is a bug, and
#: `tests/test_action_semantics.py` asserts the pipeline never does it.
PORTFOLIO_ONLY_ACTIONS: frozenset[Action] = frozenset({
    Action.HOLD, Action.TRIM, Action.SELL,
})


class ProductType(StrEnum):
    """Drives the cost model: intraday and delivery are taxed differently."""

    DELIVERY = "DELIVERY"  # CNC
    INTRADAY = "INTRADAY"  # MIS


class Regime(StrEnum):
    """Market regime. Used as a risk gate that scales gross exposure --
    never as a signal flipper."""

    TREND_UP = "TREND_UP"
    CHOP = "CHOP"
    TREND_DOWN = "TREND_DOWN"
    HIGH_VOL = "HIGH_VOL"
    UNKNOWN = "UNKNOWN"


class Sleeve(StrEnum):
    """Strategy sleeves the Thompson sampler allocates across."""

    MOMENTUM_BREAKOUT = "MOMENTUM_BREAKOUT"
    PULLBACK_IN_UPTREND = "PULLBACK_IN_UPTREND"
    MEAN_REVERSION = "MEAN_REVERSION"
    NEWS_CATALYST = "NEWS_CATALYST"
    SECTOR_ROTATION = "SECTOR_ROTATION"
    OI_BUILDUP = "OI_BUILDUP"


class Arm(StrEnum):
    """The four arms scored identically every night.

    FROZEN_CONTROL is the single most valuable number in the project: if the
    'learned' system cannot beat day-1 rules, the learning is cosmetic.
    """

    FROZEN_CONTROL = "FROZEN_CONTROL"
    DETERMINISTIC = "DETERMINISTIC"
    COMMITTEE = "COMMITTEE"
    RANDOM = "RANDOM"


class BarrierHit(StrEnum):
    """Triple-barrier outcome."""

    UP = "UP"                # profit target touched first
    DOWN = "DOWN"            # stop touched first
    VERTICAL = "VERTICAL"    # time stop reached
    CANCELLED = "CANCELLED"  # vetoed pre-open; itself a scoreable signal
    OPEN = "OPEN"            # not yet resolved


class ReasonCode(StrEnum):
    """Why the system took the trade. CLOSED SET.

    Every recommendation carries >=1. P&L is exploded one row per reason code,
    so these are the unit of learning.
    """

    # trend / momentum
    MA_STACK_ALIGNED = "MA_STACK_ALIGNED"
    BREAKOUT_52W_PROXIMITY = "BREAKOUT_52W_PROXIMITY"
    MULTI_HORIZON_MOMENTUM = "MULTI_HORIZON_MOMENTUM"
    ADX_TRENDING = "ADX_TRENDING"
    # mean reversion
    OVERSOLD_ZSCORE = "OVERSOLD_ZSCORE"
    BOLLINGER_REVERSION = "BOLLINGER_REVERSION"
    PULLBACK_TO_SUPPORT = "PULLBACK_TO_SUPPORT"
    # volume / liquidity
    RVOL_SPIKE = "RVOL_SPIKE"
    DELIVERY_PCT_SPIKE = "DELIVERY_PCT_SPIKE"
    TURNOVER_EXPANSION = "TURNOVER_EXPANSION"
    # relative strength
    RS_VS_SECTOR = "RS_VS_SECTOR"
    RS_VS_NIFTY = "RS_VS_NIFTY"
    SECTOR_LEADERSHIP = "SECTOR_LEADERSHIP"
    # derivatives
    OI_LONG_BUILDUP = "OI_LONG_BUILDUP"
    OI_SHORT_COVERING = "OI_SHORT_COVERING"
    # news / events
    EARNINGS_SURPRISE = "EARNINGS_SURPRISE"
    GUIDANCE_RAISED = "GUIDANCE_RAISED"
    ORDER_WIN = "ORDER_WIN"
    RATING_UPGRADE = "RATING_UPGRADE"
    BLOCK_DEAL_ACCUMULATION = "BLOCK_DEAL_ACCUMULATION"
    REGULATORY_TAILWIND = "REGULATORY_TAILWIND"
    # fundamentals
    QUALITY_COMPOSITE = "QUALITY_COMPOSITE"
    VALUE_COMPOSITE = "VALUE_COMPOSITE"
    GROWTH_COMPOSITE = "GROWTH_COMPOSITE"
    EARNINGS_REVISION_UP = "EARNINGS_REVISION_UP"
    PROMOTER_BUYING = "PROMOTER_BUYING"
    # risk-side / exits
    STOP_DISCIPLINE = "STOP_DISCIPLINE"
    TIME_STOP = "TIME_STOP"
    REGIME_RISK_OFF = "REGIME_RISK_OFF"


class FailureMode(StrEnum):
    """Assigned by the weekly critic to resolved losers. CLOSED SET,
    so post-mortems aggregate into a chi-square table instead of prose."""

    THESIS_WRONG = "THESIS_WRONG"
    ENTRY_TOO_LATE = "ENTRY_TOO_LATE"
    STOP_TOO_TIGHT = "STOP_TOO_TIGHT"
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    SIZE_TOO_LARGE = "SIZE_TOO_LARGE"
    REGIME_MISREAD = "REGIME_MISREAD"
    NEWS_STALE = "NEWS_STALE"
    NEWS_MISREAD = "NEWS_MISREAD"
    LIQUIDITY_TRAP = "LIQUIDITY_TRAP"
    GAP_RISK = "GAP_RISK"
    SECTOR_DRAWDOWN = "SECTOR_DRAWDOWN"
    MARKET_DRAWDOWN = "MARKET_DRAWDOWN"
    EXECUTION_SLIPPAGE = "EXECUTION_SLIPPAGE"
    UNLUCKY_VARIANCE = "UNLUCKY_VARIANCE"


class DataQuality(StrEnum):
    """Stamped on every trading day. A degraded day must be distinguishable
    in the ledger or the learning loop fits noise."""

    FULL = "FULL"
    PARTIAL = "PARTIAL"    # some optional source failed
    DEGRADED = "DEGRADED"  # a smaller model was used than intended


class TrendState(StrEnum):
    """Multi-timeframe trend classification.

    Eight members, first-match-wins. `UNDEFINED` is NOT a synonym for `RANGE`:
    a symbol with too little history, or one that failed the liquidity gate,
    has no measurable trend, and recording that as "range-bound" would feed the
    learning loop a fact that was never observed.

    A `*_DECAYING` state blocks NEW entries in trend-following sleeves. It
    never forces an exit and never flips a signal to the opposite side -- a
    weakening trend is a reason to stop adding risk, not a reason to reverse.
    """

    UP_STRONG = "UP_STRONG"
    UP = "UP"
    UP_DECAYING = "UP_DECAYING"
    RANGE = "RANGE"
    DOWN_DECAYING = "DOWN_DECAYING"
    DOWN = "DOWN"
    DOWN_STRONG = "DOWN_STRONG"
    UNDEFINED = "UNDEFINED"


#: Bumped whenever a TrendState threshold moves or a member is added.
#: Rows carrying an older version are excluded from posterior updates, because
#: a state that meant something different then is not the same observation.
TREND_ENUM_VERSION: int = 1

TREND_FOLLOWING_SLEEVES: frozenset[str] = frozenset({
    "MOMENTUM_BREAKOUT", "PULLBACK_IN_UPTREND", "SECTOR_ROTATION",
})


class RuleState(StrEnum):
    """Status of a capital-protection rule at evaluation time.

    `DISPLAY_ONLY` is load-bearing: several rules are specified before the
    statistics that would make them trustworthy exist. Showing such a rule as
    `OK` would imply an assurance nobody has earned.
    """

    OK = "OK"
    WARN = "WARN"
    BREACH = "BREACH"
    DISPLAY_ONLY = "DISPLAY_ONLY"


class RuleAction(StrEnum):
    """What a breach does. Separated from the rule so it is auditable."""

    ALLOW = "ALLOW"
    WARN = "WARN"
    RESIZE = "RESIZE"
    REJECT = "REJECT"
    HALT_NEW = "HALT_NEW"


class BarrierState(StrEnum):
    """Whether a barrier probability may be shown at all.

    `UNAVAILABLE` must render as an em-dash plus a reason, never as a number.
    A percentage with four observations behind it is not a weak estimate, it
    is a different kind of object, and the UI must not let the two share a
    visual language.
    """

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"
