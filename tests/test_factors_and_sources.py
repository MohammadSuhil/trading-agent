"""Tests for expanded price factors, pairwise correlation, surveillance, benchmark TRI, and BSE/ISIN reconciliation.

Covers:
- Phase 2B: 70 strictly-trailing price factors (Invariant P-05 / leakage-free)
- Pairwise residual correlation for RISK-08
- M2.8 Surveillance ingestion and PARTIAL degradation
- M2.10 Benchmark TRI ingestion and panel reconstruction
- M2.11 BSE BhavCopy archive source and ISIN reconciliation
"""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import polars as pl
from ta_core.asof import AsOf
from ta_core.enums import DataQuality
from ta_datafeed.registry import reconcile_isin_bars
from ta_datafeed.sources.benchmark import parse_benchmark_csv, reconstruct_benchmark_from_panel
from ta_datafeed.sources.bse_archive import bse_bhavcopy_url
from ta_datafeed.sources.surveillance import load_surveillance_symbols, parse_surveillance_csv
from ta_quant.factors.correlation import compute_pair_correlations
from ta_quant.factors.price import FACTOR_COLUMNS, add_price_factors


# ============================================================================
# 1. Price Factor Expansion (~20 -> >= 70) and Strict Trailing Windows
# ============================================================================
def _create_sample_panel(n_sessions: int = 60, symbol: str = "TEST") -> pl.DataFrame:
    """Generate a valid, non-trivial bar panel for testing."""
    d0 = date(2026, 1, 1)
    dates = [d0 + timedelta(days=i) for i in range(n_sessions)]

    prices = [100.0 + (i * 0.5) + math.sin(i * 0.3) * 2.0 for i in range(n_sessions)]
    highs = [p + 1.5 for p in prices]
    lows = [p - 1.2 for p in prices]
    opens = [p - 0.2 for p in prices]
    closes = prices
    vols = [10_000 + i * 100 for i in range(n_sessions)]
    tos = [p * v for p, v in zip(closes, vols, strict=True)]
    trades = [500 + i * 5 for i in range(n_sessions)]

    return pl.DataFrame({
        "symbol": [symbol] * n_sessions,
        "d": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": vols,
        "turnover": tos,
        "n_trades": trades,
    })


def test_add_price_factors_has_all_70_columns():
    """Verify that add_price_factors outputs all columns in FACTOR_COLUMNS (>= 70)."""
    assert len(FACTOR_COLUMNS) >= 70, f"Expected >= 70 factors, got {len(FACTOR_COLUMNS)}"

    df = _create_sample_panel(60)
    factored = add_price_factors(df)

    missing = [c for c in FACTOR_COLUMNS if c not in factored.columns]
    assert not missing, f"Missing factor columns in result: {missing}"


def test_price_factors_strictly_trailing_window():
    """Mutating future bars t+1..N MUST NOT alter any factor value at bar t.

    This proves no forward leakage or centred rolling windows exist.
    """
    n = 60
    t = 30  # evaluation bar

    df_orig = _create_sample_panel(n, "INFY")
    factored_orig = add_price_factors(df_orig)

    # Capture all 70 factor values at bar t
    orig_at_t = {col: factored_orig[col][t] for col in FACTOR_COLUMNS}

    # Mutate future bars t+1..n drastically
    mutated_rows = []
    for i, row in enumerate(df_orig.iter_rows(named=True)):
        if i > t:
            # Drastically change price, high, low, volume, turnover
            row["close"] = row["close"] * 5.0
            row["high"] = row["high"] * 5.5
            row["low"] = row["low"] * 4.5
            row["volume"] = row["volume"] * 10
            row["turnover"] = row["turnover"] * 50
        mutated_rows.append(row)

    df_mutated = pl.DataFrame(mutated_rows)
    factored_mutated = add_price_factors(df_mutated)

    for col in FACTOR_COLUMNS:
        v_orig = orig_at_t[col]
        v_mut = factored_mutated[col][t]
        if v_orig is None:
            assert v_mut is None, f"Factor {col} was None originally but became {v_mut}"
        else:
            assert abs(v_orig - v_mut) < 1e-6, (
                f"Leakage detected in factor {col} at bar {t}: "
                f"orig={v_orig}, mutated={v_mut}"
            )


# ============================================================================
# 2. Pairwise Residual Correlation (RISK-08)
# ============================================================================
def test_compute_pair_correlations():
    """Verify market-residual correlation separates correlated pairs from counter-moving pairs."""
    d0 = date(2026, 1, 1)
    dates = [d0 + timedelta(days=i) for i in range(30)]

    rows = []
    for i, dt in enumerate(dates):
        # Market component
        m = 0.01 if i % 2 == 0 else -0.01
        # Idiosyncratic component
        shock = 0.02 if (i // 2) % 2 == 0 else -0.02

        rows.append({"d": dt, "symbol": "CAND1", "ret_1d": m + shock})
        rows.append({"d": dt, "symbol": "POS_CORR", "ret_1d": m + shock})
        rows.append({"d": dt, "symbol": "POS_OPP", "ret_1d": m - shock})
        rows.append({"d": dt, "symbol": "BENCH", "ret_1d": m})

    panel = pl.DataFrame(rows)
    eval_d = dates[-1]

    corrs = compute_pair_correlations(
        panel,
        eval_d,
        candidate_symbols=["CAND1"],
        open_symbols=["POS_CORR", "POS_OPP"],
        window=30,
        min_periods=20,
    )

    # Co-moving residual returns should yield high correlation (> 0.65)
    assert corrs["CAND1"]["POS_CORR"] > 0.65
    # Opposing residual returns should yield negative correlation (< 0.0)
    assert corrs["CAND1"]["POS_OPP"] < 0.0


def test_compute_pair_correlations_insufficient_periods():
    """Returns 0.0 when overlapping periods are below min_periods."""
    d0 = date(2026, 1, 1)
    dates = [d0 + timedelta(days=i) for i in range(10)]  # only 10 days

    rows = []
    for dt in dates:
        rows.append({"d": dt, "symbol": "CAND", "ret_1d": 0.01})
        rows.append({"d": dt, "symbol": "POS", "ret_1d": 0.01})

    panel = pl.DataFrame(rows)
    corrs = compute_pair_correlations(
        panel, dates[-1], ["CAND"], ["POS"], window=30, min_periods=20
    )
    assert corrs["CAND"]["POS"] == 0.0


# ============================================================================
# 3. Surveillance Ingestion & Degradation (M2.8)
# ============================================================================
def test_parse_surveillance_csv():
    """Verify parsing of exchange surveillance list CSV."""
    csv_bytes = b"Symbol,measure,stage\nRELIANCE,ASM,Stage 1\nADANIENT,GSM,Stage 2\n"
    d = date(2026, 10, 30)
    kt = datetime(2026, 10, 30, 18, 30, tzinfo=UTC)

    df = parse_surveillance_csv(csv_bytes, d, kt)
    assert df.height == 2
    assert set(df["symbol"].to_list()) == {"RELIANCE", "ADANIENT"}
    assert df["list_type"].to_list() == ["ASM", "GSM"]
    assert df["stage"].to_list() == ["Stage 1", "Stage 2"]
    assert df["d"][0] == d


class MockGuardedClient:
    """Mock client for testing query_pit."""

    def __init__(self, return_df: pl.DataFrame):
        self.return_df = return_df
        self.last_query: str | None = None

    def query_pit(
        self, table: str, as_of: AsOf, *, columns: str = "*", where: str | None = None
    ) -> pl.DataFrame:
        self.last_query = f"table={table}, as_of={as_of.sql()}, where={where}"
        return self.return_df


def test_load_surveillance_symbols_degradation():
    """When surveillance table has zero rows, it degrades to PARTIAL, never silent FULL."""
    as_of = AsOf(datetime(2026, 10, 30, 18, 30, tzinfo=UTC))
    d = date(2026, 10, 30)

    # Empty store -> PARTIAL
    client_empty = MockGuardedClient(pl.DataFrame({"symbol": [], "list_type": []}))
    syms, quality = load_surveillance_symbols(client_empty, d, as_of)
    assert syms == set()
    assert quality == DataQuality.PARTIAL

    # Populated store -> FULL
    client_full = MockGuardedClient(
        pl.DataFrame({"symbol": ["ADANIPOWER"], "list_type": ["ASM"]})
    )
    syms, quality = load_surveillance_symbols(client_full, d, as_of)
    assert syms == {"ADANIPOWER"}
    assert quality == DataQuality.FULL


# ============================================================================
# 4. Benchmark TRI Ingestion & Reconstruction (M2.10)
# ============================================================================
def test_parse_benchmark_csv():
    """Verify parsing published benchmark TRI CSV."""
    csv_bytes = (
        b"Date,Total Returns Index\n"
        b"2026-01-01,1000.0\n"
        b"2026-01-02,1010.0\n"
        b"2026-01-03,1005.0\n"
    )
    kt = datetime(2026, 1, 3, 18, 30, tzinfo=UTC)
    df = parse_benchmark_csv(csv_bytes, index_id="NIFTY50", knowledge_time=kt)

    assert df.height == 3
    assert df["index_id"][0] == "NIFTY50"
    assert df["tri_source"][0] == "PUBLISHED"
    assert df["close"].to_list() == [1000.0, 1010.0, 1005.0]
    # Check ret_1d: row 0 is 0.0, row 1 is 1010/1000 - 1 = 0.01
    assert abs(df["ret_1d"][1] - 0.01) < 1e-6


def test_reconstruct_benchmark_from_panel():
    """Verify panel constituent reconstruction compounds from 1000.0 base."""
    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)
    kt = datetime(2026, 1, 2, 18, 30, tzinfo=UTC)

    bars = pl.DataFrame({
        "symbol": ["RELIANCE", "TCS", "RELIANCE", "TCS"],
        "d": [d1, d1, d2, d2],
        "close": [100.0, 200.0, 102.0, 202.0],  # REL +2%, TCS +1%
        "turnover": [1000.0, 1000.0, 1000.0, 1000.0],  # Equal weight
        "ret_1d": [0.0, 0.0, 0.02, 0.01],
        "knowledge_time": [kt] * 4,
    })
    members = pl.DataFrame({
        "index_name": ["NIFTY50", "NIFTY50"],
        "symbol": ["RELIANCE", "TCS"],
    })

    rec = reconstruct_benchmark_from_panel(bars, members, index_id="NIFTY50", knowledge_time=kt)
    assert rec.height == 2
    assert rec["tri_source"][0] == "RECONSTRUCTED"
    assert rec["series_kind"][0] == "TRI"
    # Session 2 turnover-weighted return: (0.02*1000 + 0.01*1000)/2000 = 0.015
    assert abs(rec["ret_1d"][1] - 0.015) < 1e-6
    # Compounded from 1000: 1000 * 1.0 * (1 + 0.015) = 1015.0
    assert abs(rec["close"][1] - 1015.0) < 1e-4


# ============================================================================
# 5. BSE BhavCopy URL & Dual-Listed ISIN Reconciliation (M2.11)
# ============================================================================
def test_bse_bhavcopy_url():
    """Verify exact BSE BhavCopy download URL."""
    url = bse_bhavcopy_url(date(2026, 10, 29))
    assert url == (
        "https://www.bseindia.com/download/BhavCopy/Equity/"
        "BhavCopy_BSE_CM_0_0_0_20261029_F_0000.CSV"
    )


def test_reconcile_isin_bars_no_duplicate_listings():
    """Verify NSE is primary and dual-listed BSE rows are dropped by ISIN."""
    d = date(2026, 10, 30)
    kt = datetime(2026, 10, 30, 18, 30, tzinfo=UTC)

    nse_bars = pl.DataFrame({
        "symbol": ["RELIANCE", "TCS"],
        "isin": ["INE002A01018", "INE467B01029"],
        "d": [d, d],
        "close": [2500.0, 3800.0],
        "turnover": [1e8, 5e7],
        "source": ["nse_udiff", "nse_udiff"],
        "knowledge_time": [kt, kt],
    })

    bse_bars = pl.DataFrame({
        "symbol": ["500325", "532540", "599999"],  # RELIANCE, TCS, and a BSE-exclusive
        "isin": ["INE002A01018", "INE467B01029", "INE999X01011"],
        "d": [d, d, d],
        "close": [2498.0, 3802.0, 120.0],
        "turnover": [1e6, 5e5, 2e5],
        "source": ["bse_udiff", "bse_udiff", "bse_udiff"],
        "knowledge_time": [kt, kt, kt],
    })

    reconciled = reconcile_isin_bars(nse_bars, bse_bars)

    # Result must contain 3 rows: 2 from NSE (primary), 1 BSE-exclusive
    assert reconciled.height == 3
    symbols = set(reconciled["symbol"].to_list())
    assert symbols == {"RELIANCE", "TCS", "599999"}

    # RELIANCE and TCS must retain the primary NSE prices and source
    rel_row = reconciled.filter(pl.col("symbol") == "RELIANCE")
    assert rel_row["close"][0] == 2500.0
    assert rel_row["source"][0] == "nse_udiff"

    # BSE exclusive must be retained
    bse_row = reconciled.filter(pl.col("symbol") == "599999")
    assert bse_row["close"][0] == 120.0
    assert bse_row["source"][0] == "bse_udiff"
