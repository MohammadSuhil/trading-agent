"""Polite HTTP client.

Three behaviours this enforces, each learned from a real failure mode:

1. **Per-host rate limiting.** NSE and news sites will block a client that
   hammers them, and being blocked mid-backfill is expensive. One token bucket
   per host, conservative by default.

2. **Immutable-response disk cache.** A bhavcopy for 2024-03-15 will never
   change. Re-downloading it during development wastes bandwidth and goodwill,
   so a response keyed `(url)` under `data/raw/` is fetched exactly once, ever.

3. **Raw bytes land on disk BEFORE anything parses them.** If the parser has a
   bug -- and it will, NSE changes CSV headers without notice -- the fix is a
   re-parse, not a re-download. Everything downstream is reconstructible from
   `data/raw/`.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

import httpx

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: Minimum seconds between requests, per host. Deliberately generous.
HOST_DELAYS: dict[str, float] = {
    "nsearchives.nseindia.com": 1.0,
    "www.nseindia.com": 5.0,
    "niftyindices.com": 2.0,
    "api.bseindia.com": 2.0,
    "www.bseindia.com": 2.0,
    "_default": 2.0,
}


class FetchError(RuntimeError):
    pass


@dataclass
class _Bucket:
    delay: float
    last: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self) -> None:
        with self.lock:
            gap = time.monotonic() - self.last
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self.last = time.monotonic()


class PoliteClient:
    """Rate-limited, caching HTTP client.

    `raw_root` is where immutable bodies are archived. Pass `cache=False` for
    endpoints whose content genuinely changes (a live quote, a moving index).
    """

    def __init__(
        self,
        raw_root: Path | str = "data/raw",
        *,
        timeout: float = 45.0,
        user_agent: str = DEFAULT_UA,
    ) -> None:
        self.raw_root = Path(raw_root)
        self._buckets: dict[str, _Bucket] = {}
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def _bucket(self, host: str) -> _Bucket:
        if host not in self._buckets:
            self._buckets[host] = _Bucket(HOST_DELAYS.get(host, HOST_DELAYS["_default"]))
        return self._buckets[host]

    def _cache_path(self, url: str, subdir: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        name = url.rsplit("/", 1)[-1].split("?")[0] or "body"
        return self.raw_root / subdir / f"{digest}_{name}"

    def get_bytes(
        self,
        url: str,
        *,
        subdir: str = "misc",
        reuse: bool = True,
        archive: bool = True,
        headers: dict[str, str] | None = None,
        allow_404: bool = False,
    ) -> bytes | None:
        """Fetch `url`, archiving the body. Returns None on an allowed 404.

        A 404 is normal and expected when probing whether a date was a trading
        session -- NSE simply does not publish a bhavcopy for a holiday, which
        makes 404 a meaningful *answer*, not an error.

        `reuse` and `archive` are deliberately SEPARATE knobs, because the two
        questions they answer are independent and conflating them loses data.
        A past bhavcopy never changes, so it is both reused and archived. An
        index constituent list *does* change, so it must never be reused -- but
        it must still be archived, or the dated membership snapshot that
        `md.index_members` depends on is gone forever the moment NSE updates
        the file. Principle 3 is "raw bytes are immutable", not "raw bytes are
        cached".
        """
        path = self._cache_path(url, subdir)
        if reuse and path.exists() and path.stat().st_size > 0:
            return path.read_bytes()

        host = httpx.URL(url).host or "_default"
        self._bucket(host).wait()

        try:
            r = self._client.get(url, headers=headers or {})
        except httpx.HTTPError as e:
            raise FetchError(f"{url}: {e}") from e

        if r.status_code == 404 and allow_404:
            return None
        if r.status_code != 200:
            raise FetchError(f"{url}: HTTP {r.status_code}")

        body = r.content
        if archive:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)        # raw bytes BEFORE any parsing
        return body

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
