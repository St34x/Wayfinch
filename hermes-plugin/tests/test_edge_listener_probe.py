# SPDX-License-Identifier: Apache-2.0
"""The edge listener's probe of an existing socket must not hold the event loop: a live listener whose backlog is
full does not take another connection, and a blocking connect would wait for room in it. The probe answers at once
(``EAGAIN``, not a timeout) and leaves the path alone. These tests guard against a probe that blocks, and against one
that merely waits a while before answering: the call must take well under a second. A real unix socket with
``listen(0)`` and a filled backlog; nothing of the product is mocked."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

import pytest
from edge_helpers import short_dir

from glasses.edge_listener import EdgeSocketError, bind_socket

CANNOT_TELL = "cannot tell whether another process uses it; left untouched"
FAST_S = 0.2  # the probe needs microseconds; a probe that waits for the listener, even briefly, is too slow
JOIN_S = 2.0  # how long the test itself waits for the probe thread; generous, a loaded host must not fail it


class LoopBlocked(Exception):  # not an OSError: the probe turns every OSError into its own refusal
    """Raised by the alarm when the probe has held the event loop for too long."""


@contextmanager
def full_backlog(path: Path) -> Iterator[socket.socket]:
    """A live listener at *path* that never accepts, with its backlog full (a further connect gets ``EAGAIN``)."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    fillers: list[socket.socket] = []
    try:
        listener.bind(str(path))
        listener.listen(0)
        for _ in range(8):
            filler = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            fillers.append(filler)
            filler.setblocking(False)
            try:
                filler.connect(str(path))
            except BlockingIOError:
                break
        else:
            pytest.fail("the backlog of the listener did not fill up")
        yield listener
    finally:
        listener.setblocking(False)
        for _ in fillers:  # make room, so that a connect that was left blocked returns and its thread ends
            with contextlib.suppress(OSError):
                listener.accept()
        for filler in fillers:
            filler.close()
        listener.close()


def test_a_full_backlog_is_refused_at_once_by_a_probe_that_does_not_wait() -> None:
    outcome: list[BaseException | None] = []

    def probe(path: str) -> None:
        try:
            bind_socket(path)
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 - the thread reports whatever the product raised
            outcome.append(exc)

    with short_dir() as d, full_backlog(d / "edge.sock"):
        worker = threading.Thread(target=probe, args=(str(d / "edge.sock"),), daemon=True)
        started = time.monotonic()
        worker.start()
        worker.join(JOIN_S)
        took = time.monotonic() - started
        still_waiting = worker.is_alive()
        assert (d / "edge.sock").exists()  # the live socket's file is left where it was
    worker.join(JOIN_S)  # the cleanup made room: a blocked connect has returned by now
    assert not still_waiting, "the probe is still waiting for room in the backlog of a live listener"
    assert took < FAST_S, f"the probe took {took:.2f} s to answer a full backlog"
    assert len(outcome) == 1 and isinstance(outcome[0], EdgeSocketError) and CANNOT_TELL in str(outcome[0])


def test_the_event_loop_keeps_ticking_while_the_probe_meets_a_full_backlog() -> None:
    ticks: list[float] = []

    async def ticker() -> None:
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.005)

    async def start(path: str) -> None:
        task = asyncio.ensure_future(ticker())
        await asyncio.sleep(0.03)  # the ticker is running
        before = len(ticks)
        try:
            started = time.monotonic()
            with pytest.raises(EdgeSocketError, match=CANNOT_TELL):
                bind_socket(path)  # on the loop thread, as in ``EdgeListener.start``
            held = time.monotonic() - started  # how long the loop could not run
            await asyncio.sleep(0.03)
            assert len(ticks) > before  # the loop was handed back and went on
            assert held < FAST_S, f"the probe held the event loop for {held:.2f} s"
        finally:
            task.cancel()

    def too_slow(_signum: int, _frame: FrameType | None) -> None:
        raise LoopBlocked("the probe blocked the event loop")  # a blocking connect would otherwise hang the suite

    previous = signal.signal(signal.SIGALRM, too_slow)
    try:
        with short_dir() as d, full_backlog(d / "edge.sock"):
            signal.alarm(3)
            try:
                asyncio.run(start(str(d / "edge.sock")))
            finally:
                signal.alarm(0)
    finally:
        signal.signal(signal.SIGALRM, previous)
