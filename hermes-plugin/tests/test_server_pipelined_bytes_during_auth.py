# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.184b): bytes a client writes right behind its upgrade request, while ``authenticate``
still waits, must stay in the kernel. aiohttp appends them to a tail without a limit until ``ws.prepare`` installs the
WebSocket parser, and ``prepare`` then turns the whole tail into queued frames in one call (loop gap under
250 ms, resident set growth under 100 MiB). The transport is therefore unread from the start of the upgrade, and read
again on every exit that is not a WebSocket (a refused request is answered and its connection is usable or ended)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from server_auth_helpers import always, creds, upgrade_headers
from server_helpers import app_for, serving, wiring
from server_raw_ws import RawWs, frame_bytes
from server_stream_helpers import CHUNK, PING, Stream, run_stream

from glasses.connections import UpgradeCap
from glasses.server import _CAP

from test_server_frame_stream_gaps import FREE_GAP_LIMIT, GROWTH_LIMIT

# how long the gate waits: the client writes ~15 MiB meanwhile (about 420 MiB at 0.2 s before the pause)
AUTH_WAIT_S = 0.4
TINY = frame_bytes(PING, b"")  # a masked empty ping: 6 bytes
FLOOD = TINY * (4 * CHUNK)  # ~9.6 MB, more than the window moves
SECOND_REQUEST = b"GET /nothing HTTP/1.1\r\nHost: x\r\n\r\n"


class PipeliningWs(RawWs):
    """The upgrade request goes out first, the flood from a second thread right behind it: the answer to the request
    is read only after the whole stream is under way, as a client that pipelines would."""

    def __init__(self, port: int, headers: dict[str, str], pipelined: bytes) -> None:
        self.pipelined = pipelined
        self.sender = threading.Thread(target=self._pipeline, daemon=True)
        super().__init__(port, headers, timeout=20)

    def _pipeline(self) -> None:
        with contextlib.suppress(OSError):  # the server ended the connection in the middle of the stream
            self.sock.sendall(self.pipelined)

    def _read_head(self) -> bytes:
        self.sender.start()
        return super()._read_head()


class PipelinedUpgrade(Stream):
    def client(self, port: int, headers: dict[str, str], before: Callable[[RawWs], None] | None = None) -> None:
        self.holding = True  # the loop's gap counts from the request, not from start-up
        ws = PipeliningWs(port, headers, self.chunk)
        try:
            assert ws.upgrade_reply.startswith(b"HTTP/1.1 101"), ws.upgrade_reply
            self.close_code = ws.close_code()
            self.holding = False
        finally:
            ws.shut()
            ws.sender.join(20)


def test_bytes_pipelined_behind_an_upgrade_request_stay_unread_while_authenticate_waits(tmp_path):
    waited: list[float] = []

    async def slow_gate() -> bool:
        started = time.monotonic()
        await asyncio.sleep(AUTH_WAIT_S)
        waited.append(time.monotonic() - started)
        return True

    flood = PipelinedUpgrade(FLOOD, once=True)
    run_stream(tmp_path, flood, gate_ok_async=slow_gate)
    assert waited and waited[0] >= AUTH_WAIT_S * 0.9, "authenticate did not wait: the test saw nothing"
    assert flood.close_code == 1008
    assert flood.held_gap < FREE_GAP_LIMIT, f"loop gap {flood.held_gap * 1000:.0f} ms behind the upgrade request"
    assert flood.peak_growth < GROWTH_LIMIT, f"RSS grew {flood.peak_growth / 2**20:.0f} MiB behind the upgrade request"


class ReadSpy:
    """What the server's transports were told: per transport the ordered ``pause`` / ``resume`` calls."""

    def __init__(self) -> None:
        self.calls: dict[int, list[str]] = {}

    def install(self, patch: pytest.MonkeyPatch) -> None:
        cls = asyncio.selector_events._SelectorTransport  # type: ignore[attr-defined]
        for name in ("pause_reading", "resume_reading"):
            real = getattr(cls, name)

            def spy(transport: Any, *, _real: Callable[[Any], None] = real, _name: str = name) -> None:
                self.calls.setdefault(id(transport), []).append(_name.split("_")[0])
                _real(transport)

            patch.setattr(cls, name, spy)

    def left_paused(self) -> list[int]:
        """Transports whose last call was a pause."""
        return [key for key, calls in self.calls.items() if calls[-1] == "pause"]

    def paused_at_all(self) -> bool:
        return any("pause" in calls for calls in self.calls.values())


def _revoke(w: Any, _app: Any) -> None:
    w.revoked = True


def _cap_of_zero(_w: Any, app: Any) -> None:
    app[_CAP] = UpgradeCap(per_key=0)


REFUSALS = {  # name: (changes to the deps, token wrong, hook on (wiring, app), status)
    "bad_token": ({}, True, None, b"401"),
    "allow_all_gate": ({"gate_ok_async": always(False)}, False, None, b"503"),
    "revoked_device": ({}, False, _revoke, b"403"),
    "upgrade_cap": ({}, False, _cap_of_zero, b"503"),
}


@pytest.mark.parametrize("name", list(REFUSALS))
def test_every_refused_upgrade_leaves_the_transport_read_and_the_connection_usable(tmp_path, monkeypatch, name):
    changes, bad_token, hook, status = REFUSALS[name]
    spy = ReadSpy()
    spy.install(monkeypatch)
    w = wiring(tmp_path)
    headers = {**upgrade_headers(), **creds("x" * 43 if bad_token else w.token)}
    request = (
        "GET /ws HTTP/1.1\r\nHost: x\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    ).encode()

    async def main() -> tuple[bytes, bytes]:
        app = app_for(w, **changes)
        if hook is not None:
            hook(w, app)
        async with serving(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(request)
            first = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            await reader.readexactly(int(first.split(b"Content-Length: ")[1].split(b"\r\n")[0]))
            writer.write(SECOND_REQUEST)  # the connection is read again: this is answered, not left unread
            second = await asyncio.wait_for(reader.read(200), 5)
            writer.close()
            return first, second

    first, second = asyncio.run(asyncio.wait_for(main(), 30))
    assert first.startswith(b"HTTP/1.1 " + status), first
    assert second.startswith(b"HTTP/1.1 "), "the refused connection was never read again"
    assert spy.paused_at_all(), "the upgrade did not pause the transport: the test saw nothing"
    assert spy.left_paused() == []


def test_a_refused_upgrade_with_a_flood_behind_it_ends_the_connection_and_frees_the_slot(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    headers = {**upgrade_headers(), **creds("x" * 43)}
    request = (
        "GET /ws HTTP/1.1\r\nHost: x\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    ).encode()

    async def main() -> tuple[bytes, dict[str, int]]:
        app = app_for(w)
        async with serving(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(request + TINY * 200_000)
            try:
                replies = await asyncio.wait_for(reader.read(), 10)  # to the end of the connection: no hang
            except ConnectionResetError:
                replies = b""  # the server closed with the unread tail still in its buffer
            writer.close()
            return replies, dict(app[_CAP]._open)

    replies, slots = asyncio.run(asyncio.wait_for(main(), 30))
    # aiohttp may drop the answer: the tail is refused
    assert replies == b"" or replies.startswith(b"HTTP/1.1 4"), replies[:80]
    assert slots == {}  # the upgrade slot of the certificate is free again
    assert "Traceback" not in caplog.text  # every logger, tracebacks included
