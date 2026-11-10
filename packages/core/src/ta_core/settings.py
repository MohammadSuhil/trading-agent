"""Application settings data structure.

Zero I/O: This module defines `AppSettings` and a pure `from_mapping` parser.
It never accesses `os.environ` or reads files directly (enforcing Invariant I-05).
The application layer (apps/api or CLI) reads the environment and passes the
mapping in.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

__all__ = ["AppSettings"]


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Global configuration settings for the trading agent platform."""

    clickhouse_host: str = "127.0.0.1"
    clickhouse_port: int = 8123
    mongo_uri: str = "mongodb://127.0.0.1:27017/?replicaSet=rs0"
    redis_url: str = "redis://127.0.0.1:6379/0"
    raw_root: str = "data/raw"
    equity_base: float = 500_000.0
    tradeable_index: str = "NIFTY500"
    scored_index: str = "NIFTY500"

    @classmethod
    def from_mapping(cls, env: Mapping[str, str]) -> Self:
        """Parse configuration from a string mapping without performing any I/O.

        Accepts case-insensitive keys (e.g., CLICKHOUSE_HOST or clickhouse_host).

        >>> s = AppSettings.from_mapping({"CLICKHOUSE_PORT": "9000", "EQUITY_BASE": "1000000"})
        >>> s.clickhouse_port, s.equity_base
        (9000, 1000000.0)
        """
        lookup = {k.lower(): v for k, v in env.items()}

        def get_str(key: str, default: str) -> str:
            return lookup.get(key, default)

        def get_int(key: str, default: int) -> int:
            val = lookup.get(key)
            return int(val) if val is not None else default

        def get_float(key: str, default: float) -> float:
            val = lookup.get(key)
            return float(val) if val is not None else default

        return cls(
            clickhouse_host=get_str("clickhouse_host", "127.0.0.1"),
            clickhouse_port=get_int("clickhouse_port", 8123),
            mongo_uri=get_str("mongo_uri", "mongodb://127.0.0.1:27017/?replicaSet=rs0"),
            redis_url=get_str("redis_url", "redis://127.0.0.1:6379/0"),
            raw_root=get_str("raw_root", "data/raw"),
            equity_base=get_float("equity_base", 500_000.0),
            tradeable_index=get_str("tradeable_index", "NIFTY500"),
            scored_index=get_str("scored_index", "NIFTY500"),
        )
