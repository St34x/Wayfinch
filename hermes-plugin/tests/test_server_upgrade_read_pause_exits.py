# SPDX-License-Identifier: Apache-2.0
"""WS server upgrade (T1.184b): the transport is unread while ``authenticate`` waits, and every way out of the upgrade
gives the read back or leaves the connection to its closer: a refusal of any kind, a hang-up, a cancellation, an
unexpected error. The slot of the per-certificate upgrade cap is free on every one of those exits, and the WebSocket
path still works with bytes pipelined right behind the request."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from server_auth_helpers import FP, creds, upgrade_headers
from server_helpers import Wiring, app_for, serving, wiring
from server_raw_ws import TEXT, RawWs, frame_bytes, hello_bytes

from glasses import server
from glasses.connections import TooManyUpgrades, UpgradeCap

PLAIN_HEADERS = {"Host": "x"}


class FakeTransport:
    """The transport calls ``_upgrade`` makes; ``closing`` is what ``is_closing`` answers."""

    def __init__(self, closing: bool = False) -> None:
        self.calls: list[str] = []
        self.closing = closing

    def pause_reading(self) -> None:
        self.calls.append("pause")

    def resume_reading(self) -> None:
        self.calls.append("resume")

    def is_closing(self) -> bool:
        return self.closing


def _request(transport: FakeTransport | None, cap: UpgradeCap) -> Any:
    return SimpleNamespace(app={server._CAP: cap, server._AUTH: object()}, transport=transport)


def _failing(exc: BaseException) -> Callable[..., Any]:
    async def authenticate(*_args: Any) -> Any:
        raise exc

    return authenticate


EXITS = {
    "refusal": web.HTTPUnauthorized(),
    "hang_up": ConnectionResetError(),
    "unexpected_error": RuntimeError("boom"),
}


@pytest.mark.parametrize("name", list(EXITS))
def test_every_failed_authenticate_gives_the_read_back_and_frees_the_cap_slot(monkeypatch, name):
    cap, transport = UpgradeCap(), FakeTransport()
    monkeypatch.setattr(server, "authenticate", _failing(EXITS[name]))
    with pytest.raises(type(EXITS[name])):
        asyncio.run(server._upgrade(_request(transport, cap), FP))
    assert transport.calls == ["pause", "resume"]
    assert cap._open == {}


def test_a_cancelled_authenticate_gives_the_read_back_and_frees_the_cap_slot(monkeypatch):
    cap, transport = UpgradeCap(), FakeTransport()

    async def main() -> None:
        started = asyncio.Event()

        async def hang(*_args: Any) -> Any:
            started.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(server, "authenticate", hang)
        task = asyncio.create_task(server._upgrade(_request(transport, cap), FP))
        await started.wait()
        assert cap._open == {FP: 1}, "the slot is held while authenticate waits"
        assert transport.calls == ["pause"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert transport.calls == ["pause", "resume"]
    assert cap._open == {}


def test_a_transport_that_is_closing_is_not_resumed_after_a_hang_up(monkeypatch):
    transport = FakeTransport(closing=True)
    monkeypatch.setattr(server, "authenticate", _failing(ConnectionResetError()))
    with pytest.raises(ConnectionResetError):
        asyncio.run(server._upgrade(_request(transport, UpgradeCap()), FP))
    assert transport.calls == ["pause"]


def test_a_request_without_a_transport_is_refused_as_before(monkeypatch):
    cap = UpgradeCap()
    monkeypatch.setattr(server, "authenticate", _failing(web.HTTPForbidden()))
    with pytest.raises(web.HTTPForbidden):
        asyncio.run(server._upgrade(_request(None, cap), FP))
    assert cap._open == {}


def test_the_cap_refusal_is_raised_before_authenticate_and_the_read_is_given_back(monkeypatch):
    cap, transport, asked = UpgradeCap(per_key=0), FakeTransport(), []

    async def authenticate(*args: Any) -> Any:
        asked.append(args)

    monkeypatch.setattr(server, "authenticate", authenticate)
    with pytest.raises(TooManyUpgrades):
        asyncio.run(server._upgrade(_request(transport, cap), FP))
    assert asked == []
    assert transport.calls[-1] == "resume"
    assert cap._open == {}


class ReadSpy:
    """Per transport the ordered ``pause`` / ``resume`` calls of the real selector transports."""

    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.calls: dict[int, list[str]] = {}
        cls = asyncio.selector_events._SelectorTransport  # type: ignore[attr-defined]
        for name in ("pause_reading", "resume_reading"):
            patch.setattr(cls, name, self._wrap(name, getattr(cls, name)))

    def _wrap(self, name: str, real: Callable[[Any], None]) -> Callable[[Any], None]:
        def spy(transport: Any) -> None:
            self.calls.setdefault(id(transport), []).append(name.split("_")[0])
            real(transport)

        return spy

    def left_paused(self) -> int:
        return sum(1 for calls in self.calls.values() if calls[-1] == "pause")


def _request_bytes(headers: dict[str, str], path: str = "/ws") -> bytes:
    lines = "".join(f"{k}: {v}\r\n" for k, v in {"Host": "x", **headers}.items())
    return f"GET {path} HTTP/1.1\r\n{lines}\r\n".encode()


async def _answer(reader: asyncio.StreamReader) -> bytes:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    length = int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0]) if b"Content-Length: " in head else 0
    await reader.readexactly(length)
    return head


async def _two_requests(app: web.Application, first: bytes) -> tuple[bytes, bytes]:
    """The first request, then a plain one on the same connection: the second is answered only if reads went on."""
    async with serving(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(first)
            head = await _answer(reader)
            writer.write(_request_bytes(PLAIN_HEADERS, "/nothing"))
            return head, await _answer(reader)
        finally:
            writer.close()


def test_a_request_that_is_no_upgrade_is_refused_and_its_connection_is_read_again(tmp_path, monkeypatch):
    spy, w = ReadSpy(monkeypatch), wiring(tmp_path)
    app = app_for(w)
    plain_with_credentials = _request_bytes({"Host": "x", **creds(w.token)})
    head, second = asyncio.run(asyncio.wait_for(_two_requests(app, plain_with_credentials), 20))
    assert head.startswith(b"HTTP/1.1 4"), head
    assert second.startswith(b"HTTP/1.1 404")
    assert spy.left_paused() == 0
    assert app[server._CAP]._open == {}


def test_an_unexpected_error_in_authenticate_is_a_500_that_ends_the_connection_with_the_read_given_back(
    tmp_path, monkeypatch, caplog
):
    spy, w = ReadSpy(monkeypatch), wiring(tmp_path)
    monkeypatch.setattr(server, "authenticate", _failing(RuntimeError("boom")))
    app = app_for(w)

    async def main() -> tuple[bytes, bytes]:
        async with serving(app) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(_request_bytes({**upgrade_headers(), **creds(w.token)}))
            head = await _answer(reader)
            rest = await asyncio.wait_for(reader.read(), 5)  # aiohttp ends the connection after an unhandled error
            writer.close()
            return head, rest

    with caplog.at_level(logging.CRITICAL):
        head, rest = asyncio.run(asyncio.wait_for(main(), 20))
    assert head.startswith(b"HTTP/1.1 500"), head
    assert rest == b""
    assert spy.left_paused() == 0
    assert app[server._CAP]._open == {}


def test_a_client_that_hangs_up_while_authenticate_waits_leaves_no_slot_no_pause_no_error(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    spy, w = ReadSpy(monkeypatch), wiring(tmp_path)

    async def main() -> dict[str, int]:
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate() -> bool:
            entered.set()
            await release.wait()
            return True

        app = app_for(w, gate_ok_async=gate)
        async with serving(app) as port:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(_request_bytes(w.headers()))
            await asyncio.wait_for(entered.wait(), 5)
            assert app[server._CAP]._open, "the slot is held while authenticate waits"
            writer.transport.abort()  # the hang-up, with the gate still waiting
            await asyncio.sleep(0.1)
            release.set()
            await asyncio.sleep(0.3)
            return dict(app[server._CAP]._open)

    assert asyncio.run(asyncio.wait_for(main(), 30)) == {}
    assert spy.left_paused() == 0
    noisy = [
        r for r in caplog.records if r.levelno >= logging.WARNING and r.name.split(".")[0] in {"glasses", "aiohttp"}
    ]
    assert noisy == [], [(r.name, r.getMessage()) for r in noisy]


def test_slots_of_hung_upgrades_are_held_until_they_end_and_the_cap_refusal_is_read_again(tmp_path, monkeypatch):
    spy, w = ReadSpy(monkeypatch), wiring(tmp_path)

    async def main() -> tuple[bytes, bytes, dict[str, int]]:
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate() -> bool:
            entered.set()
            await release.wait()
            return True

        app = app_for(w, gate_ok_async=gate)
        app[server._CAP] = UpgradeCap(per_key=1)
        async with serving(app) as port:
            _held_reader, held = await asyncio.open_connection("127.0.0.1", port)
            held.write(_request_bytes(w.headers()))
            await asyncio.wait_for(entered.wait(), 5)
            refused, second = await _two_requests_on(port, _request_bytes(w.headers()))
            release.set()
            held.close()
            await asyncio.sleep(0.3)
            return refused, second, dict(app[server._CAP]._open)

    refused, second, slots = asyncio.run(asyncio.wait_for(main(), 30))
    assert refused.startswith(b"HTTP/1.1 503"), refused
    assert second.startswith(b"HTTP/1.1 404"), "the connection refused at the cap was never read again"
    assert slots == {}
    assert spy.left_paused() == 0


async def _two_requests_on(port: int, first: bytes) -> tuple[bytes, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(first)
        head = await _answer(reader)
        writer.write(_request_bytes(PLAIN_HEADERS, "/nothing"))
        return head, await _answer(reader)
    finally:
        writer.close()


class PipeliningHello(RawWs):
    """The upgrade request and a ``hello`` frame in one write: the frame is already in the server's first read."""

    def __init__(self, port: int, headers: dict[str, str], tail: bytes) -> None:
        self.tail = tail
        super().__init__(port, headers, timeout=10)

    def _read_head(self) -> bytes:
        self.sock.sendall(self.tail)
        return super()._read_head()


def _hello_behind_the_request(port: int, w: Wiring) -> dict[str, Any]:
    ws = PipeliningHello(port, w.headers(), frame_bytes(TEXT, hello_bytes()))
    try:
        got = ws.recv()
        assert got is not None and got[0] == TEXT, got
        return json.loads(got[1])
    finally:
        ws.shut()


def test_a_hello_pipelined_behind_the_upgrade_request_is_welcomed_and_the_transport_ends_unpaused(
    tmp_path, monkeypatch
):
    spy, w = ReadSpy(monkeypatch), wiring(tmp_path)

    async def main() -> dict[str, Any]:
        async with serving(app_for(w)) as port:
            frame = await asyncio.get_running_loop().run_in_executor(None, _hello_behind_the_request, port, w)
            await asyncio.sleep(0.2)
            return frame

    frame = asyncio.run(asyncio.wait_for(main(), 30))
    assert frame["type"] == "welcome" and frame["body"]["deviceId"] == w.device.device_id
    assert any("pause" in calls for calls in spy.calls.values()), "no pause seen: the test saw nothing"
    assert spy.left_paused() == 0
