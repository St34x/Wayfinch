# SPDX-License-Identifier: Apache-2.0
"""The edge listener (``glasses.edge_listener``): what it does with the path before ``bind()`` (SAT-SC-15), the
socket mode, the unlink on stop, the drop-in error of an unwritable directory, no TCP listener at all (SAT-SC-11 (2)),
and the peer check of each accepted connection before a byte is read (SAT-SC-11 (1), unreadable credentials)."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import socket
import stat
import types
from asyncio import trsock
from pathlib import Path
from typing import cast

import pytest
from edge_helpers import SpyRegistry, edge_serving, listening_tcp_inodes, raw_unix, request_bytes, short_dir
from server_helpers import app_for, wiring

from glasses import edge_listener
from glasses.edge_listener import DROP_IN, EdgeListener, EdgeSocketError

OLD_LOOPBACK_PORT = 8765  # the default of the retired `port` key


def _stale_socket(path: Path) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()  # the file stays: a stale socket of this user


def _snapshot(path: Path) -> tuple[object, ...]:
    found = os.lstat(path)
    content = os.readlink(path) if stat.S_ISLNK(found.st_mode) else None
    if stat.S_ISREG(found.st_mode):
        content = path.read_bytes()
    return found.st_ino, found.st_mode, found.st_mtime_ns, content


def _start(path: Path) -> None:
    async def run() -> None:
        listener = EdgeListener(str(path), os.geteuid())
        await listener.start(asyncio.Protocol)
        await listener.stop()

    asyncio.run(run())


def test_a_stale_socket_of_this_user_is_replaced_and_the_socket_is_0660_then_gone_after_stop() -> None:
    with short_dir() as d:
        path = d / "edge.sock"
        _stale_socket(path)

        async def run() -> None:
            listener = EdgeListener(str(path), os.geteuid())
            await listener.start(asyncio.Protocol)
            found = os.lstat(path)
            assert stat.S_ISSOCK(found.st_mode) and stat.S_IMODE(found.st_mode) == 0o660
            await listener.stop()
            assert not path.exists()

        asyncio.run(run())


@pytest.mark.parametrize("kind", ["file", "symlink", "symlink_to_socket", "directory"])
def test_anything_but_a_stale_socket_is_left_untouched_and_refused(kind: str) -> None:
    with short_dir() as d:
        path, target = d / "edge.sock", d / "target"
        if kind == "file":
            path.write_bytes(b"keep me")
        elif kind == "symlink":
            path.symlink_to(d / "nowhere")
        elif kind == "symlink_to_socket":
            _stale_socket(target)  # a socket of this user behind the link: stat() would see it, lstat() does not
            path.symlink_to(target)
        else:
            path.mkdir()
        before = _snapshot(path)
        with pytest.raises(EdgeSocketError, match="left untouched"):
            _start(path)
        assert _snapshot(path) == before
        if kind == "symlink_to_socket":
            assert stat.S_ISSOCK(os.lstat(target).st_mode)


def test_a_socket_of_another_uid_is_left_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    with short_dir() as d:
        path = d / "edge.sock"
        _stale_socket(path)
        before = _snapshot(path)
        real_lstat = os.lstat

        def foreign(p: object) -> os.stat_result:
            found = real_lstat(p)  # type: ignore[arg-type]
            values = list(found)
            values[stat.ST_UID] = os.geteuid() + 1
            return os.stat_result(values)

        monkeypatch.setattr(edge_listener.os, "lstat", foreign)
        with pytest.raises(EdgeSocketError, match="left untouched"):
            _start(path)
        monkeypatch.undo()
        assert _snapshot(path) == before


@pytest.mark.parametrize("code", [errno.EROFS, errno.ENOENT])
def test_an_unwritable_directory_names_the_drop_in_and_opens_no_tcp_socket(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def refuse(self: socket.socket, _address: object) -> None:
        raise OSError(code, os.strerror(code))

    before = listening_tcp_inodes()
    with short_dir() as d:
        monkeypatch.setattr(edge_listener.socket.socket, "bind", refuse)
        with pytest.raises(EdgeSocketError) as raised:
            _start(d / "edge.sock")
        monkeypatch.undo()
        assert DROP_IN in str(raised.value) and errno.errorcode[code] in str(raised.value)
        assert not (d / "edge.sock").exists()
    assert listening_tcp_inodes() <= before


def test_a_missing_directory_is_not_created() -> None:
    with short_dir() as d, pytest.raises(EdgeSocketError, match="ReadWritePaths"):
        _start(d / "missing" / "edge.sock")
    assert not (d / "missing").exists()


def test_an_empty_path_is_refused_before_any_socket_is_made(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bind("")`` would autobind an abstract socket that nothing can reach: the bind itself must fail closed."""

    def no_socket(*_args: object, **_kwargs: object) -> socket.socket:
        raise AssertionError("a socket was created for an empty path")

    monkeypatch.setattr(edge_listener, "socket", types.SimpleNamespace(AF_UNIX=1, SOCK_STREAM=1, socket=no_socket))
    with pytest.raises(EdgeSocketError, match="empty"):
        edge_listener.bind_socket("")
    with pytest.raises(EdgeSocketError, match="empty"):
        asyncio.run(EdgeListener("", os.geteuid()).start(asyncio.Protocol))


def test_stop_does_not_unlink_a_path_that_is_no_longer_its_socket() -> None:
    with short_dir() as d:
        path = d / "edge.sock"

        async def run() -> None:
            listener = EdgeListener(str(path), os.geteuid())
            await listener.start(asyncio.Protocol)
            path.unlink()
            path.write_bytes(b"someone else's")
            await listener.stop()

        asyncio.run(run())
        assert path.read_bytes() == b"someone else's"


def test_stop_does_not_unlink_another_socket_that_took_the_path_over() -> None:
    """A socket of this user at the path, but not the file this listener created (new inode): left in place."""
    with short_dir() as d:
        path, other = d / "edge.sock", d / "other.sock"

        async def run() -> None:
            listener = EdgeListener(str(path), os.geteuid())
            await listener.start(asyncio.Protocol)
            first = os.lstat(path)
            rival = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            rival.bind(str(other))  # bound while the first inode still exists: a different inode for certain
            rival.listen(1)
            os.rename(other, path)  # replaces the listener's file atomically
            taken = os.lstat(path)
            assert stat.S_ISSOCK(taken.st_mode) and (taken.st_dev, taken.st_ino) != (first.st_dev, first.st_ino)
            await listener.stop()
            kept = os.lstat(path)
            assert stat.S_ISSOCK(kept.st_mode) and (kept.st_dev, kept.st_ino) == (taken.st_dev, taken.st_ino)
            rival.close()

        asyncio.run(run())


def test_the_socket_file_is_already_0660_when_listen_is_called(monkeypatch: pytest.MonkeyPatch) -> None:
    """Between ``listen`` and the ``chmod`` the file would still carry the umask's mode: the mode comes first."""
    modes: list[int] = []
    real_listen = socket.socket.listen

    def spy(self: socket.socket, *args: int) -> None:
        modes.append(stat.S_IMODE(os.lstat(self.getsockname()).st_mode))
        real_listen(self, *args)

    with short_dir() as d:
        old_umask = os.umask(0o002)  # a umask that would leave the freshly bound file at 0664
        try:
            monkeypatch.setattr(edge_listener.socket.socket, "listen", spy)
            _start(d / "edge.sock")
        finally:
            os.umask(old_umask)
    assert modes[0] == 0o660  # the first call is bind_socket's own (asyncio calls listen again on the same socket)


def test_a_peer_of_another_uid_is_closed_before_a_byte_is_read_and_tcp_is_refused(tmp_path: Path) -> None:
    w = wiring(tmp_path)
    spy = SpyRegistry(w.registry)
    gates: list[int] = []

    async def gate() -> bool:
        gates.append(1)
        return True

    async def run() -> None:
        app = app_for(w, registry=spy, gate_ok_async=gate, is_revoked=lambda _id: gates.append(2) or False)
        with short_dir() as d:
            async with edge_serving(app, d / "edge.sock", os.geteuid() + 1):
                headers = {**w.headers(), "Authorization": f"Bearer {w.token}"}
                for method, target in (("GET", "/ws"), ("GET", "/enroll"), ("PUT", "/enroll"), ("POST", "/enroll")):
                    assert await raw_unix(d / "edge.sock", request_bytes(method, target, headers)) == b""
                with pytest.raises(ConnectionRefusedError):
                    socket.create_connection(("127.0.0.1", OLD_LOOPBACK_PORT), timeout=1).close()
        assert (spy.lookups, gates, w.peers) == ([], [], [])

    asyncio.run(run())


def test_unreadable_peer_credentials_close_the_connection_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    w = wiring(tmp_path)
    spy = SpyRegistry(w.registry)

    def broken(*_args: object) -> bytes:
        raise OSError(errno.EBADF, "errtext-sentinel")

    monkeypatch.setattr(trsock.TransportSocket, "getsockopt", broken)

    async def run() -> None:
        with short_dir() as d:
            async with edge_serving(app_for(w, registry=spy), d / "edge.sock", os.geteuid()):
                for _ in range(50):
                    assert await raw_unix(d / "edge.sock", request_bytes("GET", "/ws", w.headers())) == b""

    with caplog.at_level(logging.DEBUG, logger="glasses.edge_listener"):
        asyncio.run(run())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "OSError" in warnings[0].getMessage()
    assert "errtext-sentinel" not in caplog.text
    assert (spy.lookups, w.peers) == ([], [])


class _Flooder(asyncio.Protocol):
    """The app's side of one connection: writes more than the transport buffer holds and records the flow control."""

    events: list[str] = []

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        cast(asyncio.Transport, transport).write(b"x" * (8 << 20))

    def pause_writing(self) -> None:
        self.events.append("pause")

    def resume_writing(self) -> None:
        self.events.append("resume")


def test_write_flow_control_of_a_peer_that_does_not_read_reaches_the_app_through_the_guard() -> None:
    """aiohttp's writes wait on ``pause_writing`` (the pong timeout needs it): the guard must forward both calls."""
    _Flooder.events = []

    async def run() -> None:
        with short_dir() as d:
            listener = EdgeListener(str(d / "edge.sock"), os.geteuid())
            await listener.start(_Flooder)
            reader, writer = await asyncio.open_unix_connection(str(d / "edge.sock"))
            for _ in range(100):  # the peer reads nothing yet: the buffer fills and the app is told to pause
                if _Flooder.events:
                    break
                await asyncio.sleep(0.02)
            assert _Flooder.events == ["pause"]
            while _Flooder.events != ["pause", "resume"]:  # reading drains the buffer: the app may write again
                assert await asyncio.wait_for(reader.read(1 << 20), 5)
            writer.close()
            await listener.stop()

    asyncio.run(run())
