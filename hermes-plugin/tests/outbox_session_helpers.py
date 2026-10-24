# SPDX-License-Identifier: Apache-2.0
"""The outbox over the real WS server (T1.09b): the adapter's frame router as ``on_frame`` and its ``send()``, an
``Outbox`` over a temp state dir as ``on_hello``, and a test client that keeps a seen-set by ``seq`` across
connections (a conforming de-duplicating phone). Built like ``connect()`` wires it, without the edge listener."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
from outbox_helpers import Clock, overflow
from server_helpers import HELLO, PING, SCHEMA, Wiring, serving, wiring

from glasses.outbox import FILE_NAME, Outbox, Store
from glasses.revoked_ids import RevokedIds
from glasses.server import CONNECTIONS, build_app

from test_adapter import new_adapter

EXTRA = {"lanes": ["assistant", "work"], "home_lane": "assistant"}
LANES = ("assistant", "work")


@dataclass
class Phone:
    """What the de-duplicating client saw: every frame on the wire, and each numbered frame once."""

    wire: list[dict[str, Any]] = field(default_factory=list)
    seen: dict[int, dict[str, Any]] = field(default_factory=dict)

    def take(self, frame: dict[str, Any]) -> None:
        self.wire.append(frame)
        if "seq" in frame:
            self.seen.setdefault(frame["seq"], frame)

    def texts(self) -> list[str]:
        return [self.seen[seq]["body"]["text"] for seq in sorted(self.seen) if self.seen[seq]["type"] == "msg.out"]


@dataclass
class Rig:
    adapter: Any
    w: Wiring
    box: Outbox
    state: Path
    phone: Phone = field(default_factory=Phone)


def rig(module: Any, tmp_path: Path, clock: Clock | None = None) -> Rig:
    adapter = new_adapter(module, EXTRA)
    w = wiring(tmp_path, LANES)
    state = tmp_path / "plugin-state"
    store = Store(state / FILE_NAME, clock or Clock(), overflow)
    box = Outbox(store, adapter.router.text.dedupe.forget, w.registry.list)  # as connect(): hello checks the registry
    adapter._edge = SimpleNamespace(outbox=box, registry=w.registry)  # what connect() keeps for send()
    return Rig(adapter, w, box, state)


def revoked_ids(r: Rig, read: Any = None) -> RevokedIds:
    """The frame check as ``connect()`` builds it: the overlay's place, the outbox and the registry file (T1.248)."""
    return RevokedIds(r.w.registry._file, read or r.w.registry.list, lambda _id: r.w.revoked, r.box.is_revoked)


@asynccontextmanager
async def served(r: Rig, revoked: RevokedIds | None = None, **changes: Any) -> AsyncIterator[int]:
    """The app as ``connect()`` wires it; with *revoked*, its check is ``is_revoked`` and its cut is bound (T1.248)."""
    changes.setdefault("on_frame", r.adapter.router.on_frame)
    changes.setdefault("is_revoked", revoked.is_revoked if revoked else lambda _id: r.w.revoked)  # else the overlay's
    changes.setdefault("on_revoked", r.box.on_revoked(r.w.registry.list))  # as connect() wires it
    changes.setdefault("on_closed", r.box.closed)
    if revoked is not None:
        await revoked.load()
    app = build_app(r.w.deps(on_hello=r.box.hello, **changes))
    r.adapter.router.bind(app[CONNECTIONS], SCHEMA, r.box)
    if revoked is not None:
        revoked.bind(app[CONNECTIONS].close_device, r.box.purge_soon)
    try:
        async with serving(app) as port:
            yield port
    finally:
        if revoked is not None:
            revoked.close()  # as _Edge.stop(): the poll and the read worker end first
        r.adapter._edge = None
        await r.box.close()
        await r.adapter.disconnect()  # the gate's worker pool


def hello(acked: int) -> str:
    frame = copy.deepcopy(HELLO)
    frame["body"]["lastAckedSeq"] = acked
    return json.dumps(frame)


async def until_pong(ws: aiohttp.ClientWebSocketResponse, phone: Phone) -> list[dict[str, Any]]:
    """Send a ping and take every frame up to and including the pong."""
    await ws.send_str(json.dumps(PING))
    frames = [await ws.receive_json(timeout=5)]
    while frames[-1]["type"] != "pong":
        frames.append(await ws.receive_json(timeout=5))
    for frame in frames:
        phone.take(frame)
    return frames


@asynccontextmanager
async def device(
    port: int, r: Rig, session: aiohttp.ClientSession, acked: int
) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
    ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers())
    try:
        await ws.send_str(hello(acked))
        yield ws
    finally:
        await ws.close()
