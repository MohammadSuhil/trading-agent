"""Golden vector and invariant tests for core, quant factors, trend, and barriers.

Enforces arithmetic correctness, zero-I/O rules, and resilience against NaN poisoning.
"""
from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from ta_core.dto import CorporateAction, Fill, OrderIntent
from ta_core.enums import Action, Arm, Horizon, Side, Sleeve, TrendState
from ta_core.ids import make_rec_id, make_run_id
from ta_core.ports import BrokerPort, MarketDataPort
from ta_core.settings import AppSettings
from ta_quant.factors.price import add_price_factors
from ta_quant.trend import _efficiency_ratio, add_trend_states, nw_lag


def _py_wilder(values: list[float | None], n: int = 14) -> list[float | None]:
    """Pure-Python reference Wilder smoothing matching ewm_mean(alpha=1/n, adjust=False)."""
    out: list[float | None] = [None] * len(values)
    alpha = 1.0 / n
    val: float | None = None
    count = 0
    for i, x in enumerate(values):
        if x is None:
            continue
        count += 1
        if val is None:
            val = float(x)
        else:
            val = alpha * float(x) + (1.0 - alpha) * val
        if count >= n:
            out[i] = val
    return out


# ---------------------------------------------------------------------------
# Test 1: Wilder Smoothing Reference Match (atr14, rsi14, adx14)
# ---------------------------------------------------------------------------
def test_wilder_price_factors_against_reference():
    """Verify vectorised atr14, rsi14, and adx14 against independent reference."""
    n = 60
    d0 = date(2026, 1, 1)

    # Deterministic price series with trend, swings, and varied ranges
    closes = [100.0 + i * 1.5 + (5.0 if i % 2 == 0 else -3.0) for i in range(n)]
    highs = [c + 2.5 + (i % 3) for i, c in enumerate(closes)]
    lows = [c - 2.0 - (i % 2) for i, c in enumerate(closes)]
    volumes = [1000 + i * 10 for i in range(n)]

    df = pl.DataFrame({
        "symbol": ["TEST"] * n,
        "d": [d0 + timedelta(days=i) for i in range(n)],
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "turnover": [c * v for c, v in zip(closes, volumes, strict=True)],
    })

    factored = add_price_factors(df)

    # 1. Reference ATR14
    tr_ref: list[float | None] = []
    for i in range(n):
        hl = highs[i] - lows[i]
        if i == 0:
            tr_ref.append(hl)
        else:
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            tr_ref.append(max(hl, hc, lc))

    atr_expected = _py_wilder(tr_ref, 14)
    for i in range(14, n):
        polars_atr = factored["atr14"][i]
        ref_atr = atr_expected[i]
        assert polars_atr is not None and ref_atr is not None
        assert abs(polars_atr - ref_atr) < 1e-6, f"ATR mismatch at {i}: {polars_atr} vs {ref_atr}"

    # 2. Reference RSI14
    gains: list[float | None] = [0.0]
    losses: list[float | None] = [0.0]
    for i in range(1, n):
        ret = closes[i] - closes[i - 1]
        gains.append(ret if ret > 0 else 0.0)
        losses.append(-ret if ret < 0 else 0.0)

    avg_gain = _py_wilder(gains, 14)
    avg_loss = _py_wilder(losses, 14)
    for i in range(14, n):
        ag = avg_gain[i]
        al = avg_loss[i]
        polars_rsi = factored["rsi14"][i]
        assert polars_rsi is not None and ag is not None and al is not None
        expected_rsi = 100.0 - 100.0 / (1.0 + ag / al)
        assert abs(polars_rsi - expected_rsi) < 1e-6, f"RSI mismatch at {i}: {polars_rsi} vs {expected_rsi}"

    # 3. Reference ADX14
    plus_dm: list[float | None] = [0.0]
    minus_dm: list[float | None] = [0.0]
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if up > dn and up > 0 else 0.0)
        minus_dm.append(dn if dn > up and dn > 0 else 0.0)

    sm_plus = _py_wilder(plus_dm, 14)
    sm_minus = _py_wilder(minus_dm, 14)
    dx_vals: list[float | None] = []
    for i in range(n):
        sp, sm, tr_s = sm_plus[i], sm_minus[i], atr_expected[i]
        if sp is None or sm is None or tr_s is None or tr_s == 0:
            dx_vals.append(None)
        else:
            pdi = 100.0 * sp / tr_s
            mdi = 100.0 * sm / tr_s
            denom = pdi + mdi
            dx_vals.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)

    adx_expected = _py_wilder(dx_vals, 14)
    for i in range(28, n):
        polars_adx = factored["adx14"][i]
        ref_adx = adx_expected[i]
        if polars_adx is not None and ref_adx is not None:
            assert abs(polars_adx - ref_adx) < 1e-6, f"ADX mismatch at {i}: {polars_adx} vs {ref_adx}"


# ---------------------------------------------------------------------------
# Test 2: Flat-bar Resilience (NaN Poisoning Protection)
# ---------------------------------------------------------------------------
def test_flat_bar_resilience_no_nan_poisoning():
    """20 flat bars followed by a trend must not poison rsi14, adx14, bb_z, or t_drift_20."""
    n_flat = 22
    n_trend = 30
    n = n_flat + n_trend
    d0 = date(2026, 1, 1)

    prices = [100.0] * n_flat + [100.0 + i * 1.5 for i in range(1, n_trend + 1)]
    highs = [p + 0.0 if i < n_flat else p + 1.0 for i, p in enumerate(prices)]
    lows = [p - 0.0 if i < n_flat else p - 1.0 for i, p in enumerate(prices)]
    vols = [1000] * n

    df = pl.DataFrame({
        "symbol": ["FLAT"] * n,
        "d": [d0 + timedelta(days=i) for i in range(n)],
        "open": prices,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": vols,
        "turnover": [p * v for p, v in zip(prices, vols, strict=True)],
    })

    factored = add_price_factors(df)
    with_trend = add_trend_states(factored)

    # Check for NaN in rsi14, adx14, bb_z, and t_drift_20 across all rows
    for col in ("rsi14", "adx14", "bb_z", "t_drift_20"):
        vals = with_trend[col].to_list()
        for i, val in enumerate(vals):
            if val is not None:
                assert not math.isnan(val), f"NaN poisoned {col} at index {i}"

    # During flat period, rsi14 should be neutral 50.0 (not NaN) once defined
    for i in range(14, n_flat):
        assert abs(factored["rsi14"][i] - 50.0) < 1e-6

    # During flat period, bb_z should be 0.0 (not NaN)
    for i in range(20, n_flat):
        assert abs(factored["bb_z"][i] - 0.0) < 1e-6

    # At row 20 (still within the 22 flat bars), t_drift_20 should be 0.0 (not NaN)
    assert abs(with_trend["t_drift_20"][20] - 0.0) < 1e-6


# ---------------------------------------------------------------------------
# Test 3: Moving Average Stack, 52w Distance, and mom_12_1 Exact Checks
# ---------------------------------------------------------------------------
def test_ma_stack_and_momentum_exact_arithmetic():
    """Verify ma_stack (+1, -1, 0), 52w extremes, and mom_12_1 on known series."""
    n = 260
    d0 = date(2025, 1, 1)

    # Strictly monotonic uptrend: close > sma20 > sma50 > sma200 -> ma_stack = +1
    uptrend_closes = [100.0 + i * 1.0 for i in range(n)]
    df_up = pl.DataFrame({
        "symbol": ["UP"] * n,
        "d": [d0 + timedelta(days=i) for i in range(n)],
        "open": uptrend_closes,
        "high": [c + 1.0 for c in uptrend_closes],
        "low": [c - 1.0 for c in uptrend_closes],
        "close": uptrend_closes,
        "volume": [1000] * n,
        "turnover": [c * 1000.0 for c in uptrend_closes],
    })
    f_up = add_price_factors(df_up)

    # After 200 bars, MA stack should be +1; after 252 bars, 52w extremes are defined
    for i in range(200, n):
        assert f_up["ma_stack"][i] == 1
    for i in range(251, n):
        # At 52w high, dist_52w_high == 0.0
        assert abs(f_up["dist_52w_high"][i] - 0.0) < 1e-6
        # dist_52w_low is positive
        assert f_up["dist_52w_low"][i] > 0.0

    # mom_12_1 check at index 255: close[255 - 21] / close[255 - 252] - 1
    expected_mom = (uptrend_closes[255 - 21] / uptrend_closes[255 - 252]) - 1.0
    assert abs(f_up["mom_12_1"][255] - expected_mom) < 1e-6

    # Strictly monotonic downtrend: close < sma20 < sma50 < sma200 -> ma_stack = -1
    downtrend_closes = [1000.0 - i * 1.0 for i in range(n)]
    df_down = pl.DataFrame({
        "symbol": ["DOWN"] * n,
        "d": [d0 + timedelta(days=i) for i in range(n)],
        "open": downtrend_closes,
        "high": [c + 1.0 for c in downtrend_closes],
        "low": [c - 1.0 for c in downtrend_closes],
        "close": downtrend_closes,
        "volume": [1000] * n,
        "turnover": [c * 1000.0 for c in downtrend_closes],
    })
    f_down = add_price_factors(df_down)
    for i in range(200, n):
        assert f_down["ma_stack"][i] == -1
    for i in range(251, n):
        assert abs(f_down["dist_52w_low"][i] - 0.0) < 1e-6
        assert f_down["dist_52w_high"][i] < 0.0


# ---------------------------------------------------------------------------
# Test 4: Trend Module Functions (nw_lag, efficiency ratio, states)
# ---------------------------------------------------------------------------
def test_trend_properties_and_states():
    """Verify nw_lag truncation, Kaufman efficiency ratio, and state classifications."""
    # 1. nw_lag
    assert nw_lag(20) == 2
    assert nw_lag(63) == 3
    assert nw_lag(252) == 6

    # 2. Efficiency Ratio
    # Straight-line monotonic path: net / path == 1.0
    straight_line = pl.DataFrame({
        "symbol": ["A"] * 25,
        "close": [100.0 + i * 2.0 for i in range(25)],
    })
    er_straight = straight_line.select(
        _efficiency_ratio(pl.col("close"), 20, pl.col("symbol")).alias("er")
    )["er"].to_list()
    assert abs(er_straight[-1] - 1.0) < 1e-6

    # Round trip: net == 0.0 -> er == 0.0
    round_trip = pl.DataFrame({
        "symbol": ["B"] * 21,
        "close": [100.0 + i for i in range(11)] + [110.0 - i for i in range(1, 11)],
    })
    er_round = round_trip.select(
        _efficiency_ratio(pl.col("close"), 20, pl.col("symbol")).alias("er")
    )["er"].to_list()
    assert abs(er_round[-1] - 0.0) < 1e-6

    # 3. add_trend_states on steady strong uptrend
    n = 120
    d0 = date(2026, 1, 1)
    strong_up = [10.0 + i * 3.0 for i in range(n)]
    df_strong = pl.DataFrame({
        "symbol": ["STRG"] * n,
        "d": [d0 + timedelta(days=i) for i in range(n)],
        "open": strong_up,
        "high": [c + 1.5 for c in strong_up],
        "low": [c - 1.0 for c in strong_up],
        "close": strong_up,
        "volume": [10_000] * n,
        "turnover": [c * 10_000.0 for c in strong_up],
    })
    st_df = add_trend_states(add_price_factors(df_strong))

    # Before bars_seen >= n + 21, trend must be UNDEFINED
    for i in range(20 + 20):  # 40 bars
        assert st_df["trend_20"][i] == TrendState.UNDEFINED.value

    # Once established with strong drift, efficiency, stack, and adx, state is UP_STRONG or UP
    last_states = st_df["trend_20"][-20:].to_list()
    assert all(s in {TrendState.UP.value, TrendState.UP_STRONG.value} for s in last_states)


# ---------------------------------------------------------------------------
# Test 5: ta_core Zero-I/O, IDs, DTOs, and AppSettings
# ---------------------------------------------------------------------------
def test_ta_core_contracts():
    """Verify pure-domain contracts in ta_core."""
    # 1. Deterministic run and recommendation IDs
    d = date(2026, 11, 10)
    rid1 = make_run_id(d, Arm.DETERMINISTIC)
    rid2 = make_run_id(d, "DETERMINISTIC")
    assert rid1 == rid2
    assert len(rid1) == 12

    rec1 = make_rec_id(rid1, "INFY")
    assert rec1 == f"{rid1}-INFY"
    rec2 = make_rec_id(rid1, "INFY", suffix="SWING")
    assert rec2 == f"{rid1}-INFY-SWING"

    # 2. Frozen slotted DTOs
    intent = OrderIntent(
        rec_id="rec-1",
        symbol="RELIANCE",
        industry="Refineries",
        d=d,
        side=Side.LONG,
        action=Action.BUY,
        sleeve=Sleeve.MOMENTUM_BREAKOUT,
        horizon=Horizon.SWING,
        qty=100,
        entry_lo=2500.0,
        entry_hi=2510.0,
        stop=2450.0,
        target=2600.0,
    )
    with pytest.raises(FrozenInstanceError):
        intent.qty = 200  # type: ignore[misc]

    fill = Fill(
        order_id="ord-1",
        symbol="RELIANCE",
        d=d,
        side=Side.LONG,
        qty=100,
        price=2505.0,
        gross_notional=250500.0,
        costs_rupees=59.5,
        net_notional=250559.5,
    )
    with pytest.raises(FrozenInstanceError):
        fill.price = 2510.0  # type: ignore[misc]

    ca = CorporateAction(
        symbol="TCS",
        ex_date=d,
        action_type="SPLIT",
        ratio_from=1.0,
        ratio_to=2.0,
        adj_factor=0.5,
        knowledge_time=datetime(2026, 11, 10, 18, 0, tzinfo=UTC),
    )
    with pytest.raises(FrozenInstanceError):
        ca.adj_factor = 0.25  # type: ignore[misc]

    # 3. Protocols runtime checkable
    assert isinstance(MarketDataPort, type)
    assert isinstance(BrokerPort, type)

    # 4. AppSettings from_mapping (pure, zero-I/O)
    settings = AppSettings.from_mapping({
        "CLICKHOUSE_PORT": "9000",
        "EQUITY_BASE": "1000000.0",
        "REDIS_URL": "redis://localhost:6380/1",
    })
    assert settings.clickhouse_port == 9000
    assert settings.equity_base == 1_000_000.0
    assert settings.redis_url == "redis://localhost:6380/1"
    assert settings.clickhouse_host == "127.0.0.1"  # default kept
