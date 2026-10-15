# SPDX-License-Identifier: Apache-2.0
"""Edge cases of the signer socket client: the socket path in every wrong shape and peers that close early, stall,
flood, answer twice or answer for another request (deadlines, retries, error hygiene, the device token and the test
guard are in ``test_signer_client_failures.py``)."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path

import fake_signer
import pytest

from glasses import signer_client
from glasses import signer_protocol as proto
from glasses.signer_client import SignerClient, Timeouts

FAST = Timeouts(connect_s=1.0, op_s=1.0, enrol_s=1.0)
SLACK_S = 0.4
MARKER = "zq" * 20  # planted in answers; must never show up in an error


@pytest.fixture
def fake():
    with fake_signer.running() as running:
        yield running


def client(path: Path, **kwargs) -> SignerClient:
    return SignerClient(path, os.geteuid(), timeouts=kwargs.pop("timeouts", FAST), **kwargs)


def rendered(exc: BaseException) -> str:
    """Everything a log handler or a traceback print would show of *exc*."""
    return "".join(traceback.format_exception(exc)) + repr(exc) + str(exc) + repr(exc.args)


def assert_clean(exc: BaseException, *needles: str) -> None:
    assert isinstance(exc, proto.SignerError)
    assert exc.__cause__ is None
    assert exc.__context__ is None or exc.__suppress_context__
    text = rendered(exc)
    for needle in needles:
        assert needle not in text


# A scripted raw peer for shapes the fake cannot produce (hold the connection open, flood, a late second line)


@contextlib.contextmanager
def raw_peer(behaviour: Callable[[socket.socket, bytes], None]) -> Iterator[tuple[Path, list[bytes]]]:
    directory = Path(tempfile.mkdtemp(prefix="fs-"))
    fake_signer.SOCKET_DIRS.append(directory)
    path, seen, stop = directory / "r.sock", [], threading.Event()
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(8)
    server.settimeout(0.05)

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            with conn, contextlib.suppress(OSError):
                conn.settimeout(5.0)
                line = conn.makefile("rb").readline(proto.REQUEST_CAP + 1)
                seen.append(line)
                behaviour(conn, line)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield path, seen
    finally:
        stop.set()
        thread.join(10)
        server.close()
        path.unlink()
        directory.rmdir()
        fake_signer.SOCKET_DIRS.remove(directory)


def rid_of(line: bytes) -> str:
    return str(json.loads(line)["rid"])


def window_close_answer(line: bytes) -> bytes:
    return fake_signer.answer(rid_of(line), {"was_open": True})


# Socket path shapes (refused before anything is sent)


def test_a_directory_at_the_socket_path_is_refused(tmp_path):
    with pytest.raises(signer_client.SocketRefused):
        client(tmp_path).status()


def test_a_fifo_at_the_socket_path_is_refused(tmp_path):
    path = tmp_path / "s.sock"
    os.mkfifo(path)
    with pytest.raises(signer_client.SocketRefused):
        client(path).status()


def test_a_dangling_symlink_at_the_socket_path_is_refused(tmp_path):
    link = tmp_path / "s.sock"
    link.symlink_to(tmp_path / "absent.sock")
    with pytest.raises(signer_client.SocketRefused):
        client(link).status()


def test_a_symlink_to_a_regular_file_is_refused(tmp_path):
    (tmp_path / "f").write_bytes(b"")
    link = tmp_path / "s.sock"
    link.symlink_to(tmp_path / "f")
    with pytest.raises(signer_client.SocketRefused):
        client(link).status()


def test_a_socket_with_no_listener_is_unavailable_at_once(tmp_path, fake):
    path = fake.socket_path.parent / "dead.sock"
    with socket.socket(socket.AF_UNIX) as bound:
        bound.bind(str(path))  # bound, never listening: connect gets ECONNREFUSED
        started = time.monotonic()
        with pytest.raises(signer_client.SignerUnavailable) as info:
            client(path).status()
        assert time.monotonic() - started < 0.5
    path.unlink()
    assert_clean(info.value, str(path))


@pytest.mark.parametrize("mode", [0o600, 0o666, 0o777])
def test_the_socket_mode_is_not_judged_only_owner_type_and_peer(fake, mode):
    os.chmod(fake.socket_path, mode)
    assert client(fake.socket_path).window_close() == {"was_open": True}


def test_an_owner_mismatch_is_refused_whatever_the_peer_reports(fake):
    with pytest.raises(signer_client.SocketRefused):
        SignerClient(fake.socket_path, os.geteuid() + 1, peer_uid=os.geteuid(), timeouts=FAST).status()
    assert fake.requests == []


def test_a_peer_uid_read_failure_is_unavailable_and_nothing_is_sent(fake, monkeypatch):
    def broken(_sock: socket.socket) -> int:
        raise OSError(95, "not supported " + MARKER)

    monkeypatch.setattr(signer_client, "read_peer_uid", broken)
    with pytest.raises(signer_client.SignerUnavailable) as info:
        client(fake.socket_path).status()
    time.sleep(0.1)
    assert fake.requests == []
    assert_clean(info.value, MARKER)


def test_a_root_peer_is_refused_when_the_signer_user_is_not_root(fake, monkeypatch):
    monkeypatch.setattr(signer_client, "read_peer_uid", lambda _s: 0)
    with pytest.raises(signer_client.PeerRefused):
        client(fake.socket_path).status()
    time.sleep(0.1)
    assert fake.requests == []


@pytest.mark.xfail(
    strict=True,
    reason="known gap: a symlink in a parent directory of the socket path is followed; only the last component is "
    "lstat-ed (owner and peer checks still hold)",
)
def test_a_symlinked_parent_directory_of_the_socket_is_refused(fake, tmp_path):
    parent_link = tmp_path / "d"
    parent_link.symlink_to(fake.socket_path.parent)
    with pytest.raises(signer_client.SocketRefused):
        client(parent_link / fake.socket_path.name).status()


# Answer shapes


def test_an_answer_of_exactly_the_cap_is_accepted():
    def exact(conn: socket.socket, line: bytes) -> None:
        body = window_close_answer(line)[:-2] + b"}\n"
        pad = proto.ANSWER_CAP - len(body)
        conn.sendall(body[:-2] + b" " * pad + b"}\n")

    with raw_peer(exact) as (path, _seen):
        assert client(path).window_close() == {"was_open": True}


def test_an_answer_one_byte_over_the_cap_is_too_large():
    def over(conn: socket.socket, line: bytes) -> None:
        body = window_close_answer(line)
        conn.sendall(body[:-2] + b" " * (proto.ANSWER_CAP + 1 - len(body)) + b"}\n")

    with raw_peer(over) as (path, _seen), pytest.raises(proto.AnswerTooLarge) as info:
        client(path).window_close()
    assert_clean(info.value)


def test_an_endless_answer_without_newline_stops_at_the_cap_before_the_deadline():
    sent = []

    def flood(conn: socket.socket, _line: bytes) -> None:
        chunk = b"x" * 4096
        with contextlib.suppress(OSError):
            for _ in range(10_000):
                conn.sendall(chunk)
                sent.append(len(chunk))

    started = time.monotonic()
    with raw_peer(flood) as (path, _seen), pytest.raises(proto.AnswerTooLarge):
        client(path, timeouts=Timeouts(op_s=3.0)).window_close()
    assert time.monotonic() - started < 2.0


def test_a_partial_line_then_a_held_connection_times_out_at_the_deadline():
    def partial(conn: socket.socket, _line: bytes) -> None:
        conn.sendall(b'{"v":1,')
        time.sleep(2.0)

    with raw_peer(partial) as (path, seen):
        started = time.monotonic()
        with pytest.raises(signer_client.SignerTimeout) as info:
            client(path, timeouts=Timeouts(op_s=0.5)).window_close()
        elapsed = time.monotonic() - started
    assert 0.45 <= elapsed < 0.5 + SLACK_S
    assert len(seen) == 1
    assert_clean(info.value)


def test_an_answer_on_a_held_connection_is_a_timeout_at_the_deadline():
    def held(conn: socket.socket, line: bytes) -> None:
        conn.sendall(window_close_answer(line))
        time.sleep(2.0)

    with raw_peer(held) as (path, _seen):
        started = time.monotonic()
        with pytest.raises(signer_client.SignerTimeout):
            client(path, timeouts=Timeouts(op_s=0.5)).window_close()  # the signer closes after its answer
        assert 0.45 <= time.monotonic() - started < 0.5 + SLACK_S


def test_a_second_line_sent_after_a_pause_is_refused():
    def two(conn: socket.socket, line: bytes) -> None:
        conn.sendall(window_close_answer(line))
        time.sleep(0.2)
        conn.sendall(window_close_answer(line))

    with raw_peer(two) as (path, _seen), pytest.raises(proto.SignerError):
        client(path).window_close()


def test_two_lines_in_one_write_are_refused():
    def two(conn: socket.socket, line: bytes) -> None:
        conn.sendall(window_close_answer(line) * 2)

    with raw_peer(two) as (path, _seen), pytest.raises(proto.AnswerNotJson):
        client(path).window_close()


def test_an_answer_shaped_for_another_op_is_a_schema_error(fake):
    fake.scripts["window_close"] = fake_signer.Script(raw=lambda rid: fake_signer.answer(rid, {"open": False}))
    with pytest.raises(proto.AnswerSchemaError):
        client(fake.socket_path).window_close()


def test_an_answer_for_another_rid_echoes_neither_rid(fake):
    other = "f" * 32
    fake.scripts["status"] = fake_signer.Script(raw=lambda _rid: fake_signer.answer(other, {"x": MARKER}))
    with pytest.raises(proto.RidMismatch) as info:
        client(fake.socket_path).status()
    sent_rid = rid_of(fake.requests[0].raw)
    assert_clean(info.value, other, sent_rid, MARKER)
