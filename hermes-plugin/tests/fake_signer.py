# SPDX-License-Identifier: Apache-2.0
"""A fake device-CA signer for tests: a threaded unix-socket server that answers the seven ops the plugin may send.

By default every op gets a schema-valid canned success answer (``status`` and ``rotate_status`` in the rotation state
the fake was started with, ``none`` or ``overlap``). A test scripts one op with a ``Script``: an error code with its
extras, raw answer bytes, a delay, an answer sent one byte at a time, or a close without an answer. Every request line
is recorded (raw bytes and parsed JSON). The fake holds no authority logic: it checks no window, code or proof.

``running()`` binds the socket in a short ``tempfile.mkdtemp`` directory (an ``AF_UNIX`` path holds at most 107
bytes, fewer than a ``tmp_path`` in a worktree leaves) and lists the directory in ``SOCKET_DIRS`` while it runs; the
conftest signer guard allows exactly those directories besides the test's temp dirs.
"""

from __future__ import annotations

import contextlib
import json
import os
import socketserver
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from glasses import signer_protocol as proto

SOCKET_DIRS: list[Path] = []
CA_ID, DEVICE_ID, TIME = "0123456789abcdef", "fedcba9876543210", "2026-10-12T00:00:00Z"
SHA256 = "ab" * 32
CERT_PEM = "-----BEGIN CERTIFICATE-----\nMIIBAA==\n-----END CERTIFICATE-----\n"
TOKEN = "A" * 43  # base64url of 32 zero bytes


def canned(op: str, fields: Mapping[str, object], rotation: str = "none") -> dict[str, object]:
    """A schema-valid result for *op* (``revoke`` echoes the requested ``device_id``)."""
    crl_short = {"ca_id": CA_ID, "number": 7, "next_update": TIME}
    previous = [{"device_id": DEVICE_ID, "name": "old phone"}] if rotation == "overlap" else []
    results: dict[str, dict[str, object]] = {
        "status": {
            "ca": [{"ca_id": CA_ID, "subject_cn": "device CA", "not_after": TIME, "role": "current"}],
            "crl": [{**crl_short, "this_update": TIME, "sha256": SHA256}],
            "edge": {"state": "ok", "installed": [crl_short], "last_attempt_at": None},
            "window": {"open": False, "expires_at": None, "pending": False},
            "rotation": {"state": rotation, "devices_on_previous": len(previous)},
            "signer_version": "fake 1",
        },
        "enrol": {"device_id": DEVICE_ID, "cert": CERT_PEM, "token": TOKEN},
        "revoke": {"device_id": fields.get("device_id"), "already_revoked": False, "crl_number": 8},
        "crl_generate": {"crl": [crl_short]},
        "window_status": {
            "open": True,
            "expires_at": TIME,
            "name": "phone",
            "lanes": ["main"],
            "attempts": 0,
            "pending": None,
        },
        "window_close": {"was_open": True},
        "rotate_status": {"state": rotation, "devices_on_previous": previous},
    }
    return results[op]


@dataclass
class Script:
    """What the fake does for one op instead of the canned answer. ``raw`` gets the request's rid."""

    error: str | None = None
    extras: dict[str, object] = field(default_factory=dict)
    raw: Callable[[str], bytes] | None = None
    delay_s: float = 0.0
    drip_s: float = 0.0  # pause before each byte of the answer
    close: bool = False


@dataclass(frozen=True)
class Recorded:
    raw: bytes
    parsed: dict[str, object] | None


class FakeSigner:
    def __init__(self, directory: Path, rotation: str = "none") -> None:
        self.socket_path = directory / "s.sock"
        self.rotation = rotation
        self.scripts: dict[str, Script] = {}
        self.requests: list[Recorded] = []
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._server = _Server(str(self.socket_path), _Handler)
        self._server.fake = self

    @property
    def ops(self) -> list[object]:
        """The op of every recorded request, in order."""
        return [r.parsed.get("op") if r.parsed else None for r in self.requests]

    def reply(self, raw: bytes) -> bytes | None:
        """The answer to one request line; ``None`` closes without one."""
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        with self._lock:
            self.requests.append(Recorded(raw, parsed if isinstance(parsed, dict) else None))
        if not isinstance(parsed, dict):
            return None
        rid, op = str(parsed.get("rid")), str(parsed.get("op"))
        script = self.scripts.get(op, Script())
        self.stopping.wait(script.delay_s)
        if script.close:
            return None
        if script.raw is not None:
            return script.raw(rid)
        error = script.error or (None if op in proto.OPS else "forbidden")  # the real signer's answer to other ops
        body = {"error": error, **script.extras} if error else canned(op, parsed, self.rotation)
        return answer(rid, body, ok=error is None)


def answer(rid: str, body: Mapping[str, object], *, ok: bool = True) -> bytes:
    return json.dumps({"v": 1, "rid": rid, "ok": ok, **body}).encode() + b"\n"


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    fake: FakeSigner


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        fake = cast(_Server, self.server).fake
        raw = self.rfile.readline(proto.REQUEST_CAP + 1)
        if not raw:
            return  # the client closed before sending (refused after connecting)
        script = fake.scripts.get(str(_op(raw)), Script())
        reply = fake.reply(raw)
        with contextlib.suppress(OSError):  # the client may have timed out and gone
            if reply is not None:
                self._send(reply, script.drip_s, fake.stopping)

    def _send(self, reply: bytes, drip_s: float, stopping: threading.Event) -> None:
        if not drip_s:
            self.wfile.write(reply)
            return
        for byte in reply:
            if stopping.wait(drip_s):
                return
            self.wfile.write(bytes([byte]))


def _op(raw: bytes) -> object:
    with contextlib.suppress(ValueError, AttributeError):
        return json.loads(raw).get("op")
    return None


@contextlib.contextmanager
def running(rotation: str = "none") -> Iterator[FakeSigner]:
    """A started fake in a fresh short directory; stopped and removed afterwards."""
    directory = Path(tempfile.mkdtemp(prefix="fs-"))
    SOCKET_DIRS.append(directory)
    fake = FakeSigner(directory, rotation)
    thread = threading.Thread(target=fake._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        fake.stopping.set()
        fake._server.shutdown()
        fake._server.server_close()
        thread.join()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(fake.socket_path)
        os.rmdir(directory)
        SOCKET_DIRS.remove(directory)
