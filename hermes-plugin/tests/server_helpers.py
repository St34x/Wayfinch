# SPDX-License-Identifier: Apache-2.0
"""Shared helpers of the WS server tests: the real schema, a ``ServerDeps`` with spies, a loopback server started
through ``build_runner`` (the production runner, unlike ``TestServer``), and a raw-socket sender."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web
from jsonschema import Draft202012Validator
from server_auth_helpers import EDGE_CERT, FP, always, creds, upgrade_headers

from glasses.generated.protocol_types import Frame
from glasses.registry import Device, Registry
from glasses.server import ServerDeps, build_app, build_runner

PROTOCOL = Path(__file__).resolve().parents[2] / "protocol"
SCHEMA = Draft202012Validator(json.loads((PROTOCOL / "envelope.schema.json").read_text(encoding="utf-8")))
HELLO = json.loads((PROTOCOL / "fixtures" / "valid" / "hello.json").read_text(encoding="utf-8"))
PING = json.loads((PROTOCOL / "fixtures" / "valid" / "ping.json").read_text(encoding="utf-8"))
ERROR = json.loads((PROTOCOL / "fixtures" / "valid" / "error.json").read_text(encoding="utf-8"))
MSG_IN = json.loads((PROTOCOL / "fixtures" / "valid" / "msg.in.json").read_text(encoding="utf-8"))


@dataclass
class Wiring:
    """The injected parts with spies: ``frames`` is what ``on_frame`` got, ``peers`` the predicate's calls."""

    registry: Registry
    device: Device
    token: str
    frames: list[tuple[str, str]] = field(default_factory=list)
    peers: list[str] = field(default_factory=list)
    revoked: bool = False
    allow_peer: bool = True

    def deps(self, **changes: Any) -> ServerDeps:
        async def on_frame(device: Device, frame: Frame) -> None:
            self.frames.append((device.device_id, frame["type"]))

        def peer_allowed(request: web.Request) -> bool:
            self.peers.append(request.path)
            return self.allow_peer

        values: dict[str, Any] = {
            "gate_ok_async": always(True),
            "registry": self.registry,
            "is_revoked": lambda _id: self.revoked,
            "peer_allowed": peer_allowed,
            "client_fingerprint": EDGE_CERT.fingerprint,
            "on_frame": on_frame,
            "schema": SCHEMA,
            "max_audio_sec": 30,
        }
        return ServerDeps(**{**values, **changes})

    def headers(self) -> dict[str, str]:
        return {**upgrade_headers(), **creds(self.token)}


def wiring(tmp_path: Path, lanes: tuple[str, ...] = ("main",)) -> Wiring:
    registry = Registry(tmp_path / "state")
    device, token = registry.add("glasses", FP, list(lanes))
    return Wiring(registry, device, token)


@asynccontextmanager
async def serving(app: web.Application) -> AsyncIterator[int]:
    """The app on a loopback port, run by ``build_runner`` exactly as production runs it."""
    runner = build_runner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        yield int(runner.addresses[0][1])
    finally:
        await runner.cleanup()


def raw_exchange(port: int, data: bytes, wait: float = 0.5) -> bytes:
    """Send *data* as it is and read what comes back until the server stops or *wait* seconds pass."""
    chunks: list[bytes] = []
    with socket.create_connection(("127.0.0.1", port), timeout=wait) as sock:
        try:
            sock.sendall(data)
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        except (TimeoutError, ConnectionResetError, BrokenPipeError):
            pass  # the server may close on a refused request before it has read everything we send
    return b"".join(chunks)


async def raw(port: int, data: bytes) -> bytes:
    return await asyncio.get_running_loop().run_in_executor(None, raw_exchange, port, data)


def app_for(w: Wiring, **changes: Any) -> web.Application:
    return build_app(w.deps(**changes))
