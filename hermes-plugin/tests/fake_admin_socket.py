# SPDX-License-Identifier: Apache-2.0
"""Test-only fake of the plugin's admin socket (``glasses.admin_protocol`` wire format v1): a threaded ``AF_UNIX``
server bound as ``admin.sock`` in a fresh short ``tempfile.mkdtemp(prefix="as-")`` directory (the test's
``state_dir``; AF_UNIX paths hold at most 107 bytes), removed at teardown. Per-test scripting: the canned confirm
(``counts {"sessions_closed": 1}``), raw answer bytes, a delay, a close without an answer, an answer trickled one byte
at a time, or an answer followed by holding the connection. It records each request line, calls ``on_request`` when a
connection arrives, and ``wait_handled`` waits until the handlers have finished."""

from __future__ import annotations

import contextlib
import json
import shutil
import socketserver
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AdminScript:
    raw: Callable[[str], bytes] | None = None  # device id -> answer bytes
    delay_s: float = 0.0
    close: bool = False  # close without an answer
    trickle_s: float = 0.0  # pause before each byte of the answer
    hold: bool = False  # keep the connection open after the answer until teardown


def confirm(device_id: str, counts: dict[str, object] | None = None) -> bytes:
    body = {"v": 1, "ok": True, "deviceId": device_id, "counts": counts or {"sessions_closed": 1}}
    return (json.dumps(body) + "\n").encode()


@dataclass
class FakeAdminSocket:
    state_dir: Path
    script: AdminScript = field(default_factory=AdminScript)
    requests: list[bytes] = field(default_factory=list)
    connections: int = 0
    on_request: Callable[[], None] | None = None
    stopping: threading.Event = field(default_factory=threading.Event)
    handled: int = 0  # connections whose handler has finished
    _done: threading.Condition = field(default_factory=threading.Condition)

    @property
    def socket_path(self) -> Path:
        return self.state_dir / "admin.sock"

    def finished(self) -> None:
        with self._done:
            self.handled += 1
            self._done.notify_all()

    def wait_handled(self, count: int = 1, timeout_s: float = 2.0) -> bool:
        """True once *count* handlers have finished (each has recorded its request line), False after *timeout_s*."""
        with self._done:
            return self._done.wait_for(lambda: self.handled >= count, timeout_s)

    def reply(self, line: bytes) -> bytes | None:
        self.requests.append(line)
        script = self.script
        if script.delay_s:
            self.stopping.wait(script.delay_s)
        if script.close:
            return None
        try:
            device_id = str(json.loads(line).get("deviceId"))
        except ValueError:
            device_id = ""
        return script.raw(device_id) if script.raw else confirm(device_id)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    fake: FakeAdminSocket


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        fake = self.server.fake  # type: ignore[attr-defined]
        try:
            self._serve(fake)
        finally:
            fake.finished()

    def _serve(self, fake: FakeAdminSocket) -> None:
        fake.connections += 1
        if fake.on_request is not None:
            fake.on_request()
        line = self.rfile.readline(8192)
        answer = fake.reply(line)
        if answer is None:
            return
        with contextlib.suppress(OSError):
            self._write(fake, answer)
            if fake.script.hold:
                fake.stopping.wait(10)

    def _write(self, fake: FakeAdminSocket, answer: bytes) -> None:
        if not fake.script.trickle_s:
            self.wfile.write(answer)
            return
        for index in range(len(answer)):
            if fake.stopping.wait(fake.script.trickle_s):
                return
            self.wfile.write(answer[index : index + 1])
            self.wfile.flush()


@contextmanager
def running() -> Iterator[FakeAdminSocket]:
    directory = Path(tempfile.mkdtemp(prefix="as-"))
    fake = FakeAdminSocket(directory)
    server = _Server(str(fake.socket_path), _Handler)
    server.fake = fake
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        fake.stopping.set()
        server.shutdown()
        server.server_close()
        shutil.rmtree(directory, ignore_errors=True)
