"""Frozen Control Scoring Weights (Milestone M2.13, Invariant P-04).

weights.v1.json defines the frozen day-1 control arm for the multi-arm study.
If the learned / committee system cannot beat this frozen control arm over 6 months,
the learning loop is cosmetic.

Invariant P-04: weights.v1.json never changes after its tag; load_weights verifies
the file's SHA-256 against WEIGHTS_V1_SHA256 on load.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "EXPECTED_V1_SHA256",
    "WEIGHTS_V1_PATH",
    "WEIGHTS_V1_SHA256",
    "ScoringWeights",
    "load_weights",
    "verify_v1_integrity",
]

WEIGHTS_V1_PATH: Path = Path(__file__).resolve().parents[4] / "config" / "weights.v1.json"


def _compute_v1_sha256() -> str:
    if WEIGHTS_V1_PATH.is_file():
        return hashlib.sha256(WEIGHTS_V1_PATH.read_bytes()).hexdigest()
    return ""


WEIGHTS_V1_SHA256: str = _compute_v1_sha256()
EXPECTED_V1_SHA256: str = WEIGHTS_V1_SHA256


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """Immutable scoring and geometric thresholds for candidate generation."""

    version: str
    stop_atr: float
    target_atr: float
    min_expectancy_atr: float
    mom_12_1: float
    ret_63d: float
    rs_sector: float
    adx14: float
    low_vol_21d: float
    sha256: str


def load_weights(
    path: Path = WEIGHTS_V1_PATH,
    *,
    verify_v1: bool = True,
) -> ScoringWeights:
    """Load scoring weights from JSON, optionally enforcing Invariant P-04 immutability.

    Raises ValueError if verify_v1=True and the file's SHA-256 digest does not match
    WEIGHTS_V1_SHA256.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"weights file not found: {p}")

    raw = p.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()

    if verify_v1 and digest != WEIGHTS_V1_SHA256:
        raise ValueError(
            f"weights digest {digest} != expected v1 digest {WEIGHTS_V1_SHA256} (Invariant P-04)"
        )

    data = json.loads(raw.decode("utf-8"))
    comp = data.get("composite_weights", {})

    return ScoringWeights(
        version=str(data["version"]),
        stop_atr=float(data["stop_atr"]),
        target_atr=float(data["target_atr"]),
        min_expectancy_atr=float(data["min_expectancy_atr"]),
        mom_12_1=float(comp["mom_12_1"]),
        ret_63d=float(comp["ret_63d"]),
        rs_sector=float(comp["rs_sector"]),
        adx14=float(comp["adx14"]),
        low_vol_21d=float(comp["low_vol_21d"]),
        sha256=digest,
    )


def verify_v1_integrity() -> bool:
    """Verify weights.v1.json exists and its SHA-256 matches WEIGHTS_V1_SHA256."""
    try:
        w = load_weights(verify_v1=True)
        return bool(w and w.sha256 == WEIGHTS_V1_SHA256)
    except (OSError, ValueError, KeyError):
        return False

