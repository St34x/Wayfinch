# SPDX-License-Identifier: Apache-2.0
"""Shared helpers of the edge-listener tests: a short scratch directory for the socket (``AF_UNIX`` paths are short),
the app served by the production ``serve`` on a real ``EdgeListener``, raw unix-socket exchanges, spies, another real
user, and the listening TCP sockets of this process."""

from __future__ import annotations

import asyncio
import os
import pwd
import shutil
import socket
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from glasses.edge_listener import EdgeListener
from glasses.registry import Device, Registry
from glasses.server import serve


@contextmanager
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="e"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def other_user() -> pwd.struct_passwd:
    """A real user that is neither root nor the user running the tests."""
    return next(p for p in pwd.getpwall() if p.pw_uid not in (0, os.geteuid()))


@asynccontextmanager
async def edge_serving(app: web.Application, path: Path, uid: int) -> AsyncIterator[EdgeListener]:
    listener = EdgeListener(str(path), uid)
    runner = await serve(app, listener)
    try:
        yield listener
    finally:
        await listener.stop()
        await runner.cleanup()


def unix_exchange(path: Path, data: bytes, wait: float = 0.5) -> bytes:
    chunks: list[bytes] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(wait)
        sock.connect(str(path))
        try:
            sock.sendall(data)
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        except (TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
    return b"".join(chunks)


async def raw_unix(path: Path, data: bytes, wait: float = 0.5) -> bytes:
    return await asyncio.get_running_loop().run_in_executor(None, unix_exchange, path, data, wait)


def request_bytes(method: str, path: str, headers: dict[str, str], body: bytes = b"") -> bytes:
    lines = [f"{method} {path} HTTP/1.1", "Host: localhost", *(f"{k}: {v}" for k, v in headers.items())]
    if body or method in ("POST", "PUT"):
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


@dataclass
class SpyRegistry:
    """Wraps a registry; records every ``lookup``."""

    inner: Registry
    lookups: list[str] = field(default_factory=list)

    def lookup(self, token: str, fingerprint: str) -> tuple[Device | None, bool]:
        self.lookups.append(fingerprint)
        return self.inner.lookup(token, fingerprint)


def listening_tcp_inodes() -> set[str]:
    """Inodes of the TCP sockets in LISTEN state that this process holds open; ``RuntimeError`` when no TCP table
    can be read (an empty answer would make "nothing listens" pass without a check)."""
    listening: set[str] = set()
    tables = 0
    for name in ("tcp", "tcp6"):
        try:
            rows = Path(f"/proc/self/net/{name}").read_text().splitlines()[1:]
        except OSError:
            continue
        tables += 1
        listening |= {row.split()[9] for row in rows if row.split()[3] == "0A"}
    if not tables:
        raise RuntimeError("no TCP table of this process is readable (/proc/self/net/tcp, tcp6)")
    mine: set[str] = set()
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        if target.startswith("socket:["):
            mine.add(target[8:-1])
    return mine & listening
