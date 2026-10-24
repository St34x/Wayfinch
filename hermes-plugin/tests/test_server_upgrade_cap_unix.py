# SPDX-License-Identifier: Apache-2.0
"""The upgrade cap over the REAL unix listener (T1.184b). Over a unix socket ``request.remote`` is the empty
string for every client (and may be ``None``); the cap is keyed on the verified certificate (T1.184c), so nine
concurrent upgrades with ONE certificate from clients of the edge uid give exactly eight registry reads and one ``503``
without a registry call, whatever ``remote`` is, and an unset ``remote`` never crashes."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

import pytest
from aiohttp import web
from edge_helpers import short_dir
from server_auth_helpers import SpyRegistry, upgrade_headers
from server_helpers import app_for, wiring

from glasses.connections import UPGRADES_PER_CERT_MAX
from glasses.edge_auth import edge_peer_allowed
from glasses.edge_listener import EdgeListener
from glasses.server import serve

CLIENTS = UPGRADES_PER_CERT_MAX + 1
SLOW = 0.5  # seconds one registry read takes: all nine upgrades are in flight together
ACCESS = re.compile(r' "GET /ws" (\d{3}) \d+')


async def _upgrade(path: Path, headers: dict[str, str]) -> bytes:
    """One upgrade request over its own unix connection; the first chunk of the answer (head and body)."""
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        lines = ["GET /ws HTTP/1.1", "Host: example.com", *(f"{k}: {v}" for k, v in headers.items())]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        return await asyncio.wait_for(reader.read(65536), 10)
    finally:
        writer.close()


async def _nine_upgrades(tmp_path: Path, remotes: list[object]) -> tuple[list[bytes], SpyRegistry]:
    w = wiring(tmp_path)
    registry = SpyRegistry(delay=SLOW)  # no device: every read that runs answers 401
    gates: list[int] = []

    async def gate() -> bool:
        gates.append(1)
        return True

    allowed = edge_peer_allowed(os.geteuid())  # the production predicate: the peers are of the edge uid

    def peer_allowed(request: web.BaseRequest) -> bool:
        remotes.append(request.remote)
        return allowed(request)

    app = app_for(w, registry=registry, gate_ok_async=gate, peer_allowed=peer_allowed)
    headers = w.headers()
    with short_dir() as d:
        listener = EdgeListener(str(d / "edge.sock"), os.geteuid())
        runner = await serve(app, listener)
        try:
            replies = await asyncio.wait_for(
                asyncio.gather(*(_upgrade(d / "edge.sock", headers | upgrade_headers()) for _ in range(CLIENTS))), 30
            )
        finally:
            await listener.stop()
            await runner.cleanup()
    assert len(gates) == UPGRADES_PER_CERT_MAX  # the gate bound runs for the eight reads, never for the ninth
    return replies, registry


def _statuses(replies: list[bytes]) -> list[int]:
    return sorted(int(reply.split(b" ", 2)[1]) for reply in replies)


def test_nine_upgrades_over_the_unix_socket_with_an_empty_remote_cost_eight_registry_reads_and_one_503(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    remotes: list[object] = []
    replies, registry = asyncio.run(_nine_upgrades(tmp_path, remotes))
    assert remotes == [""] * CLIENTS  # the probe: aiohttp gives the empty string for an unnamed unix peer
    assert _statuses(replies) == [401] * UPGRADES_PER_CERT_MAX + [503]
    assert len(registry.calls) == UPGRADES_PER_CERT_MAX  # the ninth never reached the registry hop
    refused = [r for r in replies if r.startswith(b"HTTP/1.1 503")]
    assert len(refused) == 1 and refused[0].endswith(b'{"error":"unavailable"}')
    lines = [r.getMessage() for r in caplog.records if r.name == "aiohttp.access"]
    assert sorted(m.group(1) for m in map(ACCESS.search, lines) if m) == ["401"] * UPGRADES_PER_CERT_MAX + ["503"]
    assert all(line.startswith(' "GET /ws"') and "?" not in line for line in lines)  # empty remote, no query string


def test_an_unset_remote_is_the_same_global_cap_and_never_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(web.BaseRequest, "remote", property(lambda _self: None))
    remotes: list[object] = []
    replies, registry = asyncio.run(_nine_upgrades(tmp_path, remotes))
    assert remotes == [None] * CLIENTS
    assert _statuses(replies) == [401] * UPGRADES_PER_CERT_MAX + [503]  # no 500: the key is "" for None too
    assert len(registry.calls) == UPGRADES_PER_CERT_MAX
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
