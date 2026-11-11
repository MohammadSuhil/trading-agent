"""Tests for Invariant P-04: weights.v1.json is immutable and frozen control.

The frozen control arm never changes after its commit tag.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from ta_quant.weights import (
    WEIGHTS_V1_PATH,
    WEIGHTS_V1_SHA256,
    load_weights,
)


def test_weights_v1_is_immutable(tmp_path: Path) -> None:
    """Assert config/weights.v1.json exists, matches WEIGHTS_V1_SHA256, and rejects mutations."""
    assert WEIGHTS_V1_PATH.is_file(), f"Missing frozen control weights file at {WEIGHTS_V1_PATH}"

    # Verify digest matches
    actual_hash = hashlib.sha256(WEIGHTS_V1_PATH.read_bytes()).hexdigest()
    assert actual_hash == WEIGHTS_V1_SHA256
    assert len(WEIGHTS_V1_SHA256) == 64

    # Load frozen control weights
    weights = load_weights(verify_v1=True)
    assert weights.version == "v1"
    assert weights.stop_atr == 1.5
    assert weights.target_atr == 2.0
    assert weights.min_expectancy_atr == 0.05
    assert weights.mom_12_1 == 0.30
    assert weights.ret_63d == 0.20
    assert weights.rs_sector == 0.25
    assert weights.adx14 == 0.15
    assert weights.low_vol_21d == 0.10
    assert weights.sha256 == actual_hash

    # Create a modified copy with different values
    tampered_file = tmp_path / "weights.v1.modified.json"
    tampered_file.write_text(
        '{\n  "version": "v1",\n  "stop_atr": 9.9,\n  "target_atr": 20.0,\n'
        '  "min_expectancy_atr": 0.5,\n  "composite_weights": {\n'
        '    "mom_12_1": 0.5,\n    "ret_63d": 0.5,\n    "rs_sector": 0.0,\n'
        '    "adx14": 0.0,\n    "low_vol_21d": 0.0\n  }\n}\n'
    )

    # Invariant P-04: loading tampered weights with verify_v1=True MUST raise ValueError
    with pytest.raises(ValueError, match="Invariant P-04"):
        load_weights(tampered_file, verify_v1=True)

    # Unverified loading succeeds (for prospective testing or new versions)
    unverified = load_weights(tampered_file, verify_v1=False)
    assert unverified.stop_atr == 9.9
    assert unverified.sha256 != WEIGHTS_V1_SHA256
