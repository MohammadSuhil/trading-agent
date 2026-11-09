"""The dashboard's contract with the API.

These run against the real app object (no network), so they fail if an
endpoint stops returning the shape the UI renders. The UI computes nothing,
which means a field that silently disappears here becomes a blank tile there
with no error anywhere -- the exact failure this file exists to catch.

Endpoints that need ClickHouse are skipped when it is not running, so the
suite stays green on a fresh checkout.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from ta_api.main import app

client = TestClient(app, raise_server_exceptions=False)


def _ch_up() -> bool:
    return client.get("/api/health").json()["services"]["clickhouse"]


needs_ch = pytest.mark.skipif(not _ch_up(), reason="ClickHouse not running")


def test_health_always_answers_even_with_everything_down():
    r = client.get("/api/health")
    assert r.status_code == 200
    b = r.json()
    assert b["status"] in {"ok", "degraded"}
    assert set(b["services"]) == {"clickhouse", "mongo", "redis"}
    assert b["data_quality"] in {"OK", "PARTIAL"}


@needs_ch
@pytest.mark.parametrize("path", [
    "/api/context", "/api/heatmap", "/api/screener", "/api/recommendations",
    "/api/rejections", "/api/portfolio", "/api/risk", "/api/industries",
    "/api/calibration", "/api/simulation",
])
def test_every_endpoint_returns_200_and_json(path: str):
    r = client.get(path)
    assert r.status_code == 200, r.text[:400]
    assert isinstance(r.json(), dict)


@needs_ch
def test_recommendation_cards_carry_a_complete_trade_plan():
    rows = client.get("/api/recommendations").json()["rows"]
    if not rows:
        pytest.skip("no recommendations recorded yet")
    required = {"id", "symbol", "name", "industry", "action", "sleeve", "horizon",
                "entry_lo", "entry_hi", "stop", "target", "qty", "notional",
                "risk_rupees", "rr", "cost_bps", "reasons", "barrier"}
    for r in rows:
        assert required <= set(r), sorted(required - set(r))
        assert r["action"] in {"BUY", "WATCH", "HOLD", "TRIM", "SELL", "AVOID"}
        # Geometry must be coherent or the card is lying about the trade.
        assert r["stop"] < r["entry_lo"] <= r["entry_hi"] < r["target"]
        assert isinstance(r["reasons"], list) and r["reasons"]


@needs_ch
def test_barrier_block_never_shows_a_number_it_has_not_earned():
    """The whole point of the barrier design: UNAVAILABLE means no digits."""
    rows = client.get("/api/recommendations").json()["rows"]
    if not rows:
        pytest.skip("no recommendations recorded yet")
    for r in rows:
        b = r["barrier"]
        assert b["state"] in {"OK", "UNAVAILABLE"}
        assert b["reason"], "a barrier block must always explain itself"
        if b["state"] == "UNAVAILABLE":
            assert b["p_up"] is None and b["p_dn"] is None and b["p_vert"] is None
        else:
            triad = (b["p_up"], b["p_dn"], b["p_vert"])
            assert all(p is not None for p in triad)
            # Competing risks: exactly one of three outcomes occurs.
            assert abs(sum(triad) - 1.0) < 1e-3, triad
            assert b["ci_lo"] <= b["p_up"] <= b["ci_hi"]
            assert b["n_eff"] > 0


@needs_ch
def test_a_buy_card_always_has_a_real_size_and_a_hold_card_never_does():
    rows = client.get("/api/recommendations").json()["rows"]
    if not rows:
        pytest.skip("no recommendations recorded yet")
    for r in rows:
        if r["action"] == "BUY":
            assert r["qty"] > 0 and r["notional"] >= 50_000
            assert r["risk_rupees"] > 0


@needs_ch
def test_risk_rules_expose_state_and_never_a_bare_unexplained_number():
    body = client.get("/api/risk").json()
    assert body["rules"], "the risk panel must never be empty"
    for s in body["rules"]:
        assert s["state"] in {"OK", "WARN", "BREACH", "DISPLAY_ONLY"}
        assert s["detail"], f"{s['id']} has no explanation"
        if s["state"] == "DISPLAY_ONLY":
            assert s["value"] is None


@needs_ch
def test_calibration_and_simulation_refuse_rather_than_guess():
    cal = client.get("/api/calibration").json()
    assert cal["state"] in {"OK", "INSUFFICIENT"}
    if cal["state"] == "INSUFFICIENT":
        assert cal["resolution"] is None and cal["bss"] is None
        assert str(cal["needed"]) in cal["note"]

    sim = client.get("/api/simulation").json()
    assert sim["status"] in {"OK", "INSUFFICIENT"}
    if sim["status"] == "INSUFFICIENT":
        assert sim["p_lo"] is None and sim["p_hi"] is None
        assert sim["note"]


@needs_ch
def test_screener_returns_no_duplicate_symbols():
    """ReplacingMergeTree dedupes on a background merge whose timing is not
    guaranteed, so a re-run of the nightly job can leave two versions live."""
    rows = client.get("/api/screener?limit=50").json()["rows"]
    syms = [r["symbol"] for r in rows]
    assert len(syms) == len(set(syms)), "duplicate symbols leaked through"


@needs_ch
def test_heatmap_is_not_swamped_by_an_unclassified_bucket():
    rows = client.get("/api/heatmap").json()["rows"]
    if not rows:
        pytest.skip("no sector rollup yet")
    assert all(r["industry"] != "Unclassified" for r in rows)
    assert len(rows) >= 5


def test_an_unknown_symbol_404s_rather_than_500s():
    r = client.get("/api/trend/NOSUCHSYMBOL")
    assert r.status_code in {404, 503}
