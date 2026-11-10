"""Every capital-protection rule gets a test that makes it FIRE.

A risk rule without a failing-case test is a comment. The specific failure
this prevents: a predicate that silently evaluates to None (or compares
against a NULL column) never raises and never rejects, so the rule renders
green forever and is indistinguishable from one that simply never binds.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from ta_core.enums import Regime, RuleAction, RuleState
from ta_ledger.risk import (
    DEFAULT,
    Candidate,
    Portfolio,
    Position,
    evaluate_entry,
    regime_caps,
    rule_states,
)
from ta_quant.pipeline import run_nightly

EQ = 500_000.0


def flat() -> Portfolio:
    return Portfolio(equity=EQ, cash=EQ, watermark=EQ)


def good(symbol: str = "TCS", industry: str = "Information Technology",
         price: float = 1000.0, stop: float = 940.0) -> Candidate:
    """A candidate that passes everything, so each test varies ONE thing."""
    return Candidate(
        symbol=symbol, industry=industry, price=price, stop=stop,
        target=price * 1.15, adtv_rupees=5e8, n_trades=50_000,
        listed_sessions=900, series="EQ",
    )


def pos(symbol: str, industry: str, qty: int = 50, price: float = 1000.0) -> Position:
    return Position(symbol=symbol, industry=industry, qty=qty, entry=price,
                    last=price, stop=price * 0.94, opened=date(2026, 10, 1))


# --------------------------------------------------------------- the happy path
def test_clean_candidate_is_accepted_and_sized_on_risk():
    d = evaluate_entry(flat(), good(), regime=Regime.TREND_UP)
    assert d.allowed
    assert d.qty > 0
    # 0.8% of Rs 5,00,000 = Rs 4,000, minus whole-share rounding.
    assert 3_500 <= d.risk_rupees <= 4_000
    assert DEFAULT.min_notional <= d.notional <= DEFAULT.max_notional


# ------------------------------------------------------------------ each gate
def test_risk_03_rejects_a_stop_wider_than_12_5_percent():
    d = evaluate_entry(flat(), good(price=100.0, stop=80.0))
    assert d.action is RuleAction.REJECT
    assert d.rule_id == "RISK-03"


def test_risk_11_rejects_illiquidity_and_never_resizes_into_it():
    thin = Candidate("Y", "Realty", price=100.0, stop=95.0, target=115.0,
                     adtv_rupees=1e6, n_trades=100, listed_sessions=900)
    d = evaluate_entry(flat(), thin)
    assert d.action is RuleAction.REJECT
    assert d.rule_id == "RISK-11"
    assert d.qty == 0


def test_risk_11_rejects_a_newly_listed_name():
    d = evaluate_entry(flat(), Candidate(
        "NEW", "Realty", price=500.0, stop=470.0, target=600.0,
        adtv_rupees=5e8, n_trades=50_000, listed_sessions=20))
    assert d.rule_id == "RISK-11"


def test_risk_12_rejects_surveillance_and_non_eq_series():
    base = {"price": 1000.0, "stop": 940.0, "target": 1150.0,
            "adtv_rupees": 5e8, "n_trades": 50_000, "listed_sessions": 900}
    assert evaluate_entry(flat(), Candidate(
        "A", "Power", under_surveillance=True, **base)).rule_id == "RISK-12"
    assert evaluate_entry(flat(), Candidate(
        "A", "Power", series="BE", **base)).rule_id == "RISK-12"


def test_risk_13_rejects_a_stop_at_or_above_entry():
    d = evaluate_entry(flat(), good(price=100.0, stop=100.0))
    assert d.rule_id == "RISK-13"


def test_risk_05_rejects_the_eighth_position():
    """The book is SEVEN, not ten. This is the plan's number corrected."""
    full = Portfolio(
        equity=EQ, cash=EQ * 0.5, watermark=EQ,
        positions=tuple(pos(f"S{i}", f"IND{i}") for i in range(DEFAULT.max_positions)),
    )
    d = evaluate_entry(full, good())
    assert d.action is RuleAction.REJECT
    assert d.rule_id == "RISK-05"


def test_risk_06_rejects_a_third_name_in_one_industry():
    pf = Portfolio(equity=EQ, cash=EQ * 0.7, watermark=EQ,
                   positions=(pos("A", "Power"), pos("B", "Power")))
    d = evaluate_entry(pf, good(industry="Power"))
    assert d.action is RuleAction.REJECT
    assert d.rule_id == "RISK-06"


def test_risk_06_allows_a_third_financial_services_name():
    """Financial Services gets 3 names / 30%, because it is ~24% of the
    Nifty 200 by count and a 2-name cap would make the book structurally
    unable to track the index's largest sector."""
    pf = Portfolio(equity=EQ, cash=EQ * 0.7, watermark=EQ,
                   positions=(pos("A", "Financial Services"),
                              pos("B", "Financial Services")))
    d = evaluate_entry(pf, good(industry="Financial Services"))
    assert d.allowed


def test_risk_08_rejects_a_highly_correlated_pair_with_no_room():
    held = pos("HDFCBANK", "Financial Services", qty=80, price=1000.0)  # Rs 80k
    pf = Portfolio(equity=EQ, cash=EQ * 0.8, watermark=EQ, positions=(held,))
    c = Candidate("ICICIBANK", "Financial Services", price=1000.0, stop=940.0,
                  target=1150.0, adtv_rupees=5e8, n_trades=50_000,
                  listed_sessions=900, pair_corr={"HDFCBANK": 0.82})
    d = evaluate_entry(pf, c)
    assert d.action is RuleAction.REJECT
    assert d.rule_id == "RISK-08"


def test_risk_04_rejects_when_portfolio_heat_is_exhausted():
    # Six positions each risking ~1% of equity saturates the 6% heat cap.
    hot = tuple(
        Position(symbol=f"H{i}", industry=f"IND{i}", qty=100, entry=1000.0,
                 last=1000.0, stop=950.0, opened=date(2026, 10, 1))
        for i in range(6)
    )
    pf = Portfolio(equity=EQ, cash=EQ * 0.4, watermark=EQ, positions=hot)
    d = evaluate_entry(pf, good(), regime=Regime.TREND_UP)
    assert d.action is RuleAction.REJECT
    assert d.rule_id in {"RISK-04", "RISK-05"}


def test_risk_05_rejects_when_the_cash_floor_would_be_breached():
    pf = Portfolio(equity=EQ, cash=EQ * 0.09, watermark=EQ)
    d = evaluate_entry(pf, good(), regime=Regime.TREND_UP)
    assert d.rule_id == "RISK-05"
    assert "CASH" in d.reason


def test_risk_01_rejects_rather_than_rounding_up_to_the_floor():
    """Rounding up to reach the Rs 50,000 floor would silently breach the
    per-trade risk ceiling, which is the entire reason the floor exists."""
    pf = Portfolio(equity=EQ, cash=55_000.0, watermark=EQ)
    d = evaluate_entry(pf, good(), regime=Regime.TREND_UP)
    assert d.action is RuleAction.REJECT
    assert d.rule_id in {"RISK-01", "RISK-05"}


def test_halt_blocks_every_entry():
    d = evaluate_entry(flat(), good(), halted=True)
    assert d.action is RuleAction.REJECT
    assert d.rule_id == "RISK-18"


# ------------------------------------------------------------- regime gating
@pytest.mark.parametrize("regime", list(Regime))
def test_every_regime_has_caps_and_they_tighten_monotonically(regime: Regime):
    caps = regime_caps(regime)
    assert set(caps) == {"gross", "cash", "heat", "name", "new"}
    assert 0 < caps["gross"] <= 0.92
    assert abs(caps["gross"] + caps["cash"] - 1.0) < 1e-9


def test_high_vol_regime_sizes_smaller_than_trend_up():
    up = evaluate_entry(flat(), good(), regime=Regime.TREND_UP)
    hv = evaluate_entry(flat(), good(), regime=Regime.HIGH_VOL)
    assert hv.notional < up.notional


def test_unknown_regime_is_as_defensive_as_trend_down():
    assert regime_caps(Regime.UNKNOWN) == regime_caps(Regime.TREND_DOWN)


# ----------------------------------------------------- the dashboard contract
def test_rule_states_cover_every_rule_and_never_invent_an_assurance():
    states = rule_states(flat())
    ids = {s.id for s in states}
    assert {"RISK-04", "RISK-05", "RISK-07", "RISK-18", "RISK-20"} <= ids
    for s in states:
        assert isinstance(s.state, RuleState)
        # A rule with no earned statistics must say so, not render as OK.
        if s.id in {"RISK-22", "RISK-10"}:
            assert s.state is RuleState.DISPLAY_ONLY
            assert s.value is None


def test_heat_uses_the_current_stop_not_the_entry_stop():
    """A stop trailed above entry has locked in a gain and contributes no
    heat. Using the original stop keeps the book permanently hot and blocks
    entries it should allow."""
    trailed = Position("A", "Power", qty=100, entry=1000.0, last=1200.0,
                       stop=1100.0, opened=date(2026, 10, 1))
    pf = Portfolio(equity=EQ, cash=EQ * 0.8, watermark=EQ, positions=(trailed,))
    assert pf.heat == pytest.approx(10_000.0)   # (1200-1100)*100


def test_a_stop_above_the_last_price_contributes_zero_heat_not_negative():
    locked = Position("A", "Power", qty=100, entry=1000.0, last=1050.0,
                      stop=1100.0, opened=date(2026, 10, 1))
    pf = Portfolio(equity=EQ, cash=EQ, watermark=EQ, positions=(locked,))
    assert pf.heat == 0.0


def test_drawdown_is_measured_from_the_watermark_and_never_goes_negative():
    pf = Portfolio(equity=450_000.0, cash=0.0, watermark=500_000.0)
    assert pf.drawdown_pct == pytest.approx(0.10)
    up = Portfolio(equity=600_000.0, cash=0.0, watermark=500_000.0)
    assert up.drawdown_pct == 0.0


# ------------------------------------------------------- the composite result
def test_sequential_construction_actually_binds_the_industry_cap():
    """Sizing every candidate against the STARTING book is how a screener
    produces N cards that jointly breach every cap it claims to enforce."""
    pf = flat()
    accepted = []
    for i in range(6):
        c = good(symbol=f"FS{i}", industry="Financial Services")
        d = evaluate_entry(pf, c, regime=Regime.TREND_UP)
        if not d.allowed:
            break
        accepted.append(d)
        pf = Portfolio(
            equity=pf.equity, cash=pf.cash - d.notional, watermark=pf.watermark,
            positions=(*pf.positions,
                       pos(f"FS{i}", "Financial Services", qty=d.qty, price=1000.0)),
        )
    assert len(accepted) <= DEFAULT.finserv_max_names


def test_run_nightly_computes_pair_corr_and_triggers_risk_08():
    """Verify run_nightly computes pair_corr end-to-end and triggers RISK-08."""
    start = date(2026, 1, 1)
    dates: list[date] = []
    curr = start
    while len(dates) < 30:
        if curr.weekday() < 5:
            dates.append(curr)
        curr += timedelta(days=1)

    decision_d = dates[-1]
    as_of = datetime.combine(decision_d, datetime.min.time(), tzinfo=UTC) + timedelta(hours=18)

    bars_list: list[dict] = []
    p_a = 1000.0
    p_b = 1000.0
    p_m = 500.0

    for i, d in enumerate(dates):
        m = 0.01 if i % 2 == 0 else -0.01
        shock = 0.02 if i % 3 == 0 else -0.02
        r_a = m + shock
        r_b = m + shock
        r_m = m

        prev_a, p_a = p_a, p_a * (1.0 + r_a)
        prev_b, p_b = p_b, p_b * (1.0 + r_b)
        prev_m, p_m = p_m, p_m * (1.0 + r_m)

        kt = datetime.combine(d, datetime.min.time(), tzinfo=UTC) + timedelta(hours=18)
        for sym, price, prev_p in [("A", p_a, prev_a), ("B", p_b, prev_b), ("M", p_m, prev_m)]:
            bars_list.append({
                "symbol": sym,
                "d": d,
                "open": round(price * 0.998, 2),
                "high": round(price * 1.015, 2),
                "low": round(price * 0.985, 2),
                "close": round(price, 2),
                "prev_close": round(prev_p, 2),
                "volume": 200_000,
                "turnover": round(200_000.0 * price, 2),
                "n_trades": 20_000,
                "series": "EQ",
                "knowledge_time": kt,
            })

    bars = pl.DataFrame(bars_list).sort(["symbol", "d"])

    members = pl.DataFrame([
        {
            "symbol": sym,
            "industry": "Information Technology" if sym in ("A", "B") else "Power",
            "company_name": f"{sym} Corporation",
            "index_name": "NIFTY500",
            "d": decision_d,
            "knowledge_time": as_of,
        }
        for sym in ("A", "B", "M")
    ])

    # Position A has notional >= 75,000 on 500,000 equity (15% combined cap fully occupied)
    final_a_close = float(bars.filter((pl.col("symbol") == "A") & (pl.col("d") == decision_d))["close"][0])
    qty_a = int(75_000.0 // final_a_close) + 1
    held_a = Position(
        symbol="A",
        industry="Information Technology",
        qty=qty_a,
        entry=final_a_close,
        last=final_a_close,
        stop=round(final_a_close * 0.95, 2),
        opened=dates[0],
    )
    portfolio = Portfolio(
        equity=EQ,
        cash=EQ - held_a.notional,
        watermark=EQ,
        positions=(held_a,),
    )

    cfg = replace(DEFAULT, min_listed_sessions=20)
    res = run_nightly(bars, members, d=decision_d, portfolio=portfolio, cfg=cfg, as_of=as_of)

    # Candidate B must be rejected by RISK-08 due to correlation with held A
    assert not res.rejections.is_empty()
    b_rejections = res.rejections.filter(
        (pl.col("symbol") == "B") & (pl.col("rule_id") == "RISK-08")
    )
    assert b_rejections.height >= 1
    assert "PAIR_CORRELATION" in b_rejections["reason"].to_list()

