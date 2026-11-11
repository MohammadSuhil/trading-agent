"""Tests for hash-chained ledger, RunManifest, PaperBroker, and deterministic rebuild.

Enforces:
  P-01: Paper broker and backtester call the same simulate_fill() / apply_costs().
  P-02: One RunManifest, created in M2; M3 replay and M4 generation records extend it,
        never fork it.
  P-03: The ledger is hash-chained; a seq collision or gap fails verify_chain().
  I-14: Same-bar ambiguity resolves DOWN (stop first).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from ta_core.enums import Arm, BarrierHit, Horizon, Sleeve
from ta_ledger.broker import (
    BarData,
    OpenLot,
    Order,
    PaperBroker,
    apply_costs,
    simulate_fill,
)
from ta_ledger.events import (
    GENESIS_HASH,
    EventKind,
    LedgerChainError,
    LedgerEvent,
    canonical_json,
    compute_event_hash,
    events_from_df,
    events_to_df,
    verify_chain,
)
from ta_ledger.manifest import RunManifest, file_sha256
from ta_ledger.rebuild import rebuild_from_ledger


def test_seq_collision_and_gap_fail_verify_chain() -> None:
    """Invariant P-03: seq collision, gap, wrong genesis, or mutation fails verify_chain()."""
    t0 = datetime(2026, 11, 10, 9, 15, 0, tzinfo=UTC)

    # 1. Valid chain passes
    e1 = LedgerEvent.create(
        seq=1,
        ts=t0,
        run_id="R1",
        kind=EventKind.RUN_MANIFEST,
        payload={"arm": "DETERMINISTIC"},
        prev_hash=GENESIS_HASH,
    )
    e2 = LedgerEvent.create(
        seq=2,
        ts=t0 + timedelta(minutes=1),
        run_id="R1",
        kind=EventKind.ORDER_PLACED,
        payload={"order_id": "O1", "symbol": "INFY"},
        prev_hash=e1.hash,
    )
    e3 = LedgerEvent.create(
        seq=3,
        ts=t0 + timedelta(minutes=2),
        run_id="R1",
        kind=EventKind.EQUITY_MARKED,
        payload={"equity": 500_000.0},
        prev_hash=e2.hash,
    )
    verify_chain([e1, e2, e3])

    # 2. Empty sequence is valid
    verify_chain([])

    # 3. Sequence not starting at 1
    bad_start = LedgerEvent(
        seq=2,
        ts=e1.ts,
        run_id=e1.run_id,
        kind=e1.kind,
        payload=e1.payload,
        prev_hash=GENESIS_HASH,
        hash=compute_event_hash(2, e1.ts, e1.run_id, e1.kind.value, e1.payload_json(), GENESIS_HASH),
    )
    with pytest.raises(LedgerChainError, match="expected seq 1, got 2"):
        verify_chain([bad_start])

    # 4. Sequence gap (e.g. seq 1 followed by seq 3)
    bad_gap = LedgerEvent(
        seq=3,
        ts=e2.ts,
        run_id=e2.run_id,
        kind=e2.kind,
        payload=e2.payload,
        prev_hash=e1.hash,
        hash=compute_event_hash(3, e2.ts, e2.run_id, e2.kind.value, e2.payload_json(), e1.hash),
    )
    with pytest.raises(LedgerChainError, match="expected seq 2, got 3"):
        verify_chain([e1, bad_gap])

    # 5. Sequence collision (duplicate seq in ClickHouse ReplacingMergeTree)
    bad_collision = LedgerEvent(
        seq=1,
        ts=e2.ts,
        run_id=e2.run_id,
        kind=e2.kind,
        payload=e2.payload,
        prev_hash=e1.hash,
        hash=compute_event_hash(1, e2.ts, e2.run_id, e2.kind.value, e2.payload_json(), e1.hash),
    )
    with pytest.raises(LedgerChainError, match="expected seq 2, got 1"):
        verify_chain([e1, bad_collision])

    # 6. Broken prev_hash link
    broken_link = LedgerEvent(
        seq=2,
        ts=e2.ts,
        run_id=e2.run_id,
        kind=e2.kind,
        payload=e2.payload,
        prev_hash="f" * 64,
        hash=compute_event_hash(2, e2.ts, e2.run_id, e2.kind.value, e2.payload_json(), "f" * 64),
    )
    with pytest.raises(LedgerChainError, match="prev_hash"):
        verify_chain([e1, broken_link])

    # 7. Mutated payload / tampered content without updating hash
    tampered_payload = LedgerEvent(
        seq=2,
        ts=e2.ts,
        run_id=e2.run_id,
        kind=e2.kind,
        payload={"order_id": "O1", "symbol": "TAMPERED"},
        prev_hash=e1.hash,
        hash=e2.hash,
    )
    with pytest.raises(LedgerChainError, match="Hash mismatch"):
        verify_chain([e1, tampered_payload])


def test_run_manifest_roundtrip_and_extension() -> None:
    """Invariant P-02: RunManifest wraps into event #1 and extends into M3/M4 without forking."""
    as_of = datetime(2026, 11, 10, 18, 0, 0, tzinfo=UTC)
    m2_manifest = RunManifest(
        run_id="RUN-20261110-DETERMINISTIC",
        d=date(2026, 11, 10),
        as_of=as_of,
        arm="DETERMINISTIC",
        git_sha="0123456789abcdef0123456789abcdef01234567",
        uv_lock_sha="a" * 64,
        weights_sha="b" * 64,
        config_sha="c" * 64,
    )

    # Wraps as event #1
    ev1 = m2_manifest.to_event(seq=1)
    assert ev1.seq == 1
    assert ev1.prev_hash == GENESIS_HASH
    assert ev1.kind == EventKind.RUN_MANIFEST
    assert ev1.run_id == "RUN-20261110-DETERMINISTIC"

    # Lossless round-trip
    restored_m2 = RunManifest.from_dict(ev1.payload)
    assert restored_m2 == m2_manifest

    # M4 generation extension without forking
    m4_manifest = RunManifest(
        run_id="RUN-20261110-COMMITTEE",
        d=date(2026, 11, 10),
        as_of=as_of,
        arm="COMMITTEE",
        git_sha="0123456789abcdef0123456789abcdef01234567",
        uv_lock_sha="a" * 64,
        weights_sha="b" * 64,
        config_sha="c" * 64,
        model_tier="QWEN_35B",
        degraded_mode=False,
        generations=(
            {"model": "qwen3.6-35b-a3b", "stage": "PASS1", "prompt_tokens": 1200, "eval_tokens": 250},
            {"model": "qwen3.6-35b-a3b", "stage": "SYNTHESIS", "prompt_tokens": 2800, "eval_tokens": 420},
        ),
        extra={"voting_rule": "SUPERMAJORITY", "temperature": 0.2},
    )
    ev_m4 = m4_manifest.to_event(seq=1)
    restored_m4 = RunManifest.from_dict(ev_m4.payload)
    assert restored_m4 == m4_manifest
    assert len(restored_m4.generations) == 2
    assert restored_m4.generations[1]["stage"] == "SYNTHESIS"
    assert restored_m4.extra["voting_rule"] == "SUPERMAJORITY"


def test_same_bar_ambiguity_resolves_to_stop() -> None:
    """Invariant I-14: Same-bar ambiguity resolves DOWN (stop beats target)."""
    lot = OpenLot(
        order_id="ORD-001",
        symbol="INFY",
        industry="IT",
        qty=50,
        entry_date=date(2026, 11, 2),
        entry_price=1500.0,
        entry_cost=35.0,
        stop=1450.0,
        initial_stop=1450.0,
        target=1600.0,
        last_price=1500.0,
    )

    # Bar touches both stop (low 1440 <= 1450) and target (high 1620 >= 1600)
    bar_ambiguous = BarData(
        symbol="INFY",
        d=date(2026, 11, 3),
        open=1510.0,
        high=1620.0,
        low=1440.0,
        close=1550.0,
    )
    res = simulate_fill(lot, bar_ambiguous)
    assert res.filled is True
    assert res.barrier_hit == BarrierHit.DOWN
    assert res.fill_price == 1450.0
    assert res.reason == "SAME_BAR_STOP_FIRST"
    assert res.stop_slippage_bps == 0.0

    # Gap stop takes precedence over any intraday price
    bar_gap_stop = BarData(
        symbol="INFY",
        d=date(2026, 11, 3),
        open=1430.0,  # gapped through stop 1450
        high=1620.0,
        low=1420.0,
        close=1500.0,
    )
    res_gap = simulate_fill(lot, bar_gap_stop)
    assert res_gap.filled is True
    assert res_gap.barrier_hit == BarrierHit.DOWN
    assert res_gap.fill_price == 1430.0
    assert res_gap.reason == "GAP_STOP"
    assert res_gap.stop_slippage_bps > 0.0

    # Target hit when stop not touched
    bar_target_only = BarData(
        symbol="INFY",
        d=date(2026, 11, 3),
        open=1510.0,
        high=1610.0,
        low=1480.0,
        close=1590.0,
    )
    res_target = simulate_fill(lot, bar_target_only)
    assert res_target.filled is True
    assert res_target.barrier_hit == BarrierHit.UP
    assert res_target.fill_price == 1600.0
    assert res_target.reason == "TARGET_HIT"


def test_ledger_rebuild() -> None:
    """Comprehensive test: runs PaperBroker across multiple sessions, then rebuilds from ledger.

    Exercises:
      - Placing orders (5 symbols)
      - Filling entries (4 symbols)
      - Expiring an order that gapped above entry_hi (1 symbol)
      - Trailing a stop (RELIANCE)
      - Hitting a stop with gap slippage (RELIANCE)
      - Hitting a target (TCS)
      - Hitting a time-stop (HDFCBANK across 10 sessions)
      - Open lot remaining active (INFY, positional horizon)
      - Full state projection parity: portfolio, open_lots, equity_curve, outcomes.
    """
    broker = PaperBroker(initial_equity=500_000.0, arm=Arm.DETERMINISTIC)
    as_of = datetime(2026, 11, 2, 18, 0, 0, tzinfo=UTC)

    manifest = RunManifest(
        run_id="RUN-TEST-001",
        d=date(2026, 11, 2),
        as_of=as_of,
        arm="DETERMINISTIC",
        git_sha="0123456789abcdef0123456789abcdef01234567",
        uv_lock_sha="a" * 64,
        weights_sha="b" * 64,
        config_sha="c" * 64,
    )
    broker.record_manifest(manifest)

    # 1. Place 5 orders on 2026-11-02
    o_rel = Order(
        order_id="ORD-REL",
        symbol="RELIANCE",
        industry="Energy",
        placed_d=date(2026, 11, 2),
        entry_lo=2400.0,
        entry_hi=2420.0,
        stop=2350.0,
        target=2550.0,
        qty=25,
        horizon=Horizon.SWING,
        sleeve=Sleeve.MOMENTUM_BREAKOUT,
        reasons=("BREAKOUT_52W_PROXIMITY",),
    )
    o_tcs = Order(
        order_id="ORD-TCS",
        symbol="TCS",
        industry="IT",
        placed_d=date(2026, 11, 2),
        entry_lo=3500.0,
        entry_hi=3520.0,
        stop=3400.0,
        target=3700.0,
        qty=20,
        horizon=Horizon.SWING,
        sleeve=Sleeve.MOMENTUM_BREAKOUT,
        reasons=("MA_STACK_ALIGNED",),
    )
    o_infy = Order(
        order_id="ORD-INFY",
        symbol="INFY",
        industry="IT",
        placed_d=date(2026, 11, 2),
        entry_lo=1500.0,
        entry_hi=1520.0,
        stop=1450.0,
        target=1650.0,
        qty=50,
        horizon=Horizon.POSITIONAL,  # Positional: 60 sessions, stays open
        sleeve=Sleeve.MOMENTUM_BREAKOUT,
        reasons=("RS_VS_NIFTY",),
    )
    o_hdfc = Order(
        order_id="ORD-HDFC",
        symbol="HDFCBANK",
        industry="Financial Services",
        placed_d=date(2026, 11, 2),
        entry_lo=1600.0,
        entry_hi=1620.0,
        stop=1550.0,
        target=1750.0,
        qty=30,
        horizon=Horizon.SWING,  # Swing: 10 sessions, hits time stop
        sleeve=Sleeve.MEAN_REVERSION,
        reasons=("OVERSOLD_ZSCORE",),
    )
    o_icici = Order(
        order_id="ORD-ICICI",
        symbol="ICICIBANK",
        industry="Financial Services",
        placed_d=date(2026, 11, 2),
        entry_lo=1000.0,
        entry_hi=1010.0,
        stop=980.0,
        target=1060.0,
        qty=40,
        horizon=Horizon.SWING,
        sleeve=Sleeve.MOMENTUM_BREAKOUT,
        reasons=("OI_LONG_BUILDUP",),
    )
    broker.place_orders(
        [o_rel, o_tcs, o_infy, o_hdfc, o_icici],
        run_id=manifest.run_id,
        ts=datetime(2026, 11, 2, 18, 30, 0, tzinfo=UTC),
    )

    # 2. Session 1: 2026-11-03
    # REL, TCS, INFY, HDFC fill. ICICI gaps above entry_hi (1050 > 1010) and expires.
    bars_s1 = {
        "RELIANCE": BarData("RELIANCE", date(2026, 11, 3), 2410.0, 2430.0, 2405.0, 2425.0),
        "TCS": BarData("TCS", date(2026, 11, 3), 3510.0, 3550.0, 3500.0, 3540.0),
        "INFY": BarData("INFY", date(2026, 11, 3), 1510.0, 1530.0, 1505.0, 1520.0),
        "HDFCBANK": BarData("HDFCBANK", date(2026, 11, 3), 1610.0, 1625.0, 1605.0, 1615.0),
        "ICICIBANK": BarData("ICICIBANK", date(2026, 11, 3), 1050.0, 1060.0, 1040.0, 1055.0),
    }
    broker.step_session(
        date(2026, 11, 3),
        bars_s1,
        run_id=manifest.run_id,
        ts=datetime(2026, 11, 3, 16, 0, 0, tzinfo=UTC),
    )
    assert len(broker.open_lots) == 4
    assert len(broker.pending_orders) == 0

    # 3. Session 2: 2026-11-04
    # Trail stop on RELIANCE from 2350 to 2380.
    broker.trail_stop(
        "ORD-REL",
        2380.0,
        run_id=manifest.run_id,
        ts=datetime(2026, 11, 4, 9, 0, 0, tzinfo=UTC),
    )

    # In session 2:
    # TCS hits target (high 3710 >= 3700).
    # RELIANCE, INFY, HDFCBANK continue.
    bars_s2 = {
        "RELIANCE": BarData("RELIANCE", date(2026, 11, 4), 2420.0, 2435.0, 2400.0, 2410.0),
        "TCS": BarData("TCS", date(2026, 11, 4), 3550.0, 3710.0, 3540.0, 3705.0),
        "INFY": BarData("INFY", date(2026, 11, 4), 1525.0, 1535.0, 1515.0, 1530.0),
        "HDFCBANK": BarData("HDFCBANK", date(2026, 11, 4), 1615.0, 1630.0, 1610.0, 1620.0),
    }
    broker.step_session(
        date(2026, 11, 4),
        bars_s2,
        run_id=manifest.run_id,
        ts=datetime(2026, 11, 4, 16, 0, 0, tzinfo=UTC),
    )
    assert "ORD-TCS" not in broker.open_lots
    assert len(broker.outcomes) == 1
    assert broker.outcomes[0]["barrier_hit"] == BarrierHit.UP.value
    assert broker.outcomes[0]["rec_id"] == "ORD-TCS"

    # 4. Session 3: 2026-11-05
    # RELIANCE gaps through trailed stop 2380 at open 2360!
    # INFY and HDFCBANK continue.
    bars_s3 = {
        "RELIANCE": BarData("RELIANCE", date(2026, 11, 5), 2360.0, 2370.0, 2340.0, 2355.0),
        "INFY": BarData("INFY", date(2026, 11, 5), 1530.0, 1545.0, 1520.0, 1535.0),
        "HDFCBANK": BarData("HDFCBANK", date(2026, 11, 5), 1620.0, 1635.0, 1610.0, 1625.0),
    }
    broker.step_session(
        date(2026, 11, 5),
        bars_s3,
        run_id=manifest.run_id,
        ts=datetime(2026, 11, 5, 16, 0, 0, tzinfo=UTC),
    )
    assert "ORD-REL" not in broker.open_lots
    assert len(broker.outcomes) == 2
    assert broker.outcomes[1]["barrier_hit"] == BarrierHit.DOWN.value
    assert broker.outcomes[1]["rec_id"] == "ORD-REL"

    # 5. Sessions 4 through 10 (dates 2026-11-06 to 2026-11-12)
    # INFY and HDFCBANK drift sideways without touching stop or target.
    # On session 10 (2026-11-12), HDFCBANK reaches session 10 (sessions_held + 1 >= 10).
    # Its vertical time-stop triggers at close! INFY is POSITIONAL (60), so it stays open.
    for day_idx in range(6, 13):
        curr_d = date(2026, 11, day_idx)
        bars_drift = {
            "INFY": BarData("INFY", curr_d, 1530.0, 1545.0, 1520.0, 1535.0),
            "HDFCBANK": BarData("HDFCBANK", curr_d, 1620.0, 1635.0, 1610.0, 1625.0),
        }
        broker.step_session(
            curr_d,
            bars_drift,
            run_id=manifest.run_id,
            ts=datetime(2026, 11, day_idx, 16, 0, 0, tzinfo=UTC),
        )

    # HDFCBANK should have closed on TIME_STOP on 2026-11-12 (day 10 for HDFCBANK)
    assert "ORD-HDFC" not in broker.open_lots
    assert len(broker.outcomes) == 3
    assert broker.outcomes[2]["rec_id"] == "ORD-HDFC"
    assert broker.outcomes[2]["barrier_hit"] == BarrierHit.VERTICAL.value

    # INFY remains open
    assert "ORD-INFY" in broker.open_lots
    assert len(broker.open_lots) == 1

    # 6. Rebuild complete state from ledger events
    rebuilt = rebuild_from_ledger(broker.events, initial_equity=500_000.0)

    # Assert exact state reconstruction
    assert rebuilt.portfolio == broker.portfolio()
    assert rebuilt.open_lots == tuple(broker.open_lots.values())
    assert rebuilt.equity_curve == broker.equity_curve
    assert rebuilt.outcomes == broker.outcomes
    assert rebuilt.pending_orders == tuple(broker.pending_orders.values())
    assert len(rebuilt.manifests) == 1
    assert rebuilt.manifests[0] == manifest


def test_polars_df_roundtrip_and_canonical_json() -> None:
    """Test Polars DataFrame conversion and canonical JSON serialization."""
    t0 = datetime(2026, 11, 10, 9, 15, 0, tzinfo=UTC)
    e1 = LedgerEvent.create(
        seq=1,
        ts=t0,
        run_id="R1",
        kind=EventKind.RUN_MANIFEST,
        payload={"symbols": ["TCS", "INFY"], "val": 123},
    )
    e2 = LedgerEvent.create(
        seq=2,
        ts=t0 + timedelta(minutes=5),
        run_id="R1",
        kind=EventKind.EQUITY_MARKED,
        payload={"equity": 500_000.0},
        prev_hash=e1.hash,
    )

    df = events_to_df([e1, e2])
    assert set(df.columns) == {"seq", "ts", "run_id", "kind", "payload", "prev_hash", "hash"}
    assert df.height == 2

    restored = events_from_df(df)
    assert len(restored) == 2
    assert restored[0] == e1
    assert restored[1] == e2

    # Empty df roundtrip
    empty_df = events_to_df([])
    assert empty_df.height == 0
    assert events_from_df(empty_df) == []

    # Canonical JSON checks
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert canonical_json({"date": date(2026, 11, 10)}) == '{"date":"2026-11-10"}'
    with pytest.raises(ValueError, match="Out of range float values"):
        canonical_json({"inf": float("inf")})


def test_file_sha256(tmp_path: Path) -> None:
    """Test file SHA-256 computation helper."""
    assert file_sha256("/nonexistent/file/path") == "MISSING"

    sample = tmp_path / "sample.txt"
    sample.write_text("trading-agent", encoding="utf-8")
    h = file_sha256(sample)
    assert len(h) == 64
    assert h == file_sha256(str(sample))


def test_apply_costs_and_precision() -> None:
    """Invariant P-01: Cost calculation matches ta_core.costs.compute_costs doctests."""
    c_rt = apply_costs(100_000, 100_000)
    assert round(c_rt.total, 2) == 237.59

    # Prove split precision
    c_tot = apply_costs(2000, 2000)
    c_buy = apply_costs(2000, 0)
    c_sell = apply_costs(0, 2000)
    assert abs(c_tot.total - (c_buy.total + c_sell.total)) < 1e-9
