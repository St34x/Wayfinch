# SPDX-License-Identifier: Apache-2.0
"""The upgrade cap keyed on the VERIFIED client-certificate fingerprint (T1.184c), over the real unix listener.

(a) 50 concurrent wrong-token upgrades with one certificate take at most the cap (8) in registry reads, and a second
certificate upgrading at the same time gets its ``101`` (not ``503``); (b) the same with the empty ``remote`` of the
unix socket and with ``remote`` unset; (c) a request whose certificate fails verification never occupies a slot and
never reaches the registry; (d) every slot is given back (``UpgradeCap._open == {}``)."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from edge_helpers import short_dir
from server_auth_helpers import FP, OTHER_FP, creds, upgrade_headers
from server_helpers import Wiring, app_for, wiring

from glasses import connections
from glasses.connections import UPGRADES_PER_CERT_MAX
from glasses.edge_auth import edge_peer_allowed
from glasses.edge_listener import EdgeListener
from glasses.registry import Device
from glasses.server import _CAP, serve

SLOW = 0.5  # seconds a failing registry read takes: the flood is in flight together
FLOOD = 50


class OneGoodDevice:
    """Admits the wiring's token with the second certificate; every other read is slow and finds nothing."""

    def __init__(self, w: Wiring) -> None:
        self.device, self.token = w.device, w.token
        self.reads: list[str] = []
        self._lock = threading.Lock()

    def lookup(self, token: Any, fingerprint: Any) -> tuple[Device | None, bool]:
        with self._lock:
            self.reads.append(fingerprint)
        if fingerprint == OTHER_FP and token == self.token:
            return self.device, False
        time.sleep(SLOW)
        return None, False


async def _upgrade(path: Path, headers: dict[str, str]) -> int:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        lines = [
            "GET /ws HTTP/1.1",
            "Host: example.com",
            *(f"{k}: {v}" for k, v in {**upgrade_headers(), **headers}.items()),
        ]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        return int((await asyncio.wait_for(reader.read(65536), 20)).split(b" ", 2)[1])
    finally:
        writer.close()


@contextmanager
def _remote(monkeypatch: pytest.MonkeyPatch, remote: str):
    if remote == "unset":
        monkeypatch.setattr(web.BaseRequest, "remote", property(lambda _self: None))
    yield


async def _serve(app: web.Application, run) -> Any:
    with short_dir() as d:
        listener = EdgeListener(str(d / "edge.sock"), os.geteuid())
        runner = await serve(app, listener)
        try:
            return await asyncio.wait_for(run(d / "edge.sock"), 60)
        finally:
            await listener.stop()
            await runner.cleanup()


@pytest.mark.parametrize("remote", ["unix-empty", "unset"])
def test_one_certificate_at_the_cap_does_not_refuse_another(tmp_path, monkeypatch, remote):
    w = wiring(tmp_path)
    registry = OneGoodDevice(w)
    app = app_for(w, registry=registry, peer_allowed=edge_peer_allowed(os.geteuid()))

    async def run(sock: Path) -> tuple[list[int], int]:
        flood = [asyncio.create_task(_upgrade(sock, creds("x" * 43, FP))) for _ in range(FLOOD)]
        await asyncio.sleep(0.2)  # the flood is at the server, eight reads in flight
        other = await _upgrade(sock, creds(w.token, OTHER_FP))
        return sorted(await asyncio.gather(*flood)), other

    with _remote(monkeypatch, remote):
        statuses, other = asyncio.run(_serve(app, run))
    assert other == 101  # the second certificate is not refused while the first one is at the cap
    assert statuses == [401] * UPGRADES_PER_CERT_MAX + [503] * (FLOOD - UPGRADES_PER_CERT_MAX)
    assert registry.reads.count(FP) == UPGRADES_PER_CERT_MAX
    assert app[_CAP]._open == {}


def test_a_refused_certificate_never_occupies_a_slot_or_reaches_the_registry(tmp_path, monkeypatch):
    w = wiring(tmp_path)
    registry = OneGoodDevice(w)
    app = app_for(w, registry=registry, peer_allowed=edge_peer_allowed(os.geteuid()))
    entered: list[str] = []
    real_slot = connections.UpgradeCap.slot

    def counting(self: connections.UpgradeCap, key: str):
        entered.append(key)
        return real_slot(self, key)

    monkeypatch.setattr(connections.UpgradeCap, "slot", counting)
    refused = [
        {"Authorization": f"Bearer {w.token}", "X-Client-Cert-Fp": OTHER_FP},  # the old header: never read
        {"Authorization": f"Bearer {w.token}", "X-Client-Cert": "not a certificate"},
        {"Authorization": f"Bearer {w.token}"},
    ]

    async def run(sock: Path) -> tuple[list[int], int]:
        statuses = await asyncio.gather(*(_upgrade(sock, refused[n % 3]) for n in range(30)))
        return list(statuses), await _upgrade(sock, creds(w.token, OTHER_FP))

    statuses, admitted = asyncio.run(_serve(app, run))
    assert statuses == [401] * 30 and admitted == 101
    assert entered == [OTHER_FP] and registry.reads == [OTHER_FP]  # only the verified one took a slot and a read
    assert app[_CAP]._open == {}
