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

    # --------------------------------------------- portfolio & general metrics
    _t("equity", "Total account value",
       "Your cash plus the current market value of all shares you hold.",
       "Every safety limit and position size is calculated as a slice of this total.",
       "metric"),
    _t("cash", "Uninvested money available",
       "Money sitting safely in your account that is not currently tied up in stocks.",
       "Keeping a cash cushion means you never have to sell a good stock at a bad time.",
       "metric"),
    _t("deployed", "Money currently invested in stocks",
       "The total current rupee value of all your open stock positions combined.",
       "Capped depending on market conditions so you are never over-exposed when the market turns rough.",
       "metric"),
    _t("watermark", "Highest account value reached",
       "The highest peak your total account value has ever touched since day one.",
       "Used as the reference point to measure how far your account has dipped from its best day.",
       "metric"),
    _t("pnl", "Profit or loss in rupees",
       "How many rupees you have gained or lost on this position since buying it, at the latest price.",
       "", "metric"),
    _t("pnl_pct", "Profit or loss in percent",
       "The percentage gain or loss on this position compared to your purchase price.",
       "", "metric"),
    _t("composite", "Overall ranking score",
       "A combined score summarizing how well a stock ranks across trend strength, momentum, and trading activity.",
       "Lets the system compare hundreds of stocks fairly on one consistent yardstick.",
       "metric"),
    _t("ret_1d", "One-day price change",
       "How much the price moved up or down in the latest trading session.",
       "", "metric"),
    _t("ret_5d", "One-week price change",
       "How much the price moved over the last 5 trading sessions (roughly one calendar week).",
       "", "metric"),
    _t("ret_21d", "One-month price change",
       "How much the price moved over the last 21 trading sessions (roughly one calendar month).",
       "", "metric"),
    _t("turnover_cr", "Total money traded (Rs crore)",
       "The total rupee value of shares bought and sold in this session, measured in crores (1 crore = 10 million rupees).",
       "Shows where real money is flowing across industries.",
       "metric"),
    _t("er_63", "How smoothly the price moved (63 days)",
       "Measures whether the stock travelled in a clean straight line (near 1) or zig-zagged back and forth (near 0) over the last three months.",
       "Smooth trends are much easier to hold than choppy ones that repeatedly shake you out.",
       "metric"),
    _t("slope_t_63", "Consistency of the 3-month climb",
       "Measures how steady the daily upward or downward drift has been over the last 63 trading sessions, accounting for day-to-day noise.",
       "A higher positive number means steady, reliable progress rather than a single lucky jump.",
       "metric"),
    _t("flips_63d", "Direction changes in 3 months",
       "How many times the stock's trend label switched direction over the last 63 sessions.",
       "Fewer flips mean a calmer, more predictable trend.",
       "metric"),
    _t("t20", "Short-term trend (1 month)",
       "The direction of the stock over the last 20 trading sessions (about one month).",
       "", "metric"),
    _t("t63", "Medium-term trend (3 months)",
       "The direction of the stock over the last 63 trading sessions (about one quarter).",
       "This is the primary timeframe used for swing trading.",
       "metric"),
    _t("t252", "Long-term trend (1 year)",
       "The direction of the stock over the last 252 trading sessions (about one full year).",
       "", "metric"),
    _t("headroom", "Room left before hitting the limit",
       "How much safety margin remains before a rule blocks new purchases.",
       "When headroom approaches zero, the system stops adding risk in that area.",
       "metric"),
    _t("cap_pct", "Maximum allowed share of account",
       "The highest percentage of your total capital that is allowed in this bucket.",
       "Prevents any single industry from dominating your portfolio.",
       "metric"),

    # -------------------------------------------------------------- risk rules
    _t("RISK-01", "Minimum position size (Rs 50,000)",
       "Every purchase must be at least Rs 50,000 so flat depository charges do not eat up your return.",
       "Buying tiny positions makes fixed transaction fees too expensive in percentage terms.",
       "risk"),
    _t("RISK-02", "Single-trade loss limit (0.8%)",
       "No single trade is allowed to lose more than 0.8% of your account (Rs 4,000 on Rs 5 lakh) if its stop is hit.",
       "Keeps any one bad pick from leaving a lasting dent in your capital.",
       "risk"),
    _t("RISK-03", "Maximum stop distance (12.5%)",
       "Rejects any stock whose stop-loss would need to be more than 12.5% below the buy price.",
       "Wild stocks with wide stops cannot fit inside both the minimum position size and the per-trade loss budget.",
       "risk"),
    _t("RISK-04", "Portfolio heat limit",
       "The total loss across all open positions if every stop were hit at once cannot exceed 6% of your account (tighter in weak markets).",
       "Protects your account if the entire market drops suddenly on the same day.",
       "risk"),
    _t("RISK-05", "Maximum open positions (7)",
       "Holds at most 7 stocks at the same time.",
       "Enough positions to spread risk, few enough that each position meets the Rs 50,000 floor.",
       "risk"),
    _t("RISK-05c", "Minimum cash reserve",
       "Always keeps at least 8% of your account in cash (up to 65% in wild markets).",
       "Ensures you are never 100% invested at the top of a market.",
       "risk"),
    _t("RISK-05g", "Maximum invested capital",
       "Caps the share of your account tied up in stocks at 92% in rising markets and down to 35% in volatile markets.",
       "Automatically scales back exposure when broad market conditions deteriorate.",
       "risk"),
    _t("RISK-06", "Per-industry concentration cap",
       "Limits any single industry to 2 stocks and 25% of your account (3 stocks and 30% for Financial Services).",
       "Stops you from accidentally betting your whole account on one sector.",
       "risk"),
    _t("RISK-07", "Cross-industry spread",
       "When holding 5 or more stocks, they must span at least half that many distinct industries.",
       "Ensures your portfolio is genuinely spread across different parts of the economy.",
       "risk"),
    _t("RISK-08", "Pair correlation cap",
       "If a candidate moves too similarly to a stock you already own, their combined size is capped at 15% of your account.",
       "Owning two stocks that move in lockstep is really owning one giant position.",
       "risk"),
    _t("RISK-10", "Macro sensitivity cap",
       "Tracks how sensitive the portfolio is to currency, interest rates, and commodities. Shown for information only until Milestone 10.",
       "", "risk"),
    _t("RISK-11", "Liquidity and trading history filter",
       "Requires at least Rs 5 crore daily trading volume, 3,000 daily trades, a share price above Rs 30, and 150 days of listing history.",
       "Makes sure you can always buy and sell easily without moving the price against yourself.",
       "risk"),
    _t("RISK-12", "Exchange surveillance filter",
       "Blocks stocks placed under exchange watchlists or trade-for-trade restrictions.",
       "Avoids stocks flagged by the exchange for unusual price manipulation or settlement risk.",
       "risk"),
    _t("RISK-13", "Valid trade levels check",
       "Checks that the stop-loss is strictly below the buy price and the profit target is above it.",
       "Catches broken price data before a trade can be sized.",
       "risk"),
    _t("RISK-14", "Daily realized loss breaker (2%)",
       "Halts new purchases if losses locked in today reach 2% of your account.",
       "Acts as a circuit breaker on a bad day.",
       "risk"),
    _t("RISK-16", "5-session loss breaker (4%)",
       "Pauses new entries for 3 sessions if the account drops 4% over a 5-session window.",
       "Forces a cooling-off period when the strategy is out of sync with the market.",
       "risk"),
    _t("RISK-17", "21-session loss breaker (7%)",
       "Pauses new entries if the account drops 7% over a 21-session month.",
       "Protects capital during sustained market downturns.",
       "risk"),
    _t("RISK-18", "Drawdown protection ladder",
       "Steps down position sizes as account drawdown reaches 8%, 12%, 15%, and 18%, and halts trading at 25%.",
       "Reduces risk automatically when losing, rather than doubling down.",
       "risk"),
    _t("RISK-20", "Losing streak counter",
       "Warns at 6 losses in a row, halves position size at 8, and halts new trades at 10 consecutive losses.",
       "Catches a broken strategy early before losses pile up.",
       "risk"),
    _t("RISK-22", "Expected portfolio loss",
       "Statistical estimate of portfolio tail risk. Shown for information only until 30 trades have resolved.",
       "", "risk"),

    # -------------------------------------------------- rejection reason codes
    _t("LIQUIDITY", "Too thinly traded or too new",
       "Rejected because daily trading volume, trade count, share price, or listing history fell below safety minimums.",
       "Thinly traded stocks are hard to exit when you need to sell.",
       "risk"),
    _t("SURVEILLANCE", "On exchange watch list",
       "Rejected because the exchange has placed this stock under surveillance or restricted settlement.",
       "", "risk"),
    _t("STOP_TOO_WIDE", "Stop-loss too far away",
       "Rejected because the stop-loss is more than 12.5% below the buy price.",
       "", "risk"),
    _t("MAX_POSITIONS", "Portfolio slots full",
       "Rejected because you already hold the maximum number of open positions.",
       "", "risk"),
    _t("INDUSTRY_NAME_CAP", "Industry slot full",
       "Rejected because you already hold the maximum allowed stocks in this industry.",
       "", "risk"),
    _t("PAIR_CORRELATION", "Moves too closely with an existing holding",
       "Rejected because this stock moves in lockstep with a stock you already own and no room remains under the combined cap.",
       "", "risk"),
    _t("PORTFOLIO_HEAT", "Total risk budget full",
       "Rejected because adding this trade would push total portfolio risk above the allowed ceiling.",
       "", "risk"),
    _t("CASH_FLOOR", "Would dip into minimum cash reserve",
       "Rejected because buying this stock would leave less cash than the safety floor requires.",
       "", "risk"),
    _t("BELOW_MIN_NOTIONAL", "Affordable size below Rs 50,000 minimum",
       "After applying risk and industry caps, the remaining allowed position was smaller than the Rs 50,000 minimum floor.",
       "The system refuses to open a tiny position where flat fees hurt your return.",
       "risk"),
    _t("RISK_CEILING_VS_FLOOR", "Cannot fit both loss limit and size floor",
       "Rounding to whole shares could not satisfy the Rs 50,000 minimum size and the 1% maximum loss ceiling at the same time.",
       "", "risk"),
    _t("HALT_DRAWDOWN", "New purchases paused by drawdown breaker",
       "The account is currently in a protective pause triggered by the drawdown ladder.",
       "", "risk"),
    _t("INVALID_GEOMETRY", "Invalid stop or target levels",
       "Rejected because the calculated stop-loss was not below the entry price.",
       "", "risk"),
    _t("DATA", "Missing price history",
       "Rejected because there are not enough historical trading days stored yet to calculate basic indicators for this stock.",
       "", "risk"),
    _t("NO_ATR", "Not enough days to measure daily move",
       "At least 14 trading sessions of price history are needed to measure a stock's typical daily range and set a stop-loss.",
       "", "risk"),
    _t("TRD-03", "Fading trend filter",
       "Blocks new trend-following purchases when a stock's upward trend is losing steam.",
       "", "risk"),
    _t("TREND_DECAYING", "Trend is losing momentum",
       "Rejected because the stock's 3-month trend is weakening rather than accelerating.",
       "", "risk"),
    _t("TRD-05", "Choppy direction filter",
       "Blocks trend-following purchases if the stock has flipped direction 6 or more times in the last 63 sessions.",
       "", "risk"),
    _t("WHIPSAW", "Too many direction reversals",
       "Rejected because the stock has repeatedly zig-zagged back and forth over the last quarter.",
       "", "risk"),

    # --------------------------------------- calibration, backtest & audit
    _t("resolution", "How well probabilities separate winners from losers",
       "Measures whether trades given a high chance of success actually succeeded more often than trades given a low chance.",
       "A system that gives every trade the same average chance has zero resolution.",
       "audit"),
    _t("bss", "Accuracy score versus a simple average",
       "Brier skill score: compares the accuracy of the system's probability estimates against simply guessing the long-run average every time. Above zero means genuine skill.",
       "", "audit"),
    _t("block_len", "Chunk size used in historical shuffle",
       "The number of consecutive trading days kept together when shuffling past returns to test future scenarios.",
       "Keeping days in chunks preserves real-world winning and losing streaks instead of assuming every day is a coin flip.",
       "audit"),
    _t("PSR", "Chance that the strategy's edge is real",
       "Probabilistic Sharpe Ratio: the statistical confidence (from 0% to 100%) that the strategy's risk-adjusted return is genuinely above zero and not just a lucky run.",
       "Requires at least 95% confidence before a strategy is trusted.",
       "audit"),
    _t("DSR", "Edge confidence after penalizing for multiple tests",
       "Deflated Sharpe Ratio: adjusts the confidence score downward to account for how many different strategy variations were tried before picking this one.",
       "If you test 100 random strategies, one will look great by pure luck. This number strips out that luck.",
       "audit"),
    _t("MinTRL", "Minimum days of history needed to prove the edge",
       "Minimum Track Record Length: how many trading sessions of live or backtested results are required before we can be 95% sure the performance is not luck.",
       "Tells you honestly when a track record is still too short to trust.",
       "audit"),
    _t("FROZEN_CONTROL", "Locked baseline strategy (v1)",
       "The original rule-based scoring weights locked in version 1 and verified by a digital fingerprint on every run.",
       "Every future improvement or AI model must beat this frozen baseline to prove it actually adds value.",
       "audit"),
    _t("LEDGER_CHAIN", "Tamper-evident trade log",
       "Every order, fill, and cash movement is linked to the previous event with a cryptographic hash, like a blockchain for your paper trades.",
       "Guarantees that past trades and results can never be silently edited or rewritten.",
       "audit"),
    _t("TRI", "Benchmark index including dividends",
       "Total Return Index: tracks the Nifty benchmark assuming all company dividends are reinvested.",
       "Comparing against a price-only index is unfair because real stockholders receive dividends. The system measures itself against the tougher total-return yardstick.",
       "audit"),
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
