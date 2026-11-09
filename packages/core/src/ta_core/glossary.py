"""Plain-English explanations for every coded term the dashboard shows.

This lives in the backend, not the UI, for the same reason every other number
does: one source of truth. A tooltip hand-written in React drifts from the
rule that actually fires, and the drift is invisible until someone trades on
the tooltip.

The audience is explicitly NOT a fund manager. `MA_STACK_ALIGNED` means
nothing to a person who has not met it before, and a dashboard that assumes
otherwise is just a wall of acronyms. Each entry therefore has:

  term    - the code as it appears on screen
  short   - 2-6 words, fits in a chip's tooltip
  plain   - one or two sentences, no jargon, no second acronym
  why     - why it is evidence *for a trade*, which is the part a glossary
            usually omits and the part that actually helps

Keep `plain` free of other codes. If an explanation needs a second term
explained, it is not plain yet.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GLOSSARY", "Term", "as_dict", "describe"]


@dataclass(frozen=True, slots=True)
class Term:
    term: str
    short: str
    plain: str
    why: str = ""
    group: str = "general"


def _t(term: str, short: str, plain: str, why: str = "", group: str = "general") -> Term:
    return Term(term, short, plain, why, group)


_TERMS: tuple[Term, ...] = (
    # ------------------------------------------------------------- actions
    _t("BUY", "Open this position now",
       "The system is telling you to actually buy this, at the size shown.",
       "It cleared every safety rule and its expected gain beats its costs.",
       "action"),
    _t("WATCH", "Ranked, but do not buy",
       "This looked good enough to rank, but the system is NOT telling you to "
       "buy it. The full plan is shown so you can see what the trade would be.",
       "Either the edge is not proven yet, or a safety cap used up the room it "
       "needed. Quantity is zero on purpose.",
       "action"),
    _t("HOLD", "You own it — keep it",
       "Only ever shown for something already in your portfolio. It means do "
       "nothing today.",
       "", "action"),
    _t("TRIM", "You own it — reduce it",
       "Sell part of a position you already hold.", "", "action"),
    _t("SELL", "You own it — exit fully",
       "Close a position you already hold.", "", "action"),
    _t("AVOID", "Screened out",
       "The system looked at this and ruled it out.", "", "action"),

    # -------------------------------------------------------- reason codes
    _t("MA_STACK_ALIGNED", "Rising price, neatly stacked averages",
       "The price is above its 20-day average, which is above its 50-day, "
       "which is above its 200-day. Short-term, medium-term and long-term "
       "trends all point the same way.",
       "When the averages line up like this the stock has been climbing "
       "steadily rather than jumping around, so a rise is more likely to "
       "continue than to immediately reverse.",
       "reason"),
    _t("MULTI_HORIZON_MOMENTUM", "Going up over several time spans",
       "The stock is up over the last month, the last quarter and the last "
       "year — not just on a single good run.",
       "One good month can be luck. Strength that shows up across several "
       "different time spans is harder to explain by chance.",
       "reason"),
    _t("RS_VS_SECTOR", "Beating its own industry",
       "This stock has outperformed the average of other companies in the "
       "same industry.",
       "If an entire industry rises, owning any of it would have worked. "
       "Beating the industry is evidence about this company specifically, "
       "rather than about the sector it happens to sit in.",
       "reason"),
    _t("RS_VS_NIFTY", "Beating the broad market",
       "The stock has done better than the Nifty 50 index over the period.",
       "Shows the strength is not just the whole market floating upward.",
       "reason"),
    _t("ADX_TRENDING", "Moving with conviction",
       "A measure of trend strength is high, meaning the price is travelling "
       "in one direction rather than chopping sideways.",
       "Trades that need a move to continue do badly in sideways markets. "
       "This filters those out.",
       "reason"),
    _t("RVOL_SPIKE", "Unusually heavy trading today",
       "Far more shares changed hands than this stock's normal daily volume.",
       "Heavy volume means real money moved, not a thin drift on a few "
       "trades. It often marks the start of a move rather than noise.",
       "reason"),
    _t("TURNOVER_EXPANSION", "Much more money traded than usual",
       "The rupee value traded is well above this stock's recent average.",
       "Similar to heavy volume, but measured in money rather than share "
       "count, so it is not distorted by a low share price.",
       "reason"),
    _t("SECTOR_LEADERSHIP", "Near its 52-week high",
       "The price is close to the highest level it has reached in a year.",
       "Stocks near their highs have no trapped sellers waiting to break even "
       "overhead, which historically makes further rises easier.",
       "reason"),
    _t("OVERSOLD_ZSCORE", "Fallen unusually far, unusually fast",
       "The price has dropped well below its own recent average by more than "
       "its normal day-to-day wobble would explain.",
       "Used for bounce-back trades: a sharp drop with no change in the "
       "underlying business often partly reverses.",
       "reason"),
    _t("BOLLINGER_REVERSION", "Stretched away from its average",
       "The price has moved far enough from its typical range that it tends "
       "to snap back.", "", "reason"),
    _t("PULLBACK_TO_SUPPORT", "Dipped to a level that held before",
       "The price has fallen back to an area where buyers stepped in "
       "previously.", "", "reason"),
    _t("QUALITY_COMPOSITE", "Scored well overall",
       "No single standout signal — it ranked well on the combined score.",
       "Shown when a candidate got in on overall ranking rather than one "
       "specific trigger.",
       "reason"),

    # ------------------------------------------------------------- sleeves
    _t("MOMENTUM_BREAKOUT", "Buying strength",
       "Buys stocks that are already rising and pushing into new high ground.",
       "", "sleeve"),
    _t("PULLBACK_IN_UPTREND", "Buying a dip in a rising stock",
       "Buys stocks in a clear uptrend that have temporarily dipped.",
       "", "sleeve"),
    _t("MEAN_REVERSION", "Buying an overdone fall",
       "Buys stocks that have dropped sharply, betting on a partial bounce.",
       "", "sleeve"),
    _t("SECTOR_ROTATION", "Buying the leaders of a strong industry",
       "Buys the strongest names inside industries that are doing well.",
       "", "sleeve"),
    _t("NEWS_CATALYST", "Buying on a specific event",
       "Buys in response to a company announcement. Not active yet.",
       "", "sleeve"),
    _t("OI_BUILDUP", "Following derivatives positioning",
       "Uses futures and options activity as a signal. Not active yet.",
       "", "sleeve"),

    # -------------------------------------------------------- trend states
    _t("UP_STRONG", "Strong, steady uptrend",
       "Rising, and doing it in a straight line rather than zig-zagging.",
       "", "trend"),
    _t("UP", "Uptrend", "Rising, with reasonable consistency.", "", "trend"),
    _t("UP_DECAYING", "Uptrend losing steam",
       "Still rising, but with less force than before.",
       "New purchases in trend-following strategies are blocked here. It does "
       "not mean sell.",
       "trend"),
    _t("RANGE", "Going sideways",
       "No reliable direction — drifting within a band.", "", "trend"),
    _t("DOWN_DECAYING", "Downtrend losing steam",
       "Still falling, but with less force than before.", "", "trend"),
    _t("DOWN", "Downtrend", "Falling, with reasonable consistency.", "", "trend"),
    _t("DOWN_STRONG", "Strong, steady downtrend",
       "Falling, and doing it persistently.", "", "trend"),
    _t("UNDEFINED", "Not enough history to say",
       "There is not enough trading history to measure a trend. This is NOT "
       "the same as 'sideways' — it means not measured.",
       "", "trend"),

    # ------------------------------------------------------------ horizons
    _t("SWING", "Days to a few weeks",
       "The trade is expected to play out over roughly 2 to 20 trading "
       "sessions. The time stop is 10 sessions.",
       "Matching the holding period to the signal matters: a signal measured "
       "over weeks tells you nothing about the next hour.",
       "horizon"),
    _t("POSITIONAL", "Months",
       "A longer hold, roughly 1 to 12 months, with a 60-session time stop.",
       "", "horizon"),
    _t("INTRADAY", "Same day",
       "Opened and closed within one session. Not active yet.",
       "SEBI's own data: 71% of individual intraday cash traders lost money "
       "in FY2022-23, rising to 80% among the most frequent.",
       "horizon"),

    # ------------------------------------------------------------- regimes
    _t("TREND_UP", "Market broadly rising",
       "Most stocks are above their 50-day average and the median stock is "
       "going up.",
       "Full risk limits apply: up to 92% invested and the normal position "
       "count.",
       "regime"),
    _t("CHOP", "Market going sideways",
       "No clear direction across the market as a whole.",
       "Limits tighten: less invested, fewer new entries per night.",
       "regime"),
    _t("TREND_DOWN", "Market broadly falling",
       "Only a third or fewer of stocks are above their 50-day average.",
       "Limits tighten hard: at most half invested, half in cash, and one new "
       "entry per night. This is why few or no names are actionable today.",
       "regime"),
    _t("HIGH_VOL", "Market unusually wild",
       "Stocks are moving far apart from each other day to day.",
       "The most defensive setting: at most a third invested.",
       "regime"),
    _t("UNKNOWN", "Cannot tell",
       "Not enough information to classify the market today.",
       "Treated exactly as defensively as a falling market -- an unmeasured "
       "market is not assumed to be a calm one.",
       "regime"),

    # -------------------------------------------------------- the numbers
    _t("entry", "Price range to buy in",
       "Buy anywhere inside this band. Paying more than the top of it "
       "changes the maths the plan is based on.", "", "metric"),
    _t("stop", "Where you give up",
       "If the price falls to here, sell. This caps the loss on the trade.",
       "Every position size on this dashboard is worked backwards from this "
       "number — that is what keeps a bad trade from becoming a big loss.",
       "metric"),
    _t("target", "Where you take the profit",
       "The price the trade is aiming for.", "", "metric"),
    _t("risk", "Rupees you lose if the stop is hit",
       "The actual money at risk: (entry minus stop) times quantity.",
       "Capped at 0.8% of your capital per trade, so no single trade can "
       "hurt much.",
       "metric"),
    _t("R:R", "Reward compared to risk",
       "How much you stand to make versus how much you stand to lose. "
       "1.33 means you would gain 1.33 rupees for every 1 rupee risked.",
       "", "metric"),
    _t("cost", "Full round-trip cost",
       "Brokerage, taxes, exchange fees and the depository charge for buying "
       "and later selling, as a share of the position.",
       "Indian delivery costs run around 25 basis points (0.25%) on a "
       "Rs 50,000 position. A trade has to beat this before it makes you "
       "anything.",
       "metric"),
    _t("expectancy", "Expected gain per trade, after costs",
       "The average result expected from this trade, measured in units of "
       "the stock's typical daily move, with all costs already subtracted.",
       "Negative means the system does not expect this trade to make money. "
       "That is shown rather than hidden.",
       "metric"),
    _t("ATR", "The stock's typical daily move",
       "Roughly how far this stock travels in an average day, in rupees.",
       "Used instead of percentages so a calm stock and a wild one are "
       "measured on their own scale.",
       "metric"),
    _t("n_eff", "How much real evidence is behind the estimate",
       "The number of genuinely independent historical examples supporting "
       "the probability shown. Lower than the raw count, because overlapping "
       "time windows are not independent.",
       "A percentage backed by 4 examples and one backed by 4,000 should not "
       "look the same. This is the number that decides whether a percentage "
       "is shown at all.",
       "metric"),
    _t("P(up barrier first)", "Chance of hitting the target before the stop",
       "Out of all the historical cases that looked like this one, the share "
       "that reached the profit target before hitting the stop.",
       "Shown as a range, never a single number, because the range is honest "
       "about how uncertain it is.",
       "metric"),
    _t("portfolio heat", "Total money at risk right now",
       "Add up what you would lose if every open position hit its stop "
       "today. Capped at 6% of your capital.",
       "This is the number that stops a run of bad luck from compounding.",
       "metric"),
    _t("drawdown", "How far below your best you are",
       "The fall from the highest your account has ever been.",
       "", "metric"),
    _t("regime", "What kind of market this is",
       "A crude read of overall conditions. It only ever tightens safety "
       "limits — it never decides what to buy.",
       "", "metric"),
    _t("breadth", "How many stocks are rising",
       "The share of the market going up rather than down.",
       "A rising index carried by three stocks is weaker than one where most "
       "stocks are rising.",
       "metric"),
    _t("DISPLAY_ONLY", "Shown, but not enforced yet",
       "This rule is defined but does not yet affect any decision, because "
       "the data needed to trust it does not exist yet.",
       "Marked explicitly rather than shown as a green tick, which would "
       "imply a protection you do not actually have.",
       "metric"),
)

GLOSSARY: dict[str, Term] = {t.term: t for t in _TERMS}


def describe(term: str) -> Term | None:
    """
    >>> describe("RS_VS_SECTOR").short
    'Beating its own industry'
    >>> describe("NOT_A_TERM") is None
    True
    """
    return GLOSSARY.get(term)


def as_dict() -> dict[str, dict[str, str]]:
    """Serialisable form for the API.

    >>> as_dict()["WATCH"]["short"]
    'Ranked, but do not buy'
    """
    return {
        k: {"term": v.term, "short": v.short, "plain": v.plain,
            "why": v.why, "group": v.group}
        for k, v in GLOSSARY.items()
    }
