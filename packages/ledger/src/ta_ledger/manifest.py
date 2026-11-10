"""RunManifest: immutable provenance metadata for every pipeline execution.

Invariant P-02: One RunManifest, created in M2; M3 replay and M4 generation records
extend it, never fork it. Replay and attribution must be able to load any past run's
manifest and reconstruct the exact commit, dependencies, configuration, weights,
and (in M4+) model generations that produced every recommendation.

The manifest is emitted as event #1 in ev.ledger_events under kind RUN_MANIFEST,
sealing the run parameters before any order or trade occurs.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .events import GENESIS_HASH, EventKind, LedgerEvent

__all__ = ["RunManifest", "file_sha256"]


def file_sha256(path: Path | str) -> str:
    """Compute the SHA-256 hex digest of a file, or return 'MISSING' if absent.

    >>> file_sha256("/nonexistent/path/for/doctest")
    'MISSING'
    """
    p = Path(path)
    if not p.is_file():
        return "MISSING"
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Provenance and environment manifest recorded at the head of every run.

    Fields:
      run_id: Unique run identifier (e.g., 'RUN-20261110-DETERMINISTIC').
      d: Decision date (trading session).
      as_of: Exact point-in-time boundary for all inputs.
      arm: Arm name ('DETERMINISTIC', 'FROZEN_CONTROL', 'RANDOM', 'COMMITTEE').
      git_sha: HEAD git commit SHA.
      uv_lock_sha: SHA-256 of uv.lock pinning Python packages.
      weights_sha: SHA-256 of scoring weights.
      config_sha: SHA-256 of risk/strategy configuration.
      stage_tuple_sha: SHA-256 of the committee pipeline stage sequence.
      data_quality: EOD data completeness indicator ('FULL', 'PARTIAL').
      model_tier: Active LLM tier ('NONE', 'QWEN_35B', 'GPT_OSS_20B', etc.).
      degraded_mode: True if memory budget forced step-down or heuristic fallback.
      generations: M4+ generation records (empty in M2/M3; extends without forking).
      extra: Optional structured metadata for experiment tracking.
    """

    run_id: str
    d: date
    as_of: datetime
    arm: str
    git_sha: str
    uv_lock_sha: str
    weights_sha: str
    config_sha: str
    stage_tuple_sha: str = ""
    data_quality: str = "FULL"
    model_tier: str = "NONE"
    degraded_mode: bool = False
    generations: tuple[dict[str, Any], ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert manifest to a JSON-serializable dictionary."""
        as_of_utc = self.as_of.astimezone(UTC) if self.as_of.tzinfo else self.as_of.replace(tzinfo=UTC)
        as_of_iso = as_of_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return {
            "run_id": self.run_id,
            "d": self.d.isoformat(),
            "as_of": as_of_iso,
            "arm": self.arm,
            "git_sha": self.git_sha,
            "uv_lock_sha": self.uv_lock_sha,
            "weights_sha": self.weights_sha,
            "config_sha": self.config_sha,
            "stage_tuple_sha": self.stage_tuple_sha,
            "data_quality": self.data_quality,
            "model_tier": self.model_tier,
            "degraded_mode": self.degraded_mode,
            "generations": [dict(g) for g in self.generations],
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunManifest:
        """Construct a RunManifest from a serialized mapping."""
        raw_d = data["d"]
        d_val = raw_d if isinstance(raw_d, date) else date.fromisoformat(str(raw_d))

        raw_as_of = data["as_of"]
        if isinstance(raw_as_of, datetime):
            as_of_val = raw_as_of if raw_as_of.tzinfo else raw_as_of.replace(tzinfo=UTC)
        else:
            as_of_val = datetime.fromisoformat(str(raw_as_of))
            if as_of_val.tzinfo is None:
                as_of_val = as_of_val.replace(tzinfo=UTC)

        generations_val = tuple(dict(g) for g in data.get("generations", ()))
        extra_val = dict(data.get("extra", {}))

        return cls(
            run_id=str(data["run_id"]),
            d=d_val,
            as_of=as_of_val,
            arm=str(data["arm"]),
            git_sha=str(data["git_sha"]),
            uv_lock_sha=str(data["uv_lock_sha"]),
            weights_sha=str(data["weights_sha"]),
            config_sha=str(data["config_sha"]),
            stage_tuple_sha=str(data.get("stage_tuple_sha", "")),
            data_quality=str(data.get("data_quality", "FULL")),
            model_tier=str(data.get("model_tier", "NONE")),
            degraded_mode=bool(data.get("degraded_mode", False)),
            generations=generations_val,
            extra=extra_val,
        )

    def to_event(self, *, seq: int = 1, prev_hash: str = GENESIS_HASH) -> LedgerEvent:
        """Wrap this manifest as the genesis LedgerEvent (or specified seq)."""
        return LedgerEvent.create(
            seq=seq,
            ts=self.as_of,
            run_id=self.run_id,
            kind=EventKind.RUN_MANIFEST,
            payload=self.to_dict(),
            prev_hash=prev_hash,
        )
