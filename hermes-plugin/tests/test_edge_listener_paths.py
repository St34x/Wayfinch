# SPDX-License-Identifier: Apache-2.0
"""The edge listener's handling of the socket path: a live socket of a second instance is never taken
over (probe first, ``ECONNREFUSED`` alone means stale); a permission or read-only error on the path is the drop-in
hint, not a bare ``PermissionError``; a failure after ``bind`` closes the socket and leaves no file behind."""

from __future__ import annotations

import asyncio
import errno
import os
import socket
from pathlib import Path

import pytest
from edge_helpers import short_dir

from glasses import edge_listener
from glasses.edge_listener import DROP_IN, EdgeListener, EdgeSocketError

from test_edge_listener import _snapshot, _stale_socket


def _start(path: Path) -> None:
    async def run() -> None:
        listener = EdgeListener(str(path), os.geteuid())
        await listener.start(asyncio.Protocol)
        await listener.stop()

    asyncio.run(run())


def test_a_live_socket_of_a_second_instance_is_refused_and_left_serving() -> None:
    with short_dir() as d:
        path = d / "edge.sock"

        async def run() -> None:
            first = EdgeListener(str(path), os.geteuid())
            await first.start(asyncio.Protocol)
            before = _snapshot(path)
            with pytest.raises(EdgeSocketError, match="in use by another process; left untouched"):
                await EdgeListener(str(path), os.geteuid()).start(asyncio.Protocol)
            assert _snapshot(path) == before  # the same inode: nothing was unlinked
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(path))  # and the first instance still listens there
            await first.stop()
            assert not path.exists()

        asyncio.run(run())


def test_a_probe_that_cannot_tell_leaves_the_socket_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live listener with a full backlog makes ``connect`` time out: that is not ``ECONNREFUSED``, so not stale."""
    with short_dir() as d:
        path = d / "edge.sock"
        _stale_socket(path)
        before = _snapshot(path)

        def timed_out(self: socket.socket, _address: object) -> None:
            raise TimeoutError

        monkeypatch.setattr(edge_listener.socket.socket, "connect", timed_out)
        with pytest.raises(EdgeSocketError, match="left untouched"):
            _start(path)
        monkeypatch.undo()
        assert _snapshot(path) == before


def test_a_directory_without_search_permission_names_the_drop_in(monkeypatch: pytest.MonkeyPatch) -> None:
    real_lstat = os.lstat

    def denied(p: object, *, dir_fd: int | None = None) -> os.stat_result:
        if dir_fd is None and str(p).endswith("edge.sock"):  # not the cleanup's lstat relative to a directory fd
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return real_lstat(p, dir_fd=dir_fd)  # type: ignore[arg-type]

    with short_dir() as d:
        monkeypatch.setattr(edge_listener.os, "lstat", denied)
        with pytest.raises(EdgeSocketError) as raised:
            _start(d / "edge.sock")
    assert DROP_IN in str(raised.value) and "EACCES" in str(raised.value)


def test_a_stale_socket_that_cannot_be_unlinked_names_the_drop_in_and_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    with short_dir() as d:
        path = d / "edge.sock"
        _stale_socket(path)
        before = _snapshot(path)

        def read_only(_p: object) -> None:
            raise OSError(errno.EROFS, os.strerror(errno.EROFS))

        monkeypatch.setattr(edge_listener.os, "unlink", read_only)
        with pytest.raises(EdgeSocketError) as raised:
            _start(path)
        monkeypatch.undo()
        assert DROP_IN in str(raised.value) and "EROFS" in str(raised.value)
        assert _snapshot(path) == before


@pytest.mark.parametrize("step", ["chmod", "listen"])
def test_a_failure_after_bind_closes_the_socket_and_removes_the_file(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    made: list[socket.socket] = []
    real_bind = socket.socket.bind

    def bind(self: socket.socket, address: object) -> None:
        made.append(self)
        real_bind(self, address)  # type: ignore[arg-type]

    def fail(*_args: object) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(edge_listener.socket.socket, "bind", bind)
    monkeypatch.setattr(edge_listener.os if step == "chmod" else edge_listener.socket.socket, step, fail)
    with short_dir() as d:
        with pytest.raises(EdgeSocketError, match="cannot listen"):
            _start(d / "edge.sock")
        monkeypatch.undo()
        assert not (d / "edge.sock").exists() and [s.fileno() for s in made] == [-1]


def test_a_path_that_vanishes_after_bind_closes_the_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[socket.socket] = []
    real_bind, real_lstat = socket.socket.bind, os.lstat

    def bind(self: socket.socket, address: object) -> None:
        made.append(self)
        real_bind(self, address)  # type: ignore[arg-type]

    def vanished(p: object, *, dir_fd: int | None = None) -> os.stat_result:
        found = real_lstat(p, dir_fd=dir_fd)  # type: ignore[arg-type]  # absent before the bind, present after it
        if dir_fd is None and str(p).endswith("edge.sock"):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))
        return found

    monkeypatch.setattr(edge_listener.socket.socket, "bind", bind)
    monkeypatch.setattr(edge_listener.os, "lstat", vanished)
    with short_dir() as d, pytest.raises(EdgeSocketError):
        _start(d / "edge.sock")
    monkeypatch.undo()
    assert [s.fileno() for s in made] == [-1]
