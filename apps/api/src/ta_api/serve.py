"""Run the API on BOTH loopback addresses: 127.0.0.1 and ::1.

Why this exists: browsers resolve `localhost` to the IPv6 loopback `::1`
first. Uvicorn binds a single host, and with `--host 127.0.0.1` a browser
pointed at `http://localhost:8000` was refused outright -- while `curl`
silently fell back to IPv4, so every command-line health check passed and
the dashboard looked "down" only from the one place that mattered.

The obvious fix, `--host ::` (or `0.0.0.0`), would also bind every network
interface and publish an unauthenticated API to the local network. So this
opens exactly two sockets, one per loopback family, and hands both to one
uvicorn server. Still localhost-only.
"""
from __future__ import annotations

import logging
import os
import socket

import uvicorn

log = logging.getLogger(__name__)

PORT = int(os.environ.get("TA_API_PORT", "8000"))


def _bind(family: socket.AddressFamily, host: str) -> socket.socket | None:
    s = socket.socket(family, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        # v6-only, so this socket cannot also claim the IPv4 side.
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    try:
        s.bind((host, PORT))
    except OSError as e:
        # A machine with IPv6 disabled should still serve IPv4.
        log.warning("could not bind %s:%d (%s); continuing without it", host, PORT, e)
        s.close()
        return None
    s.listen(128)
    s.setblocking(False)
    return s


def main() -> None:
    sockets = [s for s in (_bind(socket.AF_INET, "127.0.0.1"),
                           _bind(socket.AF_INET6, "::1")) if s is not None]
    if not sockets:
        raise SystemExit(f"could not bind port {PORT} on any loopback address")
    config = uvicorn.Config("ta_api.main:app", log_level="warning")
    uvicorn.Server(config).run(sockets=sockets)


if __name__ == "__main__":
    main()
