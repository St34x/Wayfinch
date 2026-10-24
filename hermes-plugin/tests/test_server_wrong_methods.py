# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b), request methods on ``/ws``: a peer the predicate refuses gets the unknown-path ``404`` for every
method (it cannot tell that ``/ws`` exists), an allowed peer gets ``405`` for anything but ``GET``; neither reaches the
gate, the registry or the revocation check."""

from __future__ import annotations

import asyncio

import pytest
from server_auth_helpers import SpyRegistry
from server_helpers import app_for, raw, serving, wiring

METHODS = ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]


def request(method: str, path: str) -> bytes:
    return f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()


def status_line_and_body(reply: bytes) -> tuple[bytes, bytes]:
    head, _, body = reply.partition(b"\r\n\r\n")
    return head.split(b"\r\n")[0], body


@pytest.mark.parametrize("method", METHODS)
def test_a_refused_peer_gets_the_unknown_path_404_for_every_method_on_ws(tmp_path, method):
    w = wiring(tmp_path)
    w.allow_peer = False
    spy = SpyRegistry(device=w.device)
    asked: list[str] = []

    async def gate() -> bool:
        asked.append("gate")
        return True

    async def main() -> tuple[bytes, bytes]:
        async with serving(app_for(w, registry=spy, gate_ok_async=gate)) as port:
            return await raw(port, request(method, "/ws")), await raw(port, request(method, "/nope"))

    on_ws, unknown = asyncio.run(asyncio.wait_for(main(), 20))
    assert status_line_and_body(on_ws) == status_line_and_body(unknown)
    assert status_line_and_body(on_ws)[0].startswith(b"HTTP/1.1 404 ")
    assert asked == [] and spy.calls == [] and w.frames == []


@pytest.mark.parametrize("method", METHODS)
def test_an_allowed_peer_gets_405_for_any_method_but_get_and_nothing_is_read(tmp_path, method):
    w = wiring(tmp_path)
    spy = SpyRegistry(device=w.device)

    async def main() -> bytes:
        async with serving(app_for(w, registry=spy)) as port:
            return await raw(port, request(method, "/ws"))

    reply = asyncio.run(asyncio.wait_for(main(), 20))
    assert reply.startswith(b"HTTP/1.1 405 ") and b"Allow: GET" in reply
    assert spy.calls == [] and w.frames == []
