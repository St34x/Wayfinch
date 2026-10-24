# SPDX-License-Identifier: Apache-2.0
"""Edge cases of the edge listener's ``_probe``: a stale socket, a live listener with room and with a full
backlog, a dangling or symlinked path, a regular file, and a path the user may not connect to. Each refusal has its
exact operator-facing text, the path is left alone, and the event loop is never held for long. Real unix sockets."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from edge_helpers import short_dir

from glasses.edge_listener import EdgeSocketError, bind_socket

IN_USE = "edge.socket: in use by another process; left untouched"
CANNOT_TELL = "edge.socket: cannot tell whether another process uses it; left untouched"
NOT_STALE = "edge.socket: the path exists and is not a stale socket of this user; left untouched"
HELD_S = 0.2


@contextmanager
def listener(path: Path, backlog: int) -> Iterator[socket.socket]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        sock.listen(backlog)
        yield sock
    finally:
        sock.close()


def refused_on_the_loop(path: Path, text: str) -> float:
    """Run ``bind_socket(path)`` on the loop thread beside a ticker; assert the exact refusal; return the hold time."""
    held: list[float] = []

    async def main() -> None:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.ensure_future(ticker())
        await asyncio.sleep(0.03)
        before = ticks
        started = time.monotonic()
        with pytest.raises(EdgeSocketError) as raised:
            bind_socket(str(path))
        held.append(time.monotonic() - started)
        assert str(raised.value) == text
        await asyncio.sleep(0.03)
        assert ticks > before
        task.cancel()

    asyncio.run(main())
    return held[0]


def test_a_full_backlog_gives_the_exact_cannot_tell_text_in_microseconds() -> None:
    with short_dir() as d, listener(d / "e.sock", 0):
        fillers: list[socket.socket] = []
        for _ in range(8):
            filler = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            fillers.append(filler)
            filler.setblocking(False)
            try:
                filler.connect(str(d / "e.sock"))
            except BlockingIOError:
                break
        else:
            pytest.fail("backlog did not fill")
        times = [refused_on_the_loop(d / "e.sock", CANNOT_TELL) for _ in range(20)]
        assert max(times) < HELD_S
        assert stat.S_ISSOCK(os.lstat(d / "e.sock").st_mode)
        for filler in fillers:
            filler.close()


def test_a_live_listener_with_room_is_in_use() -> None:
    with short_dir() as d, listener(d / "e.sock", 5):
        took = refused_on_the_loop(d / "e.sock", IN_USE)
        assert took < HELD_S
        assert stat.S_ISSOCK(os.lstat(d / "e.sock").st_mode)


def test_a_stale_socket_is_replaced_and_the_new_socket_listens() -> None:
    with short_dir() as d:
        old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old.bind(str(d / "e.sock"))
        old.close()  # the file stays, nobody listens
        sock, ident = bind_socket(str(d / "e.sock"))
        try:
            assert ident is not None and stat.S_ISSOCK(os.lstat(d / "e.sock").st_mode)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(str(d / "e.sock"))  # the replacement listens
            client.close()
        finally:
            sock.close()


def test_a_stale_socket_clears_on_the_loop_in_microseconds() -> None:
    with short_dir() as d:
        old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old.bind(str(d / "e.sock"))
        old.close()
        started = time.monotonic()
        sock, _ = bind_socket(str(d / "e.sock"))
        took = time.monotonic() - started
        sock.close()
        assert took < HELD_S


def test_a_dangling_symlink_is_not_a_stale_socket() -> None:
    with short_dir() as d:
        (d / "e.sock").symlink_to(d / "nowhere")
        assert refused_on_the_loop(d / "e.sock", NOT_STALE) < HELD_S
        assert (d / "e.sock").is_symlink() and not (d / "nowhere").exists()


def test_a_symlink_to_a_live_socket_is_not_followed_or_removed() -> None:
    with short_dir() as d, listener(d / "real.sock", 5):
        (d / "e.sock").symlink_to(d / "real.sock")
        assert refused_on_the_loop(d / "e.sock", NOT_STALE) < HELD_S
        assert (d / "e.sock").is_symlink() and stat.S_ISSOCK(os.lstat(d / "real.sock").st_mode)


def test_a_symlink_to_a_full_backlog_listener_is_not_probed() -> None:
    with short_dir() as d, listener(d / "real.sock", 0):
        fillers = []
        for _ in range(8):
            filler = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            fillers.append(filler)
            filler.setblocking(False)
            try:
                filler.connect(str(d / "real.sock"))
            except BlockingIOError:
                break
        (d / "e.sock").symlink_to(d / "real.sock")
        assert refused_on_the_loop(d / "e.sock", NOT_STALE) < HELD_S
        for filler in fillers:
            filler.close()


def test_a_regular_file_is_not_a_stale_socket_and_keeps_its_content() -> None:
    with short_dir() as d:
        (d / "e.sock").write_text("precious")
        assert refused_on_the_loop(d / "e.sock", NOT_STALE) < HELD_S
        assert (d / "e.sock").read_text() == "precious"


def test_a_socket_the_user_may_not_connect_to_is_cannot_tell_and_stays() -> None:
    with short_dir() as d, listener(d / "e.sock", 5):
        os.chmod(d / "e.sock", 0o000)  # connect needs write permission: EACCES
        try:
            assert refused_on_the_loop(d / "e.sock", CANNOT_TELL) < HELD_S
        finally:
            os.chmod(d / "e.sock", 0o600)
        assert stat.S_ISSOCK(os.lstat(d / "e.sock").st_mode)


def test_a_stale_socket_with_no_permission_is_cannot_tell_and_stays() -> None:
    with short_dir() as d:
        old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old.bind(str(d / "e.sock"))
        old.close()
        os.chmod(d / "e.sock", 0o000)
        try:
            assert refused_on_the_loop(d / "e.sock", CANNOT_TELL) < HELD_S
        finally:
            os.chmod(d / "e.sock", 0o600)
        assert (d / "e.sock").exists()


def test_a_directory_without_search_permission_gives_the_drop_in_hint_and_does_not_block() -> None:
    with short_dir() as d:
        sub = d / "sub"
        sub.mkdir()
        old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old.bind(str(sub / "e.sock"))
        old.close()
        os.chmod(sub, 0o000)
        try:
            started = time.monotonic()
            with pytest.raises(EdgeSocketError, match=r"cannot create the socket \(EACCES\)"):
                bind_socket(str(sub / "e.sock"))
            assert time.monotonic() - started < HELD_S
        finally:
            os.chmod(sub, 0o700)
        assert (sub / "e.sock").exists()
