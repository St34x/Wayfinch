# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b): the peer check comes first; the production runner (``build_runner``) never echoes a credential
line, not in a response body and not in any log record; handlers are not cancelled; ``on_cleanup`` closes the pool."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import threading
import time
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from server_auth_helpers import CERT_VALUES, EDGE_CERT, FP, SpyRegistry, creds
from server_helpers import Wiring, app_for, raw, serving, wiring

from glasses.registry import Device
from glasses.server import build_app, build_runner

from test_adapter import new_adapter
from test_gate import adapter_module  # noqa: F401 - fixture
from test_server_auth_stalled_gate import BOUND, stalled

TOKEN = "S3ntinelTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
FORGED_FP = "ab" * 32
FP_SENTINEL = FP  # the verified fingerprint: never in a response or a log line
CERT_SENTINEL = CERT_VALUES[FP][90:150]  # a piece of the forwarded certificate


class Events(SpyRegistry):
    """A registry stand-in that records every call in a shared list."""

    log: list[str]

    def lookup(self, token: Any, fingerprint: Any) -> tuple[Device | None, bool]:
        self.log.append("lookup")
        return super().lookup(token, fingerprint)


def recording(w: Wiring) -> tuple[list[str], Events]:
    events: list[str] = []
    spy = Events()
    spy.log = events
    return events, spy


async def one_get(app: web.Application, path: str, headers: dict[str, str]) -> tuple[int, bytes]:
    async with TestClient(TestServer(app, host="127.0.0.1")) as client, client.get(path, headers=headers) as response:
        return response.status, await response.read()


@pytest.mark.parametrize("predicate", ["refuses", "raises"])
def test_a_refused_peer_gets_the_unknown_path_404_and_reaches_no_gate_registry_or_revocation_check(tmp_path, predicate):
    w = wiring(tmp_path)
    events, spy = recording(w)

    async def gate() -> bool:
        events.append("gate")
        return True

    def revoked(_id: str) -> bool:
        events.append("is_revoked")
        return False

    def peer_allowed(request: web.Request) -> bool:
        events.append("peer")
        if predicate == "raises":
            raise RuntimeError("peer table unreadable")
        return False

    def certificate(request: web.BaseRequest) -> str | None:
        events.append("cert")
        return EDGE_CERT.fingerprint(request)

    deps = w.deps(registry=spy, gate_ok_async=gate, is_revoked=revoked, peer_allowed=peer_allowed)
    app = build_app(dataclasses.replace(deps, client_fingerprint=certificate))
    forged = {**w.headers(), "X-Client-Cert-Fp": FORGED_FP}  # a valid token with a forged forwarded fingerprint

    async def main() -> tuple[tuple[int, bytes], tuple[int, bytes]]:
        return await one_get(app, "/ws", forged), await one_get(app, "/nope", {})

    refused, unknown = asyncio.run(main())
    assert refused == unknown and refused[0] == 404
    assert events == ["peer"]  # nothing else was called: not the certificate check, the gate or a registry method


def test_the_peer_check_runs_before_the_gate_the_registry_and_the_revocation_check(tmp_path):
    w = wiring(tmp_path)
    events, spy = recording(w)
    spy.device = w.device

    async def gate() -> bool:
        events.append("gate")
        return True

    def revoked(_id: str) -> bool:
        events.append("is_revoked")
        return False

    def peer_allowed(request: web.Request) -> bool:
        events.append("peer")
        return True

    def certificate(request: web.BaseRequest) -> str | None:
        events.append("cert")
        return EDGE_CERT.fingerprint(request)

    deps = w.deps(registry=spy, gate_ok_async=gate, is_revoked=revoked, peer_allowed=peer_allowed)
    app = build_app(dataclasses.replace(deps, client_fingerprint=certificate))

    async def main() -> None:
        async with serving(app) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await ws.close()

    asyncio.run(asyncio.wait_for(main(), 10))
    assert events == ["peer", "cert", "gate", "lookup", "is_revoked"]


def test_build_runner_keeps_handler_cancellation_off(tmp_path):
    runner = build_runner(app_for(wiring(tmp_path)))
    assert (
        runner._kwargs["handler_cancellation"] is False
    )  # the mode TestServer does not use (aiohttp test_utils.py:129)


def test_a_hang_up_does_not_cancel_a_running_handler_under_build_runner(tmp_path):
    w = wiring(tmp_path)
    release = threading.Event()
    finished: list[str] = []

    class Slow(SpyRegistry):
        def lookup(self, token: Any, fingerprint: Any) -> tuple[Device | None, bool]:
            release.wait(10)
            finished.append("read")
            return super().lookup(token, fingerprint)

    async def main() -> None:
        app = build_app(w.deps(registry=Slow()))
        async with serving(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            lines = "".join(f"{k}: {v}\r\n" for k, v in w.headers().items())
            writer.write(f"GET /ws HTTP/1.1\r\nHost: x\r\n{lines}\r\n".encode())
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()  # hang up while the registry read is blocked
            await asyncio.sleep(0.2)
            release.set()
            await asyncio.sleep(0.3)

    try:
        asyncio.run(asyncio.wait_for(main(), 20))
    finally:
        release.set()
    assert finished == ["read"]


def upgrade_line(header_line: bytes) -> bytes:
    return (
        b"GET /ws HTTP/1.1\r\nHost: x\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\n"
        b"Sec-WebSocket-Key: " + b"dGhlIHNhbXBsZSBub25jZQ==" + b"\r\n" + header_line + b"\r\n"
        b"X-Client-Cert: " + CERT_VALUES[FP].encode() + b"\r\n\r\n"
    )


BAD_LINES = {
    "control byte 0x01": b"Authorization: Bearer " + TOKEN.encode() + b"\x01",
    "control byte 0x7f": b"Authorization: Bearer " + TOKEN.encode() + b"\x7f",
    "over-long line": b"Authorization: Bearer " + TOKEN.encode() + b"A" * 9_000,
    "obs-fold continuation": b"Authorization: Bearer " + TOKEN.encode() + b"\r\n continued-" + FP_SENTINEL.encode(),
}


@pytest.mark.parametrize("case", sorted(BAD_LINES))
def test_a_malformed_credential_line_is_echoed_neither_in_the_400_body_nor_in_any_log_record(tmp_path, caplog, case):
    caplog.set_level(logging.DEBUG)  # the root logger: every logger's records are captured
    w = wiring(tmp_path)

    async def main() -> bytes:
        async with serving(app_for(w)) as port:
            return await raw(port, upgrade_line(BAD_LINES[case]))

    answer = asyncio.run(asyncio.wait_for(main(), 20))
    assert re.match(rb"HTTP/1\.[01] 400 ", answer), answer[:80]
    logged = caplog.text + "".join(str(r.args) + str(r.exc_info) for r in caplog.records)
    for secret in (TOKEN, FP_SENTINEL, CERT_SENTINEL):
        assert secret.encode() not in answer
        assert secret not in logged
    assert "Traceback" not in caplog.text


def test_the_default_access_log_names_no_credential(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)

    async def main() -> int:
        url = "http://127.0.0.1:{}/ws"
        async with serving(app_for(w)) as port, aiohttp.ClientSession() as session:
            response = await session.get(url.format(port), headers=creds(TOKEN, FP_SENTINEL))
            async with response:
                return response.status

    assert asyncio.run(asyncio.wait_for(main(), 20)) == 401
    access = [r for r in caplog.records if r.name == "aiohttp.access"]
    assert access, "the access log did not run: the check below would be vacuous"
    assert all(TOKEN not in r.getMessage() and FP_SENTINEL not in r.getMessage() for r in access)
    assert TOKEN not in caplog.text and FP_SENTINEL not in caplog.text and CERT_SENTINEL not in caplog.text


def test_the_adapters_bound_gate_check_is_the_one_in_effect_in_the_app(
    tmp_path,
    adapter_module,  # noqa: F811 - the imported fixture is used by name
):
    adapter = new_adapter(adapter_module)
    release = stalled(adapter)
    w = wiring(tmp_path)
    deps = w.deps(gate_ok_async=adapter.gate_ok_async)  # the bound method, not a lambda: the 3 s bound of T1.209
    assert deps.gate_ok_async == adapter.gate_ok_async

    async def main() -> tuple[int, float]:
        async with serving(build_app(deps)) as port, aiohttp.ClientSession() as session:
            started = time.monotonic()
            async with session.get(f"http://127.0.0.1:{port}/ws", headers=creds(TOKEN, FP)) as response:
                return response.status, time.monotonic() - started

    try:
        status, elapsed = asyncio.run(asyncio.wait_for(main(), 20))
    finally:
        release.set()
    assert status == 503 and elapsed < BOUND  # the adapter's refusal after about 3 s, not the 5 s GATE_TIMEOUT


def hop_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name.startswith("glasses-auth")}


def test_one_pool_serves_the_whole_app_and_on_cleanup_leaves_none_of_its_threads(tmp_path):
    w = wiring(tmp_path)
    before = hop_threads()
    seen: list[int] = []

    async def main() -> None:
        async with serving(app_for(w)) as port, aiohttp.ClientSession() as session:
            for _ in range(20):  # twenty requests, each of which reads the registry in the hop
                response = await session.get(f"http://127.0.0.1:{port}/ws", headers=creds(TOKEN, FP))
                response.release()
                seen.append(len(hop_threads() - before))

    asyncio.run(asyncio.wait_for(main(), 20))
    assert 0 < max(seen) <= 4  # one pool of HOP_WORKERS threads, not one per request or per AuthDeps
    deadline = time.monotonic() + 5
    while hop_threads() - before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert hop_threads() - before == set()  # the runner's cleanup ran on_cleanup, which closed the hop
