"""Hash-chained event ledger for deterministic auditability and rebuilds.

Invariant P-03: The ledger is hash-chained. In ClickHouse, ev.ledger_events is
ReplacingMergeTree ORDER BY seq. If two events ever share a sequence number,
ClickHouse's merge will silently drop one of them, corrupting the audit trail
without raising an error. verify_chain() strictly validates contiguous sequence
numbers (1, 2, ..., N), genesis initialization, and cryptographic SHA-256 links
so collisions and gaps fail immediately.

Storage in the ledger is canonical JSON, never pickle (ADR-0002): records that
must be replayable years later cannot depend on transient Python class definitions.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum, StrEnum
from typing import Any

import polars as pl

__all__ = [
    "GENESIS_HASH",
    "EventKind",
    "LedgerChainError",
    "LedgerEvent",
    "canonical_json",
    "compute_event_hash",
    "events_from_df",
    "events_to_df",
    "verify_chain",
]

#: The 64-character hex zero string representing the predecessor of event 1.
GENESIS_HASH: str = "0" * 64


class EventKind(StrEnum):
    """Closed enumeration of event kinds permitted in ev.ledger_events.

    Closed on purpose: attribution and replay depend on deterministic event
    dispatch. Ad-hoc string kinds make complete event projections impossible.
    """

    RUN_MANIFEST = "RUN_MANIFEST"
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    STOP_TRAILED = "STOP_TRAILED"
    POSITION_CLOSED = "POSITION_CLOSED"
    EQUITY_MARKED = "EQUITY_MARKED"
    ENTRY_FILL = "ENTRY_FILL"
    EXIT_FILL = "EXIT_FILL"


def _json_default(obj: Any) -> Any:
    """Deterministic JSON fallback serializer for dates, datetimes, and enums."""
    if isinstance(obj, datetime):
        ts = obj.replace(tzinfo=UTC) if obj.tzinfo is None else obj.astimezone(UTC)
        return ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Serializes a mapping deterministically to compact canonical JSON.

    Sorted keys, compact separators, UTC ISO timestamps, rejection of NaN/Infinity.

    >>> canonical_json({"b": 2, "a": 1})
    '{"a":1,"b":2}'
    >>> canonical_json({"date": date(2026, 11, 10)})
    '{"date":"2026-11-10"}'
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
    )


def compute_event_hash(
    seq: int,
    ts: datetime,
    run_id: str,
    kind: str,
    payload_json: str,
    prev_hash: str,
) -> str:
    """Compute the SHA-256 hash of a ledger event.

    Timestamps are normalised to UTC millisecond precision ISO-8601 strings.

    >>> compute_event_hash(
    ...     1,
    ...     datetime(2026, 11, 10, 9, 15, 0, tzinfo=UTC),
    ...     "RUN-001",
    ...     "RUN_MANIFEST",
    ...     "{}",
    ...     "0" * 64,
    ... )
    '9dbd45e1654f74ecb3beea35f03c71468dde05bdf6702643e8633fcd8e78edd4'
    """
    ts_utc = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    ts_iso = ts_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    raw = f"{seq}|{ts_iso}|{run_id}|{kind}|{payload_json}|{prev_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """An immutable, hash-linked record in ev.ledger_events.

    Matches infra/clickhouse/schema_v2.sql lines 146-155.
    """

    seq: int
    ts: datetime
    run_id: str
    kind: EventKind
    payload: dict[str, Any]
    prev_hash: str
    hash: str

    def payload_json(self) -> str:
        """Compact canonical JSON representation of the payload."""
        return canonical_json(self.payload)

    @classmethod
    def create(
        cls,
        *,
        seq: int,
        ts: datetime,
        run_id: str,
        kind: EventKind | str,
        payload: Mapping[str, Any],
        prev_hash: str = GENESIS_HASH,
    ) -> LedgerEvent:
        """Construct a validated LedgerEvent with its cryptographic SHA-256 hash."""
        event_kind = EventKind(kind) if isinstance(kind, str) else kind
        p_dict = dict(payload)
        p_json = canonical_json(p_dict)
        h = compute_event_hash(seq, ts, run_id, event_kind.value, p_json, prev_hash)
        return cls(
            seq=seq,
            ts=ts,
            run_id=run_id,
            kind=event_kind,
            payload=p_dict,
            prev_hash=prev_hash,
            hash=h,
        )


class LedgerChainError(AssertionError):
    """Raised when ledger integrity verification fails."""


def verify_chain(events: Sequence[LedgerEvent]) -> None:
    """Verify hash-chain integrity and sequence contiguity (Invariant P-03).

    Checks:
      1. Empty sequence is valid.
      2. seq starts at 1 and is strictly contiguous (1, 2, ..., len(events)).
         Any duplicate (seq collision) or gap raises LedgerChainError.
      3. First event has prev_hash == GENESIS_HASH; subsequent events have
         events[i].prev_hash == events[i-1].hash.
      4. Every event's hash matches compute_event_hash(...). Any mutated payload
         or tampered header raises LedgerChainError.
    """
    if not events:
        return

    for i, event in enumerate(events):
        expected_seq = i + 1
        if event.seq != expected_seq:
            raise LedgerChainError(
                f"Sequence contiguity violation at index {i}: expected seq {expected_seq}, "
                f"got {event.seq} (collision or gap)"
            )

        expected_prev = GENESIS_HASH if i == 0 else events[i - 1].hash
        if event.prev_hash != expected_prev:
            raise LedgerChainError(
                f"Broken hash link at seq {event.seq}: prev_hash {event.prev_hash} "
                f"does not match expected previous hash {expected_prev}"
            )

        expected_hash = compute_event_hash(
            seq=event.seq,
            ts=event.ts,
            run_id=event.run_id,
            kind=event.kind.value,
            payload_json=event.payload_json(),
            prev_hash=event.prev_hash,
        )
        if event.hash != expected_hash:
            raise LedgerChainError(
                f"Hash mismatch at seq {event.seq}: recorded {event.hash} != computed {expected_hash} "
                "(payload or header has been tampered with)"
            )


def events_to_df(events: Sequence[LedgerEvent]) -> pl.DataFrame:
    """Convert a sequence of LedgerEvents to a Polars DataFrame matching ev.ledger_events schema."""
    schema = {
        "seq": pl.UInt64,
        "ts": pl.Datetime("ms", "UTC"),
        "run_id": pl.Utf8,
        "kind": pl.Utf8,
        "payload": pl.Utf8,
        "prev_hash": pl.Utf8,
        "hash": pl.Utf8,
    }
    if not events:
        return pl.DataFrame(schema=schema)

    return pl.DataFrame(
        {
            "seq": [e.seq for e in events],
            "ts": [e.ts.astimezone(UTC) if e.ts.tzinfo else e.ts.replace(tzinfo=UTC) for e in events],
            "run_id": [e.run_id for e in events],
            "kind": [e.kind.value for e in events],
            "payload": [e.payload_json() for e in events],
            "prev_hash": [e.prev_hash for e in events],
            "hash": [e.hash for e in events],
        },
        schema=schema,
    )


def events_from_df(df: pl.DataFrame) -> list[LedgerEvent]:
    """Convert a Polars DataFrame matching ev.ledger_events schema to a list of LedgerEvents.

    Runs verify_chain on load.
    """
    if df.is_empty():
        return []

    events: list[LedgerEvent] = []
    for row in df.iter_rows(named=True):
        raw_ts = row["ts"]
        if isinstance(raw_ts, datetime):
            ts = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=UTC)
        else:
            ts = datetime.fromisoformat(str(raw_ts))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)

        payload = json.loads(row["payload"])
        events.append(
            LedgerEvent(
                seq=int(row["seq"]),
                ts=ts,
                run_id=str(row["run_id"]),
                kind=EventKind(str(row["kind"])),
                payload=payload,
                prev_hash=str(row["prev_hash"]),
                hash=str(row["hash"]),
            )
        )

    verify_chain(events)
    return events
