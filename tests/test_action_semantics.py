"""`HOLD` must never be emitted for something you do not own.

A screened candidate labelled HOLD reads as advice about an existing
position, and quietly hides that the system is not telling you to do
anything. WATCH is the honest label. This is a user-facing correctness
property, so it gets a test rather than a convention.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from ta_api.main import app
from ta_core.enums import PORTFOLIO_ONLY_ACTIONS, Action

client = TestClient(app, raise_server_exceptions=False)


def _ch_up() -> bool:
    return client.get("/api/health").json()["services"]["clickhouse"]


needs_ch = pytest.mark.skipif(not _ch_up(), reason="ClickHouse not running")


def test_watch_and_hold_are_distinct_members():
    assert Action.WATCH != Action.HOLD
    assert Action.HOLD in PORTFOLIO_ONLY_ACTIONS
    assert Action.WATCH not in PORTFOLIO_ONLY_ACTIONS


@needs_ch
def test_no_recommendation_uses_a_portfolio_only_action_for_an_unowned_name():
    owned = {p["symbol"] for p in client.get("/api/portfolio").json()["positions"]}
    for r in client.get("/api/recommendations").json()["rows"]:
        if r["action"] in {a.value for a in PORTFOLIO_ONLY_ACTIONS}:
            assert r["symbol"] in owned, (
                f"{r['symbol']} is labelled {r['action']} but is not in the "
                f"portfolio -- use WATCH"
            )


@needs_ch
def test_watch_cards_carry_no_size_and_buy_cards_do():
    for r in client.get("/api/recommendations").json()["rows"]:
        if r["action"] == Action.WATCH.value:
            assert r["qty"] == 0, "a WATCH card must not imply a position size"
            assert r["notional"] == 0
        elif r["action"] == Action.BUY.value:
            assert r["qty"] > 0


@needs_ch
def test_every_code_the_api_emits_has_a_plain_english_entry():
    """A chip the user cannot hover for a meaning is a wall of acronyms."""
    terms = client.get("/api/glossary").json()["terms"]
    missing: set[str] = set()
    for r in client.get("/api/recommendations").json()["rows"]:
        missing |= {c for c in r.get("reasons", []) if c not in terms}
        for key in ("action", "sleeve"):
            if r.get(key) and r[key] not in terms:
                missing.add(r[key])
    assert not missing, f"no glossary entry for: {sorted(missing)}"


def test_glossary_explanations_do_not_lean_on_other_codes():
    """If an explanation needs a second acronym explained, it is not plain."""
    terms = client.get("/api/glossary").json()["terms"]
    codes = {t for t in terms if t.isupper() and "_" in t}
    for name, t in terms.items():
        leaked = {c for c in codes if c in t["plain"] and c != name}
        assert not leaked, f"{name} 'plain' text leans on {sorted(leaked)}"
