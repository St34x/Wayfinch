# SPDX-License-Identifier: Apache-2.0
"""Shared helpers of the WS upgrade-authentication tests: an app whose ``/ws`` route authenticates and then upgrades,
a registry spy, and one-shot exchanges against a loopback ``TestServer``."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cert_helpers import HEADER, device_cert, fingerprint, make_ca, pem_escaped

from glasses.edge_cert import EdgeCert
from glasses.registry import Device, Registry
from glasses.server_auth import AuthDeps, authenticate, unauthorized

TEST_CA = make_ca("glasses device CA test")
CERT_A, CERT_B = device_cert(TEST_CA), device_cert(TEST_CA)
FP = fingerprint(CERT_A)  # the SHA-256 of a real device certificate: the identity the server derives from it
OTHER_FP = fingerprint(CERT_B)
CERT_VALUES = {FP: pem_escaped(CERT_A), OTHER_FP: pem_escaped(CERT_B)}
EDGE_CERT = EdgeCert(HEADER, "pem-urlescaped", lambda: (TEST_CA.cert,))  # the real checker over the test CA


def new_fingerprints(count: int) -> list[str]:
    """*count* more device certificates of the test CA; their fingerprints work with ``creds`` and ``cert_header``."""
    made = [device_cert(TEST_CA) for _ in range(count)]
    CERT_VALUES.update({fingerprint(cert): pem_escaped(cert) for cert in made})
    return [fingerprint(cert) for cert in made]


def cert_header(fingerprint: str = FP) -> dict[str, str]:
    """The forwarded certificate header (as nginx sends it) of the test device with *fingerprint*."""
    return {HEADER: CERT_VALUES[fingerprint]}


def upgrade_headers() -> dict[str, str]:
    """The client half of a WebSocket handshake; the nonce is random per call (no key-looking literal in the tree)."""
    nonce = base64.b64encode(os.urandom(16)).decode()
    return {"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": nonce}


@dataclass(frozen=True)
class Reply:
    status: int
    content_type: str
    headers: Mapping[str, str]
    body: bytes


@dataclass
class SpyRegistry:
    """Stands in for ``Registry``: counts calls, can be slow (blocking sleep) or raise."""

    device: Device | None = None
    tombstone: bool = False
    error: BaseException | None = None
    delay: float = 0.0
    calls: list[str] = field(default_factory=list)

    def lookup(self, token: Any, fingerprint: Any) -> tuple[Device | None, bool]:
        self.calls.append("lookup")
        self._wait()
        return (self.device, False) if self.device is not None else (None, self.tombstone)

    def _wait(self) -> None:
        if self.delay:
            time.sleep(self.delay)  # a blocking registry read: must never stall the loop
        if self.error is not None:
            raise self.error


def always(value: bool) -> Callable[[], Awaitable[bool]]:
    async def gate() -> bool:
        return value

    return gate


def deps_for(registry: Any, gate: bool = True, revoked: frozenset[str] = frozenset()) -> AuthDeps:
    return AuthDeps(gate_ok_async=always(gate), registry=registry, is_revoked=lambda device_id: device_id in revoked)


def enrolled(tmp_path: Path, fingerprint: str = FP) -> tuple[Registry, Device, str]:
    registry = Registry(tmp_path / "state")
    device, token = registry.add("glasses", fingerprint, ["main"])
    return registry, device, token


def make_app(deps: AuthDeps, handled: list[str]) -> web.Application:
    """``/ws`` as the server orders it: the verified certificate (401 without one), then ``authenticate``."""

    async def ws(request: web.Request) -> web.StreamResponse:
        verified = EDGE_CERT.fingerprint(request)
        if verified is None:
            raise unauthorized()
        device = await authenticate(request, deps, verified)
        handled.append(device.device_id)
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        await sock.close()
        return sock

    async def ping(request: web.Request) -> web.Response:
        return web.Response(text="pong")

    app = web.Application()
    app.router.add_get("/ws", ws)
    app.router.add_get("/ping", ping)
    return app


def creds(token: str, fingerprint: str = FP) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **cert_header(fingerprint)}


async def fetch(client: TestClient, headers: Mapping[str, str], path: str = "/ws") -> Reply:
    async with client.get(path, headers={**upgrade_headers(), **headers}) as response:
        return Reply(response.status, response.content_type, dict(response.headers), await response.read())


def get(app: web.Application, headers: Mapping[str, str]) -> Reply:
    """One upgrade request with *headers* (the upgrade headers are added)."""

    async def main() -> Reply:
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            return await fetch(client, headers)

    return asyncio.run(main())


def refusal(reply: Reply, status: int, error: str) -> bool:
    return reply.status == status and reply.headers["Content-Type"] == "application/json" and reply.body == _body(error)


def _body(error: str) -> bytes:
    return json.dumps({"error": error}, separators=(",", ":")).encode()
