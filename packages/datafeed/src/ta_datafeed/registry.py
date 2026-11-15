"""Source registry: primary and secondary exchange archive failover and reconciliation.

Milestone M2.11: Primary source is NSEArchiveSource; secondary / failover is
BSEArchiveSource. Reconciles dual-listed securities on ISIN to prevent
double-counting, with NSE taking precedence for prices and turnover.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import polars as pl

from .http import FetchError
from .sources.bse_archive import BSEArchiveSource
from .sources.nse_archive import NSEArchiveSource, SessionResult

log = logging.getLogger(__name__)

__all__ = ["SourceRegistry", "reconcile_isin_bars"]


def reconcile_isin_bars(
    nse_bars: pl.DataFrame | None,
    bse_bars: pl.DataFrame | None,
) -> pl.DataFrame:
    """Combine NSE and BSE bars for a session without double-counting dual-listed scrips.

    NSE is primary. Any BSE row whose ISIN (when non-empty) or symbol is
    already present in nse_bars for date d is dropped. BSE-only listings
    are retained and appended to the panel.
    """
    if nse_bars is None or nse_bars.is_empty():
        return bse_bars if bse_bars is not None else pl.DataFrame()
    if bse_bars is None or bse_bars.is_empty():
        return nse_bars

    # Existing ISINs in NSE bars (ignoring null or empty string)
    if "isin" in nse_bars.columns:
        nse_isins = set(
            nse_bars.filter(pl.col("isin").is_not_null() & (pl.col("isin") != ""))["isin"].to_list()
        )
    else:
        nse_isins = set()

    nse_symbols = set(nse_bars["symbol"].to_list())

    # Filter BSE bars: exclude any row that matches on ISIN or symbol
    bse_filtered = bse_bars
    if nse_isins and "isin" in bse_filtered.columns:
        bse_filtered = bse_filtered.filter(
            (~pl.col("isin").is_in(list(nse_isins))) | pl.col("isin").is_null() | (pl.col("isin") == "")
        )
    if nse_symbols:
        bse_filtered = bse_filtered.filter(~pl.col("symbol").is_in(list(nse_symbols)))

    if bse_filtered.is_empty():
        return nse_bars

    combined = pl.concat([nse_bars, bse_filtered], how="diagonal_relaxed")
    if "symbol" in combined.columns and "d" in combined.columns:
        return combined.sort(["symbol", "d"])
    return combined


class SourceRegistry:
    """Manages primary (NSE) and secondary (BSE) archives with failover & reconciliation."""

    def __init__(
        self,
        nse_source: NSEArchiveSource,
        bse_source: BSEArchiveSource | None = None,
    ) -> None:
        self.nse_source = nse_source
        self.bse_source = bse_source

    def fetch_session(self, d: date, *, reconcile: bool = True) -> SessionResult:
        """Fetch session bars, falling back to BSE on FetchError and reconciling dual listings."""
        try:
            nse_res = self.nse_source.fetch_session(d)
        except FetchError as e:
            log.warning("NSE fetch_session failed on %s: %s; falling back to BSE", d, e)
            if self.bse_source is not None:
                return self.bse_source.fetch_session(d)
            raise

        if not reconcile or self.bse_source is None:
            return nse_res

        # If NSE had no bars (e.g. non-trading day or missing), try BSE
        if not nse_res.was_session or nse_res.bars is None or nse_res.bars.is_empty():
            try:
                bse_res = self.bse_source.fetch_session(d)
                if bse_res.was_session and bse_res.bars is not None:
                    return bse_res
            except Exception as e:  # noqa: BLE001
                log.warning("BSE fallback failed for %s: %s", d, e)
            return nse_res

        # If NSE succeeded, fetch BSE to include BSE-only listings
        try:
            bse_res = self.bse_source.fetch_session(d)
            if bse_res.was_session and bse_res.bars is not None and not bse_res.bars.is_empty():
                merged = reconcile_isin_bars(nse_res.bars, bse_res.bars)
                return SessionResult(d, merged, was_session=True)
        except Exception as e:  # noqa: BLE001
            log.warning("BSE supplementary fetch failed for %s: %s; returning NSE only", d, e)

        return nse_res

    def fetch_range(
        self,
        start: date,
        end: date,
        *,
        reconcile: bool = True,
    ) -> list[SessionResult]:
        """Fetch every session in [start, end], weekends included (NSE trades some)."""
        out: list[SessionResult] = []
        d = start
        while d <= end:
            try:
                out.append(self.fetch_session(d, reconcile=reconcile))
            except Exception as e:  # noqa: BLE001
                log.warning("Registry session %s failed: %s", d, e)
                out.append(SessionResult(d, None, was_session=False))
            d += timedelta(days=1)
        return out
