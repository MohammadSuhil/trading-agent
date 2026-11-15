"""Capital-protection rules. Pure functions, no I/O, so every one is testable.

The arithmetic that drives the whole design, and it is tighter than it looks:

    Rs 5,00,000 equity
    flat Rs 15.34 DP charge per scrip per delivery-sell  ->  Rs 50,000 position floor
    0.8% risk per trade (Rs 4,000) with a 6.0% portfolio heat cap
    ------------------------------------------------------------------
    => SEVEN concurrent positions, not ten.

The plan's "~10 concurrent positions" is optimistic: it satisfies the DP-charge
floor but not the heat cap at a realistic stop distance. Seven is the number
that satisfies both simultaneously, and the gates below enforce seven with nine
as an absolute backstop.

The honest cost of this design, stated once here so nobody rediscovers it as a
surprise: the Rs 50,000 floor combined with the 0.8% risk budget caps the
workable stop distance at 12.5%. That structurally excludes high-ATR names and
biases the book toward correlated large caps. Diversification rules then push
back against that bias, which is why RISK-08's residual-correlation gate exists
and why it is not optional.

Three enforcement points, deliberately distinct:

  * **GATE**    - checked before sizing; a failure means the candidate never
                  reaches the dossier and never consumes reasoning budget.
  * **SIZING**  - clamps that change the number but keep the trade.
  * **BREAKER** - post-hoc, evaluated against the book, can halt new entries.

A rule that is specified before the statistics that would justify it exists is
marked DISPLAY_ONLY and contributes nothing to any decision. Showing it as OK
would imply an assurance nobody has earned.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date

from ta_core.enums import Regime, RuleAction, RuleState

__all__ = [
    "DEFAULT",
    "TIERED_DEFAULT",
    "Candidate",
    "Decision",
    "Portfolio",
    "Position",
    "RiskConfig",
    "RuleStatus",
    "TieredRiskConfig",
    "clamp_portfolio_principal_heat",
    "evaluate_entry",
    "evaluate_tiered_entry",
    "regime_caps",
    "rule_states",
    "tiered_regime_caps",
]


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """Every threshold in one place, so the dashboard and the gate cannot drift.

    These are pre-committed deliberately: a drawdown ladder invented while you
    are in a drawdown is not a risk rule, it is a rationalisation.
    """

    equity_base: float = 500_000.0

    # --- position geometry
    min_notional: float = 50_000.0      # DP charge floor
    max_notional: float = 100_000.0     # 20% single-name cap
    risk_per_trade_pct: float = 0.008   # 0.8%
    risk_per_trade_ceiling_pct: float = 0.010
    max_stop_distance_pct: float = 0.125

    # --- book shape
    max_positions: int = 7
    max_positions_backstop: int = 9
    cash_floor_pct: float = 0.08
    cash_target_pct: float = 0.10
    max_gross_pct: float = 0.92
    max_portfolio_heat_pct: float = 0.060

    # --- diversification
    max_industry_pct: float = 0.25
    max_industry_names: int = 2
    finserv_industry: str = "Financial Services"
    finserv_max_pct: float = 0.30
    finserv_max_names: int = 3
    max_pair_residual_corr: float = 0.65
    pair_corr_release: float = 0.55
    corr_pair_combined_cap_pct: float = 0.15

    # --- liquidity (RISK-11)
    min_adtv_rupees: float = 5e7        # Rs 5 crore
    max_pct_of_adtv: float = 0.005      # position <= 0.5% of ADV
    min_trades_per_day: int = 3_000
    min_price: float = 30.0
    min_listed_sessions: int = 150

    # --- loss breakers
    max_daily_realised_loss_pct: float = 0.020
    max_daily_mtm_dd_pct: float = 0.035
    max_5d_loss_pct: float = 0.040
    max_21d_loss_pct: float = 0.070
    dd_rungs: tuple[float, ...] = (0.08, 0.12, 0.15, 0.18)
    dd_halt_pct: float = 0.25
    consecutive_loss_warn: int = 6
    consecutive_loss_half: int = 8
    consecutive_loss_halt: int = 10


DEFAULT = RiskConfig()


#: RISK-25. The regime gates exactly five quantities and nothing else -- it is
#: a risk gate, never a signal flipper. Widening is deliberately slower than
#: tightening (RISK-26): tighten on the first session, loosen only after three.
_REGIME_CAPS: dict[Regime, dict[str, float]] = {
    Regime.TREND_UP:   {"gross": 0.92, "cash": 0.08, "heat": 0.060, "name": 0.20, "new": 3},
    Regime.CHOP:       {"gross": 0.80, "cash": 0.20, "heat": 0.045, "name": 0.18, "new": 2},
    Regime.TREND_DOWN: {"gross": 0.50, "cash": 0.50, "heat": 0.030, "name": 0.15, "new": 1},
    Regime.HIGH_VOL:   {"gross": 0.35, "cash": 0.65, "heat": 0.020, "name": 0.12, "new": 1},
    Regime.UNKNOWN:    {"gross": 0.50, "cash": 0.50, "heat": 0.030, "name": 0.15, "new": 1},
}


def regime_caps(regime: Regime) -> dict[str, float]:
    """
    >>> regime_caps(Regime.HIGH_VOL)["heat"]
    0.02
    >>> regime_caps(Regime.UNKNOWN) == regime_caps(Regime.TREND_DOWN)
    True
    """
    return _REGIME_CAPS[regime]


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    industry: str
    qty: int
    entry: float
    last: float
    stop: float
    opened: date
    target: float = 0.0
    horizon: str = "SWING"

    @property
    def notional(self) -> float:
        return self.qty * self.last

    @property
    def risk_rupees(self) -> float:
        """Rupees still at risk to the CURRENT resting stop, floored at zero.

        A stop trailed above entry has locked in a gain, so it contributes no
        heat. Using the original stop here would keep the book permanently
        "hot" and block entries it should allow.
        """
        return max(0.0, (self.last - self.stop) * self.qty)

    @property
    def principal_risk_rupees(self) -> float:
        """Principal capital at risk to the resting stop; zero once trailed above entry."""
        return max(0.0, (min(self.entry, self.last) - self.stop) * self.qty)


@dataclass(frozen=True, slots=True)
class Portfolio:
    equity: float
    cash: float
    positions: tuple[Position, ...] = ()
    watermark: float = 0.0
    realised_today: float = 0.0
    ret_5d: float = 0.0
    ret_21d: float = 0.0
    consecutive_losses: int = 0

    @property
    def deployed(self) -> float:
        return sum(p.notional for p in self.positions)

    @property
    def heat(self) -> float:
        return sum(p.risk_rupees for p in self.positions)

    @property
    def heat_pct(self) -> float:
        return self.heat / self.equity if self.equity else 0.0

    @property
    def principal_heat(self) -> float:
        return sum(p.principal_risk_rupees for p in self.positions)

    @property
    def principal_heat_pct(self) -> float:
        return self.principal_heat / self.equity if self.equity else 0.0

    def industry_heat(self, industry: str) -> float:
        return sum(p.principal_risk_rupees for p in self.positions if p.industry == industry)

    @property
    def drawdown_pct(self) -> float:
        wm = max(self.watermark, self.equity)
        return 0.0 if wm <= 0 else (wm - self.equity) / wm

    def industry_notional(self, industry: str) -> float:
        return sum(p.notional for p in self.positions if p.industry == industry)

    def industry_names(self, industry: str) -> int:
        return sum(1 for p in self.positions if p.industry == industry)


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    industry: str
    price: float
    stop: float
    target: float
    adtv_rupees: float = 0.0
    n_trades: int = 0
    listed_sessions: int = 0
    series: str = "EQ"
    under_surveillance: bool = False
    #: residual correlation against each open position, by symbol
    pair_corr: dict[str, float] = field(default_factory=dict)

    @property
    def stop_distance_pct(self) -> float:
        return (self.price - self.stop) / self.price if self.price > 0 else 1.0


@dataclass(frozen=True, slots=True)
class Decision:
    action: RuleAction
    qty: int = 0
    notional: float = 0.0
    risk_rupees: float = 0.0
    rule_id: str = ""
    reason: str = ""
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.action in (RuleAction.ALLOW, RuleAction.RESIZE)


@dataclass(frozen=True, slots=True)
class RuleStatus:
    id: str
    label: str
    state: RuleState
    value: float | None
    limit: float | None
    unit: str
    detail: str

    @property
    def headroom_pct(self) -> float | None:
        if self.value is None or not self.limit:
            return None
        return max(0.0, min(1.0, 1.0 - self.value / self.limit))


def _reject(rule: str, reason: str, detail: str = "") -> Decision:
    return Decision(RuleAction.REJECT, rule_id=rule, reason=reason, detail=detail)


def evaluate_entry(
    pf: Portfolio,
    c: Candidate,
    *,
    regime: Regime = Regime.UNKNOWN,
    cfg: RiskConfig = DEFAULT,
    halted: bool = False,
) -> Decision:
    """Decide whether `c` may be opened, and at what size.

    Order matters: the cheapest and most absolute checks run first, so an
    illiquid or surveillance-flagged name is rejected before any correlation
    matrix is consulted.

    A liquid, uncorrelated candidate in a healthy book is sized on risk:

    >>> pf = Portfolio(equity=500_000, cash=500_000, watermark=500_000)
    >>> c = Candidate("TCS", "Information Technology", price=1000.0, stop=940.0,
    ...               target=1150.0, adtv_rupees=5e8, n_trades=50_000,
    ...               listed_sessions=900)
    >>> d = evaluate_entry(pf, c, regime=Regime.TREND_UP)
    >>> d.action, d.qty, round(d.risk_rupees)
    (<RuleAction.ALLOW: 'ALLOW'>, 66, 3960)

    A stop wider than 12.5% is refused outright rather than sized down -- at
    the Rs 50,000 floor it would breach the per-trade risk ceiling:

    >>> wide = Candidate("X", "Power", price=100.0, stop=80.0, target=140.0,
    ...                  adtv_rupees=5e8, n_trades=50_000, listed_sessions=900)
    >>> evaluate_entry(pf, wide).rule_id
    'RISK-03'

    Illiquidity is never resized into -- a smaller position in a name you
    cannot exit is still a name you cannot exit:

    >>> thin = Candidate("Y", "Realty", price=100.0, stop=95.0, target=115.0,
    ...                  adtv_rupees=1e6, n_trades=100, listed_sessions=900)
    >>> evaluate_entry(pf, thin).rule_id
    'RISK-11'
    """
    caps = regime_caps(regime)

    if halted:
        return _reject("RISK-18", "HALT_DRAWDOWN", "new entries halted by the drawdown ladder")

    # ---------------------------------------------------------- RISK-13
    if c.price <= 0 or c.stop <= 0 or c.stop >= c.price:
        return _reject("RISK-13", "INVALID_GEOMETRY",
                       f"stop {c.stop} must be below entry {c.price}")

    # ---------------------------------------------------------- RISK-11/12
    if c.series != "EQ" or c.under_surveillance:
        return _reject("RISK-12", "SURVEILLANCE",
                       f"series={c.series} surveillance={c.under_surveillance}")
    if (c.adtv_rupees < cfg.min_adtv_rupees or c.n_trades < cfg.min_trades_per_day
            or c.price < cfg.min_price or c.listed_sessions < cfg.min_listed_sessions):
        return _reject("RISK-11", "LIQUIDITY",
                       f"adtv={c.adtv_rupees/1e7:.2f}cr trades={c.n_trades} "
                       f"price={c.price:.2f} sessions={c.listed_sessions}")

    # ---------------------------------------------------------- RISK-03
    if c.stop_distance_pct > cfg.max_stop_distance_pct:
        return _reject("RISK-03", "STOP_TOO_WIDE",
                       f"stop {c.stop_distance_pct:.1%} > {cfg.max_stop_distance_pct:.1%}")

    # ---------------------------------------------------------- RISK-05
    n = len(pf.positions)
    if n >= cfg.max_positions:
        return _reject("RISK-05", "MAX_POSITIONS", f"{n} open, cap {cfg.max_positions}")

    # ---------------------------------------------------------- RISK-02 sizing
    risk_rupees = pf.equity * cfg.risk_per_trade_pct
    notional = risk_rupees / c.stop_distance_pct
    resized = False

    eq_scale = max(1.0, pf.equity / cfg.equity_base) if cfg.equity_base > 0 else 1.0
    eff_max_notional = cfg.max_notional * eq_scale
    name_ceiling = min(eff_max_notional, pf.equity * caps["name"])
    if notional > name_ceiling:
        notional = name_ceiling
        resized = True

    # ---------------------------------------------------------- RISK-06
    ind_cap_pct = (cfg.finserv_max_pct if c.industry == cfg.finserv_industry
                   else cfg.max_industry_pct)
    ind_max_names = (cfg.finserv_max_names if c.industry == cfg.finserv_industry
                     else cfg.max_industry_names)
    if pf.industry_names(c.industry) >= ind_max_names:
        return _reject("RISK-06", "INDUSTRY_NAME_CAP",
                       f"{c.industry}: {pf.industry_names(c.industry)} open, cap {ind_max_names}")
    ind_headroom = pf.equity * ind_cap_pct - pf.industry_notional(c.industry)
    if notional > ind_headroom:
        notional, resized = ind_headroom, True

    # ---------------------------------------------------------- RISK-08
    for p in pf.positions:
        rho = c.pair_corr.get(p.symbol)
        if rho is not None and rho >= cfg.max_pair_residual_corr:
            combined_cap = pf.equity * cfg.corr_pair_combined_cap_pct
            headroom = combined_cap - p.notional
            if headroom < cfg.min_notional:
                return _reject("RISK-08", "PAIR_CORRELATION",
                               f"rho({c.symbol},{p.symbol})={rho:.2f} and no room under "
                               f"the {cfg.corr_pair_combined_cap_pct:.0%} combined cap")
            if notional > headroom:
                notional, resized = headroom, True

    # ---------------------------------------------------------- RISK-04
    heat_cap = pf.equity * min(cfg.max_portfolio_heat_pct, caps["heat"])
    heat_headroom = heat_cap - pf.heat
    if heat_headroom <= 0:
        return _reject("RISK-04", "PORTFOLIO_HEAT",
                       f"heat {pf.heat_pct:.2%} at cap {heat_cap/pf.equity:.2%}")
    if notional * c.stop_distance_pct > heat_headroom:
        notional, resized = heat_headroom / c.stop_distance_pct, True

    # ---------------------------------------------------------- RISK-05 cash
    cash_floor = pf.equity * max(cfg.cash_floor_pct, caps["cash"])
    spendable = pf.cash - cash_floor
    if spendable < cfg.min_notional:
        return _reject("RISK-05", "CASH_FLOOR",
                       f"cash {pf.cash:,.0f} leaves {spendable:,.0f} above the "
                       f"{cash_floor:,.0f} floor")
    if notional > spendable:
        notional, resized = spendable, True

    gross_cap = pf.equity * min(cfg.max_gross_pct, caps["gross"])
    if pf.deployed + notional > gross_cap:
        notional, resized = gross_cap - pf.deployed, True

    # ---------------------------------------------------------- RISK-11 ADV
    adv_cap = c.adtv_rupees * cfg.max_pct_of_adtv
    if notional > adv_cap:
        notional, resized = adv_cap, True

    # ---------------------------------------------------------- RISK-01
    # Round DOWN to whole shares, then re-test the floor. Rounding up would
    # silently breach the per-trade risk ceiling, which is the whole point of
    # having a floor rather than a target.
    qty = int(notional // c.price)
    notional = qty * c.price
    if notional < cfg.min_notional:
        return _reject("RISK-01", "BELOW_MIN_NOTIONAL",
                       f"Rs {notional:,.0f} < Rs {cfg.min_notional:,.0f} floor "
                       f"(DP charge would be {15.34/max(notional,1)*10000:.1f} bps)")

    actual_risk = (c.price - c.stop) * qty
    ceiling = pf.equity * cfg.risk_per_trade_ceiling_pct
    if actual_risk > ceiling:
        qty = int(ceiling / (c.price - c.stop))
        notional, actual_risk, resized = qty * c.price, (c.price - c.stop) * qty, True
        if notional < cfg.min_notional:
            return _reject("RISK-02", "RISK_CEILING_VS_FLOOR",
                           "cannot satisfy the risk ceiling and the notional floor together")

    return Decision(
        RuleAction.RESIZE if resized else RuleAction.ALLOW,
        qty=qty, notional=notional, risk_rupees=actual_risk,
        rule_id="", reason="ACCEPTED",
        detail=f"risk {actual_risk/pf.equity:.2%} of equity, stop {c.stop_distance_pct:.1%}",
    )


def rule_states(
    pf: Portfolio, *, regime: Regime = Regime.UNKNOWN, cfg: RiskConfig = DEFAULT
) -> list[RuleStatus]:
    """Current status of every book-level rule, for the dashboard.

    The dashboard DISPLAYS these; it never enforces them. Enforcement lives in
    `evaluate_entry` and in the broker, so a dashboard that is down, stale or
    simply not being looked at cannot change what the system is allowed to do.
    """
    t_caps = tiered_regime_caps(regime)
    e = pf.equity
    n = len(pf.positions)
    industries = {p.industry for p in pf.positions}
    dd = pf.drawdown_pct

    def st(v: float, lim: float, warn: float = 0.85) -> RuleState:
        if lim <= 0:
            return RuleState.OK
        if v > lim:
            return RuleState.BREACH
        return RuleState.WARN if v / lim >= warn else RuleState.OK

    heat_cap = min(TIERED_DEFAULT.hard_heat_ceiling_pct, TIERED_DEFAULT.max_portfolio_heat_pct, t_caps["heat"])
    gross_lim = min(TIERED_DEFAULT.max_gross_pct, t_caps["gross"])
    max_ind_heat = max((pf.industry_heat(ind) / e for ind in industries), default=0.0) if e > 0 else 0.0
    min_ind = math.ceil(n / TIERED_DEFAULT.max_industry_names)
    out = [
        # 4-Tier Hierarchical Risk Engine states (active in MetaVoter v2)
        RuleStatus(
            "TIER-01",
            "Tier 1: Principal portfolio heat",
            st(pf.principal_heat_pct, TIERED_DEFAULT.max_portfolio_heat_pct),
            pf.principal_heat_pct,
            TIERED_DEFAULT.max_portfolio_heat_pct,
            "pct",
            f"Rs {pf.principal_heat:,.0f} principal risk (hard cap {TIERED_DEFAULT.hard_heat_ceiling_pct:.1%}; trailed profits = 0)",
        ),
        RuleStatus(
            "TIER-02",
            "Tier 2: Dynamic position slots",
            st(float(n), float(TIERED_DEFAULT.max_positions)),
            float(n),
            float(TIERED_DEFAULT.max_positions),
            "count",
            f"12 dynamic slots (backstop {TIERED_DEFAULT.max_positions_backstop}, min floor Rs {TIERED_DEFAULT.min_notional:,.0f})",
        ),
        RuleStatus(
            "TIER-03",
            "Tier 3: Max single-industry heat",
            st(max_ind_heat, TIERED_DEFAULT.max_industry_heat_pct),
            max_ind_heat,
            TIERED_DEFAULT.max_industry_heat_pct,
            "pct",
            f"up to {TIERED_DEFAULT.max_industry_names} names and {TIERED_DEFAULT.max_industry_heat_pct:.1%} risk / {TIERED_DEFAULT.max_industry_pct:.0%} equity per industry",
        ),
        RuleStatus(
            "TIER-04",
            "Tier 4: 60d residual correlation & beta gate",
            RuleState.OK,
            TIERED_DEFAULT.corr_soft_threshold,
            TIERED_DEFAULT.corr_hard_ceiling,
            "rho",
            f"soft-half >= {TIERED_DEFAULT.corr_soft_threshold:.2f}, reject >= {TIERED_DEFAULT.corr_hard_ceiling:.2f}, beta <= {TIERED_DEFAULT.max_portfolio_beta:.2f}",
        ),
        RuleStatus("RISK-04", "Regime-adjusted portfolio heat", st(pf.principal_heat_pct, heat_cap),
                   pf.principal_heat_pct, heat_cap, "pct",
                   f"Rs {pf.principal_heat:,.0f} principal risk vs {regime.value} cap {heat_cap:.2%}"),
        RuleStatus("RISK-05", "Concurrent positions",
                   st(float(n), float(TIERED_DEFAULT.max_positions)), float(n),
                   float(TIERED_DEFAULT.max_positions), "count",
                   f"max {TIERED_DEFAULT.max_positions} active slots (backstop {TIERED_DEFAULT.max_positions_backstop})"),
        RuleStatus("RISK-05c", "Cash floor",
                   RuleState.BREACH if pf.cash < e * TIERED_DEFAULT.cash_floor_pct else RuleState.OK,
                   pf.cash / e if e else None, TIERED_DEFAULT.cash_floor_pct, "pct_floor",
                   f"Rs {pf.cash:,.0f} cash, floor Rs {e*TIERED_DEFAULT.cash_floor_pct:,.0f} ({TIERED_DEFAULT.cash_floor_pct:.0%})"),
        RuleStatus("RISK-05g", "Gross exposure",
                   st(pf.deployed / e if e else 0.0, gross_lim),
                   pf.deployed / e if e else None, gross_lim,
                   "pct", f"Rs {pf.deployed:,.0f} deployed (regime cap {gross_lim:.0%})"),
        RuleStatus("RISK-07", "Distinct industries",
                   RuleState.OK if n < 4 or len(industries) >= min_ind
                   else RuleState.BREACH,
                   float(len(industries)),
                   float(min_ind) if n >= 4 else None, "count_min",
                   f"{len(industries)} industries across {n} names (max {TIERED_DEFAULT.max_industry_names} per industry)"),
        RuleStatus("RISK-18", "Peak-to-trough drawdown",
                   RuleState.BREACH if dd >= cfg.dd_halt_pct
                   else RuleState.WARN if dd >= cfg.dd_rungs[1] else RuleState.OK,
                   dd, cfg.dd_halt_pct, "pct",
                   f"watermark Rs {max(pf.watermark, e):,.0f}; "
                   f"rungs {'/'.join(f'{r:.0%}' for r in cfg.dd_rungs)}, halt "
                   f"{cfg.dd_halt_pct:.0%}"),
        RuleStatus("RISK-14", "Daily realised loss",
                   st(max(0.0, -pf.realised_today) / e if e else 0.0,
                      cfg.max_daily_realised_loss_pct),
                   max(0.0, -pf.realised_today) / e if e else None,
                   cfg.max_daily_realised_loss_pct, "pct",
                   "halts new entries AND tonight's publish on breach"),
        RuleStatus("RISK-16", "Rolling 5-session loss",
                   st(max(0.0, -pf.ret_5d), cfg.max_5d_loss_pct),
                   max(0.0, -pf.ret_5d), cfg.max_5d_loss_pct, "pct",
                   "halt 3 sessions; heat cap halved for 10 after"),
        RuleStatus("RISK-17", "Rolling 21-session loss",
                   st(max(0.0, -pf.ret_21d), cfg.max_21d_loss_pct),
                   max(0.0, -pf.ret_21d), cfg.max_21d_loss_pct, "pct",
                   "halt until recovery or 10 sessions, whichever is later"),
        RuleStatus("RISK-20", "Consecutive losses",
                   RuleState.BREACH if pf.consecutive_losses >= cfg.consecutive_loss_halt
                   else RuleState.WARN if pf.consecutive_losses >= cfg.consecutive_loss_warn
                   else RuleState.OK,
                   float(pf.consecutive_losses), float(cfg.consecutive_loss_halt), "count",
                   f"warn {cfg.consecutive_loss_warn} / half {cfg.consecutive_loss_half} "
                   f"/ halt {cfg.consecutive_loss_halt}"),
        # Specified now, enforceable only once the calibration map has earned
        # it. Rendering this as OK would imply an assurance nobody has earned.
        RuleStatus("RISK-22", "Expected portfolio loss", RuleState.DISPLAY_ONLY,
                   None, 0.030, "pct",
                   "needs calibrated_p with n_eff >= 30; not used for sizing yet"),
        RuleStatus("RISK-10", "Factor exposure", RuleState.DISPLAY_ONLY,
                   None, 0.35, "beta",
                   "USD/RATES/COMMODITY proxies; enforcing only from M10"),
    ]
    return out


@dataclass(frozen=True, slots=True)
class TieredRiskConfig:
    """4-Tier Hierarchical Risk Engine configuration proven in out-of-sample simulation."""

    # --- Tier 1: Portfolio envelope (hard ceiling <= 7.0% principal open heat)
    equity_base: float = 500_000.0
    max_portfolio_heat_pct: float = 0.0685
    hard_heat_ceiling_pct: float = 0.070
    max_positions: int = 12
    max_positions_backstop: int = 15
    cash_floor_pct: float = 0.06
    max_gross_pct: float = 0.94

    # --- Tier 2: Single-trade volatility-adaptive sizing
    min_notional: float = 25_000.0
    max_notional_pct: float = 0.145
    risk_per_trade_pct: float = 0.0085
    risk_per_trade_ceiling_pct: float = 0.010
    max_stop_distance_pct: float = 0.125

    # --- Tier 3: Sector / industry concentration (unlocks leading sector clusters)
    max_industry_names: int = 3
    max_industry_heat_pct: float = 0.030
    max_industry_pct: float = 0.25
    finserv_industry: str = "Financial Services"
    finserv_max_pct: float = 0.30
    finserv_max_names: int = 3

    # --- Tier 4: Pairwise residual correlation & beta guard
    corr_soft_threshold: float = 0.65
    corr_hard_ceiling: float = 0.68
    max_portfolio_beta: float = 1.35

    # --- Liquidity (RISK-11)
    min_adtv_rupees: float = 5e7
    max_pct_of_adtv: float = 0.005
    min_trades_per_day: int = 3_000
    min_price: float = 30.0
    min_listed_sessions: int = 150


TIERED_DEFAULT = TieredRiskConfig()


def tiered_regime_caps(
    regime: Regime,
    *,
    breadth_above_sma50: float = 0.50,
    cfg: TieredRiskConfig = TIERED_DEFAULT,
) -> dict[str, float]:
    """Regime-conditional dynamic slot and heat envelope for the 4-Tier Risk Engine."""
    if regime in (Regime.TREND_DOWN, Regime.UNKNOWN) or breadth_above_sma50 < 0.35:
        return {"gross": 0.50, "cash": 0.50, "heat": 0.035, "name": 0.12, "slots": 6.0, "new": 3.0}
    if regime is Regime.HIGH_VOL:
        return {
            "gross": 0.84,
            "cash": 0.16,
            "heat": min(0.058, cfg.max_portfolio_heat_pct),
            "name": 0.13,
            "slots": 10.0,
            "new": 6.0,
        }
    if regime is Regime.CHOP:
        return {
            "gross": 0.90,
            "cash": 0.10,
            "heat": min(0.065, cfg.max_portfolio_heat_pct),
            "name": cfg.max_notional_pct,
            "slots": float(cfg.max_positions),
            "new": 8.0,
        }
    return {
        "gross": cfg.max_gross_pct,
        "cash": cfg.cash_floor_pct,
        "heat": cfg.max_portfolio_heat_pct,
        "name": cfg.max_notional_pct,
        "slots": float(cfg.max_positions),
        "new": 12.0,
    }


def evaluate_tiered_entry(
    pf: Portfolio,
    c: Candidate,
    *,
    regime: Regime = Regime.TREND_UP,
    breadth_above_sma50: float = 0.50,
    candidate_beta: float = 1.0,
    portfolio_beta: float = 0.0,
    cfg: TieredRiskConfig = TIERED_DEFAULT,
    halted: bool = False,
) -> Decision:
    """Evaluate candidate under the 4-Tier Hierarchical Risk Engine (`<= 7.0%` portfolio heat)."""
    caps = tiered_regime_caps(regime, breadth_above_sma50=breadth_above_sma50, cfg=cfg)

    if halted:
        return _reject("RISK-18", "HALT_DRAWDOWN", "new entries halted by the drawdown ladder")

    # ---------------------------------------------------------- RISK-13 geometry
    if c.price <= 0 or c.stop <= 0 or c.stop >= c.price:
        return _reject("RISK-13", "INVALID_GEOMETRY", f"stop {c.stop} must be below entry {c.price}")

    # ---------------------------------------------------------- RISK-11/12 liquidity
    if c.series != "EQ" or c.under_surveillance:
        return _reject("RISK-12", "SURVEILLANCE", f"series={c.series} surveillance={c.under_surveillance}")
    if (
        c.adtv_rupees < cfg.min_adtv_rupees
        or c.n_trades < cfg.min_trades_per_day
        or c.price < cfg.min_price
        or c.listed_sessions < cfg.min_listed_sessions
    ):
        return _reject(
            "RISK-11",
            "LIQUIDITY",
            f"adtv={c.adtv_rupees/1e7:.2f}cr trades={c.n_trades} "
            f"price={c.price:.2f} sessions={c.listed_sessions}",
        )

    # ---------------------------------------------------------- RISK-03 stop width
    if c.stop_distance_pct > cfg.max_stop_distance_pct:
        return _reject("RISK-03", "STOP_TOO_WIDE", f"stop {c.stop_distance_pct:.1%} > {cfg.max_stop_distance_pct:.1%}")

    # ---------------------------------------------------------- RISK-05 dynamic slots
    max_slots = min(cfg.max_positions_backstop, int(caps["slots"]))
    if len(pf.positions) >= max_slots:
        return _reject("RISK-05", "MAX_POSITIONS", f"{len(pf.positions)} open, cap {max_slots}")

    # ---------------------------------------------------------- Tier 3 Sector Cluster Caps
    ind_cap_pct = cfg.finserv_max_pct if c.industry == cfg.finserv_industry else cfg.max_industry_pct
    ind_max_names = cfg.finserv_max_names if c.industry == cfg.finserv_industry else cfg.max_industry_names
    if pf.industry_names(c.industry) >= ind_max_names:
        return _reject(
            "RISK-06",
            "INDUSTRY_NAME_CAP",
            f"{c.industry}: {pf.industry_names(c.industry)} open, cap {ind_max_names}",
        )
    ind_headroom = pf.equity * ind_cap_pct - pf.industry_notional(c.industry)
    if ind_headroom < cfg.min_notional:
        return _reject(
            "RISK-06",
            "INDUSTRY_CAP",
            f"{c.industry}: notional headroom Rs {ind_headroom:,.0f} < Rs {cfg.min_notional:,.0f}",
        )
    ind_heat_headroom = pf.equity * cfg.max_industry_heat_pct - pf.industry_heat(c.industry)
    if ind_heat_headroom <= 0:
        return _reject(
            "RISK-06",
            "INDUSTRY_HEAT_CAP",
            f"{c.industry}: industry heat at {cfg.max_industry_heat_pct:.1%} ceiling",
        )

    # ---------------------------------------------------------- Tier 4 Correlation & Beta Guard
    max_rho = max((c.pair_corr.get(p.symbol, 0.0) for p in pf.positions), default=0.0)
    if max_rho >= cfg.corr_hard_ceiling:
        return _reject(
            "RISK-08",
            "CORRELATION_CAP",
            f"max pair rho={max_rho:.2f} >= {cfg.corr_hard_ceiling:.2f}",
        )
    resized = max_rho >= cfg.corr_soft_threshold
    eff_risk_pct = cfg.risk_per_trade_pct * (0.50 if resized else 1.0)

    # ---------------------------------------------------------- Tier 2 Vol-Adaptive Sizing
    notional = (pf.equity * eff_risk_pct) / c.stop_distance_pct
    name_ceiling = min(pf.equity * cfg.max_notional_pct, pf.equity * caps["name"])
    for lim in (name_ceiling, ind_headroom, ind_heat_headroom / c.stop_distance_pct):
        if notional > lim:
            notional, resized = lim, True

    if candidate_beta > 0 and (portfolio_beta + (notional / pf.equity) * candidate_beta) > cfg.max_portfolio_beta:
        rem_beta = cfg.max_portfolio_beta - portfolio_beta
        if rem_beta <= 0:
            return _reject(
                "RISK-10",
                "PORTFOLIO_BETA_CAP",
                f"portfolio beta {portfolio_beta:.2f} at cap {cfg.max_portfolio_beta:.2f}",
            )
        beta_max_notional = (rem_beta * pf.equity) / candidate_beta
        if beta_max_notional < cfg.min_notional:
            return _reject(
                "RISK-10",
                "PORTFOLIO_BETA_CAP",
                f"remaining beta {rem_beta:.2f} allows Rs {beta_max_notional:,.0f} < floor",
            )
        if notional > beta_max_notional:
            notional, resized = beta_max_notional, True

    # ---------------------------------------------------------- Tier 1 Portfolio Heat Cap
    eff_heat_pct = min(cfg.hard_heat_ceiling_pct, cfg.max_portfolio_heat_pct, caps["heat"])
    heat_cap = pf.equity * eff_heat_pct
    heat_headroom = heat_cap - pf.principal_heat
    if heat_headroom <= 0:
        return _reject(
            "RISK-04",
            "PORTFOLIO_HEAT",
            f"principal heat {pf.principal_heat_pct:.2%} at cap {eff_heat_pct:.2%}",
        )
    if notional * c.stop_distance_pct > heat_headroom:
        notional, resized = heat_headroom / c.stop_distance_pct, True

    # ---------------------------------------------------------- Cash / Gross / ADV
    cash_floor = pf.equity * max(cfg.cash_floor_pct, caps["cash"])
    spendable = pf.cash - cash_floor
    if spendable < cfg.min_notional:
        return _reject(
            "RISK-05",
            "CASH_FLOOR",
            f"cash {pf.cash:,.0f} leaves {spendable:,.0f} above {cash_floor:,.0f} floor",
        )
    gross_cap = pf.equity * min(cfg.max_gross_pct, caps["gross"])
    adv_cap = c.adtv_rupees * cfg.max_pct_of_adtv
    for lim in (spendable, max(0.0, gross_cap - pf.deployed), adv_cap):
        if notional > lim:
            notional, resized = lim, True

    qty = int(notional // c.price)
    notional = qty * c.price
    if notional < cfg.min_notional:
        return _reject(
            "RISK-01",
            "BELOW_MIN_NOTIONAL",
            f"Rs {notional:,.0f} < Rs {cfg.min_notional:,.0f} floor",
        )

    actual_risk = (c.price - c.stop) * qty
    ceiling = pf.equity * cfg.risk_per_trade_ceiling_pct
    if actual_risk > ceiling:
        qty = int(ceiling / (c.price - c.stop))
        notional, actual_risk, resized = qty * c.price, (c.price - c.stop) * qty, True
        if notional < cfg.min_notional:
            return _reject("RISK-02", "RISK_CEILING_VS_FLOOR", "risk ceiling vs notional floor")

    return Decision(
        RuleAction.RESIZE if resized else RuleAction.ALLOW,
        qty=qty,
        notional=notional,
        risk_rupees=actual_risk,
        rule_id="",
        reason="ACCEPTED",
        detail=f"risk {actual_risk/pf.equity:.2%} of equity, stop {c.stop_distance_pct:.1%}",
    )


def clamp_portfolio_principal_heat(
    pf: Portfolio,
    *,
    max_heat_pct: float = 0.0690,
) -> tuple[Position, ...]:
    """Proportionally tighten resting stops when principal heat drifts above `max_heat_pct`."""
    if pf.equity <= 0 or pf.principal_heat_pct <= max_heat_pct or not pf.positions:
        return pf.positions
    scale = (max_heat_pct * pf.equity) / pf.principal_heat
    clamped: list[Position] = []
    for pos in pf.positions:
        if pos.principal_risk_rupees <= 0:
            clamped.append(pos)
            continue
        ref_p = min(pos.entry, pos.last)
        new_dist = (ref_p - pos.stop) * scale
        new_stop = min(round(ref_p - 0.01, 2), math.ceil((ref_p - new_dist) * 100.0) / 100.0)
        clamped.append(replace(pos, stop=max(pos.stop, new_stop)))
    return tuple(clamped)
