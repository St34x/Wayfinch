# SPDX-License-Identifier: Apache-2.0
"""The signer client's last checks before and after the socket: the request is read back from its own line and only
an exact, allowed line is sent; every failure (socket creation, a NUL in the path, an expired deadline) is a typed
error without a chained or suppressed context; the answer is read until the signer closes, so a byte after the
newline is refused and a signer that keeps the connection open runs into the deadline; the enrol token stays out of
``repr``/``str``/``format``/logs."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import fake_signer
import pytest

from glasses import signer_client
from glasses import signer_protocol as proto
from glasses.signer_client import SignerClient, Timeouts

FAST = Timeouts(connect_s=1.0, op_s=1.0, enrol_s=1.0)
RID = "a" * 32
TOKEN = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()


@pytest.fixture
def fake():
    with fake_signer.running() as running:
        yield running


def client(path: Path, **kwargs) -> SignerClient:
    return SignerClient(path, os.geteuid(), timeouts=kwargs.pop("timeouts", FAST), **kwargs)


# The request is read back from its own line before connecting


def tampered(line: bytes) -> proto.Request:
    request = proto.build_request("window_close", RID)
    object.__setattr__(request, "line", line)
    return request


def line_of(**message: object) -> bytes:
    return json.dumps({"v": 1, "op": "window_close", "rid": RID, **message}, separators=(",", ":")).encode() + b"\n"


class SpoofedOp(str):
    """Equal to every op and hashed like ``status``: ``in``/``==`` checks pass while the text is another op."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return hash("status")


HAND_BUILT = {
    "root-op": (tampered(line_of(op="window_open")), proto.UnknownOp),
    "other-allowed-op": (tampered(line_of(op="status")), proto.RequestRefused),
    "other-rid": (tampered(line_of(rid="b" * 32)), proto.RequestRefused),
    "extra-key": (tampered(line_of(x="1")), proto.InvalidField),
    "duplicate-op-key": (tampered(line_of()[:-2] + b',"op":"window_open"}\n'), proto.RequestRefused),
    "two-lines": (tampered(line_of() + line_of()), proto.RequestRefused),
    "no-newline": (tampered(line_of()[:-1]), proto.RequestRefused),
    "two-newlines": (tampered(line_of() + b"\n"), proto.RequestRefused),
    "spaced": (tampered(line_of().replace(b",", b", ")), proto.RequestRefused),
    "v-true": (tampered(line_of(v=True)), proto.RequestRefused),
    "not-bytes": (tampered(line_of().decode()), proto.RequestRefused),
    "over-cap": (tampered(line_of()[:-2] + b" " * proto.REQUEST_CAP + b"}\n"), proto.RequestRefused),
    "spoofed-op": (tampered(line_of(op="window_open")), proto.RequestRefused),
}


@pytest.mark.parametrize("case", sorted(HAND_BUILT))
def test_a_hand_built_request_is_refused_before_connecting(fake, monkeypatch, case):
    request, error = HAND_BUILT[case]
    if case == "spoofed-op":
        object.__setattr__(request, "op", SpoofedOp("window_open"))
    monkeypatch.setattr(signer_client.proto, "build_request", lambda *_a: request)
    monkeypatch.setattr(signer_client.socket, "socket", lambda *_a: pytest.fail("the client connected"))
    with pytest.raises(error) as info:
        client(fake.socket_path).window_close()
    assert info.value.__context__ is None
    assert fake.requests == []


def test_an_untampered_request_is_sent_byte_for_byte(fake):
    client(fake.socket_path).revoke(fake_signer.DEVICE_ID)
    (recorded,) = fake.requests
    rid = str(recorded.parsed["rid"]) if recorded.parsed else ""
    assert recorded.raw == line_of(op="revoke", rid=rid, device_id=fake_signer.DEVICE_ID)


# Every failure is typed, with no context attached


def test_a_socket_creation_failure_keeps_no_context(fake, monkeypatch):
    def no_fds(*_args: object) -> socket.socket:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(signer_client.socket, "socket", no_fds)
    with pytest.raises(signer_client.SignerUnavailable) as info:
        client(fake.socket_path).status()
    assert info.value.__context__ is None and info.value.__cause__ is None


def test_a_nul_in_the_socket_path_is_a_typed_error(tmp_path):
    made = client(tmp_path / "s.sock")
    object.__setattr__(made, "socket_path", tmp_path / "s\0.sock")  # past the test guard, which cannot resolve it
    with pytest.raises(signer_client.SignerUnavailable) as info:
        made.status()
    assert info.value.__context__ is None


@pytest.mark.parametrize("overrun_s", [0.0, 5.0], ids=["exactly-expired", "past"])
def test_a_deadline_expired_after_connect_is_a_timeout_and_nothing_is_sent(fake, monkeypatch, overrun_s):
    clock = [1000.0]

    class SlowConnect(socket.socket):
        def connect(self, address):
            super().connect(address)
            clock[0] += FAST.op_s + overrun_s  # the whole deadline (and more) went by while connecting

    monkeypatch.setattr(signer_client, "time", type("Clock", (), {"monotonic": staticmethod(lambda: clock[0])}))
    monkeypatch.setattr(signer_client.socket, "socket", SlowConnect)
    with pytest.raises(signer_client.SignerTimeout) as info:
        client(fake.socket_path, timeouts=Timeouts(connect_s=5.0, op_s=FAST.op_s)).status()
    assert info.value.__context__ is None
    time.sleep(0.1)
    assert fake.requests == []


# The answer is read until the signer closes (brief section 4: one line, then close)


@contextlib.contextmanager
def peer(behaviour: Callable[[socket.socket, bytes], None]) -> Iterator[Path]:
    """A one-connection raw peer in a short registered directory (the guard allows it while it runs)."""
    directory = Path(tempfile.mkdtemp(prefix="fs-"))
    fake_signer.SOCKET_DIRS.append(directory)
    path, server = directory / "p.sock", socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)
    server.settimeout(5.0)

    def serve() -> None:
        with contextlib.suppress(OSError):
            conn, _ = server.accept()
            with conn:
                behaviour(conn, conn.makefile("rb").readline(proto.REQUEST_CAP + 1))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield path
    finally:
        thread.join(10)
        server.close()
        path.unlink()
        directory.rmdir()
        fake_signer.SOCKET_DIRS.remove(directory)


def answer_to(line: bytes) -> bytes:
    return fake_signer.answer(str(json.loads(line)["rid"]), {"was_open": True})


def test_a_byte_after_the_newline_in_a_later_read_is_refused():
    def late(conn: socket.socket, line: bytes) -> None:
        conn.sendall(answer_to(line))
        time.sleep(0.2)
        conn.sendall(b"x")

    with peer(late) as path, pytest.raises(proto.AnswerNotJson):
        client(path).window_close()


def test_an_answer_followed_by_a_close_returns_without_waiting_for_the_deadline():
    with peer(lambda conn, line: conn.sendall(answer_to(line))) as path:
        started = time.monotonic()
        assert client(path, timeouts=Timeouts(op_s=5.0)).window_close() == {"was_open": True}
        assert time.monotonic() - started < 1.0


# The enrol token


def test_the_enrol_token_is_in_no_repr_str_format_log_or_copy(fake, caplog):
    caplog.set_level(logging.DEBUG)
    body = {"device_id": fake_signer.DEVICE_ID, "cert": fake_signer.CERT_PEM, "token": TOKEN}
    fake.scripts["enrol"] = fake_signer.Script(raw=lambda rid: fake_signer.answer(rid, body))
    result = client(fake.socket_path).enrol("AAAA", "A" * 22, "AAAA")
    assert (result.device_id, result.cert, result.token) == (fake_signer.DEVICE_ID, fake_signer.CERT_PEM, TOKEN)
    logging.getLogger("probe").info("enrolled %s %r", result, result)
    shown = [repr(result), str(result), f"{result}", f"{result!r}", format(result), repr(replace(result))]
    assert not [text for text in shown if TOKEN in text]
    assert TOKEN not in caplog.text
