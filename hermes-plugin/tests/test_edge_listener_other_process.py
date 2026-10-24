# SPDX-License-Identifier: Apache-2.0
"""The edge socket path against real second processes and odd entries at the path: a live listener of another
process is never taken over, a socket whose owner was killed is stale and replaced, a symlink (to a live socket, to a
stale one, dangling) is never followed or unlinked, a listener with a full backlog is "cannot tell" (left alone), and
two listeners started in one loop end with exactly one winner."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest
from edge_helpers import short_dir

from glasses.edge_listener import EdgeListener, EdgeSocketError

from test_edge_listener import _snapshot, _stale_socket

OWNER = """
import socket, sys, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sys.argv[1])
s.listen(int(sys.argv[2]))
print("ready", flush=True)
time.sleep(120)
"""


@contextmanager
def other_process_listening(script: Path, path: Path, backlog: int = 8) -> Iterator[subprocess.Popen[str]]:
    script.write_text(OWNER, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script), str(path), str(backlog)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        yield proc
    finally:
        proc.kill()
        proc.wait(10)
        if proc.stdout is not None:
            proc.stdout.close()


def _start_and_stop(path: Path) -> None:
    async def run() -> None:
        listener = EdgeListener(str(path), os.geteuid())
        await listener.start(asyncio.Protocol)
        await listener.stop()

    asyncio.run(run())


def test_a_listener_of_another_process_is_refused_and_keeps_serving(tmp_path: Path) -> None:
    with short_dir() as d:
        path = d / "edge.sock"
        with other_process_listening(tmp_path / "owner.py", path) as owner:
            before = _snapshot(path)
            with pytest.raises(EdgeSocketError, match="in use by another process; left untouched"):
                _start_and_stop(path)
            assert _snapshot(path) == before and owner.poll() is None
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(path))  # still the other process's inode, still accepting


def test_a_socket_whose_owner_was_killed_is_stale_and_replaced(tmp_path: Path) -> None:
    with short_dir() as d:
        path = d / "edge.sock"
        with other_process_listening(tmp_path / "owner.py", path) as owner:
            owner.send_signal(signal.SIGKILL)  # no cleanup: the file stays behind
            owner.wait(10)
            assert path.exists()

            async def run() -> None:
                listener = EdgeListener(str(path), os.geteuid())
                await listener.start(asyncio.Protocol)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.connect(str(path))  # the new listener answers
                await listener.stop()

            asyncio.run(run())
            assert not path.exists()


@pytest.mark.parametrize("target", ["live", "stale", "dangling"])
def test_a_symlink_at_the_path_is_never_followed_nor_unlinked(tmp_path: Path, target: str) -> None:
    with short_dir() as d, ExitStack() as stack:
        path, real = d / "edge.sock", d / "real.sock"
        if target == "live":
            stack.enter_context(other_process_listening(tmp_path / "owner.py", real))
        elif target == "stale":
            _stale_socket(real)
        path.symlink_to(real)
        before_link, before_target = _snapshot(path), _snapshot(real) if real.exists() else None
        with pytest.raises(EdgeSocketError, match="left untouched"):
            _start_and_stop(path)
        assert _snapshot(path) == before_link and os.readlink(path) == str(real)
        assert (_snapshot(real) if real.exists() else None) == before_target


def test_a_listener_with_a_full_backlog_is_not_taken_for_stale(tmp_path: Path) -> None:
    """Backlog 0 plus connections nobody accepts: the probe's connect neither succeeds nor is refused."""
    with short_dir() as d, ExitStack() as stack:
        path = d / "edge.sock"
        owner = stack.enter_context(other_process_listening(tmp_path / "owner.py", path, backlog=0))
        for _ in range(4):  # beyond the backlog: the later connects are not accepted by the kernel
            filler = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            filler.settimeout(0.3)
            with contextlib.suppress(OSError):
                filler.connect(str(path))
        before = _snapshot(path)
        with pytest.raises(EdgeSocketError, match="left untouched"):
            _start_and_stop(path)
        assert _snapshot(path) == before and owner.poll() is None


def test_two_listeners_started_in_one_loop_leave_exactly_one_winner() -> None:
    with short_dir() as d:
        path = d / "edge.sock"
        _stale_socket(path)

        async def run() -> list[object]:
            first, second = EdgeListener(str(path), os.geteuid()), EdgeListener(str(path), os.geteuid())
            results = await asyncio.gather(
                first.start(asyncio.Protocol), second.start(asyncio.Protocol), return_exceptions=True
            )
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(path))  # whoever won is still serving
            await first.stop()
            await second.stop()
            return list(results)

        results = asyncio.run(run())
        errors = [r for r in results if isinstance(r, EdgeSocketError)]
        assert len(errors) == 1 and "in use by another process" in str(errors[0]) and results.count(None) == 1
        assert not path.exists()  # the loser's stop() removed nothing; the winner's stop removed its own file
