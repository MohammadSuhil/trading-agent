"""Deterministic identifiers for runs and recommendations.

Every nightly run and recommendation carries a stable identifier derived from
its primary inputs. This guarantees idempotency across retries: running the
pipeline twice for the same trading session and arm yields the same run_id,
preventing duplicate record accumulation in the ledger.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime

from ta_core.enums import Arm

__all__ = ["make_rec_id", "make_run_id"]


def make_run_id(
    d: date,
    arm: Arm | str = Arm.DETERMINISTIC,
    as_of: datetime | None = None,
) -> str:
    """Generate a deterministic 12-hex-character run identifier.

    The ID is the 12-char SHA-256 prefix of date, arm, and optional UTC as-of
    timestamp.

    >>> from datetime import date
    >>> make_run_id(date(2026, 11, 10))
    '3509dd4e2420'
    >>> make_run_id(date(2026, 11, 10), arm=Arm.DETERMINISTIC)
    '3509dd4e2420'
    """
    arm_str = arm.value if isinstance(arm, Arm) else str(arm)
    if as_of is not None:
        tz_aware = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
        payload = f"{d.isoformat()}|{arm_str}|{tz_aware.astimezone(UTC).isoformat()}"
    else:
        payload = f"{d.isoformat()}|{arm_str}"

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def make_rec_id(run_id: str, symbol: str, *, suffix: str = "") -> str:
    """Generate a stable recommendation identifier.

    >>> make_rec_id("abc123def456", "RELIANCE")
    'abc123def456-RELIANCE'
    >>> make_rec_id("abc123def456", "RELIANCE", suffix="SWING")
    'abc123def456-RELIANCE-SWING'
    """
    if suffix:
        return f"{run_id}-{symbol}-{suffix}"
    return f"{run_id}-{symbol}"
