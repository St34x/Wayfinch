# SPDX-License-Identifier: Apache-2.0
"""The signer socket client against the fake signer: the socket and peer checks refuse before a byte is sent, every
malformed or oversized answer is a typed error, each call has one deadline, nothing is retried, the client can send
exactly the seven allowed ops and each op round-trips (config keys and the test guard: ``test_signer_client_config``;
request read-back, typed failures and the answer framing: ``test_signer_client_framing``)."""

from __future__ import annotations

import contextlib
import os
import socket
import time
from pathlib import Path

import fake_signer
import pytest

from glasses import signer_client
from glasses import signer_protocol as proto
from glasses.signer_client import SignerClient, Timeouts

SPKI, CODE, SIG = "AAAA", "A" * 22, "AAAA"
FAST = Timeouts(connect_s=1.0, op_s=1.0, enrol_s=1.0)


@pytest.fixture
def fake():
    with fake_signer.running() as running:
        yield running


def client(fake, **kwargs) -> SignerClient:
    return SignerClient(fake.socket_path, os.geteuid(), timeouts=kwargs.pop("timeouts", FAST), **kwargs)


# Socket checks (refused before a byte is sent)


def test_a_symlink_to_the_signer_socket_is_refused_before_sending(fake, tmp_path):
    link = tmp_path / "s.sock"
    link.symlink_to(fake.socket_path)
    with pytest.raises(signer_client.SocketRefused):
        SignerClient(link, os.geteuid(), timeouts=FAST).status()
    assert fake.requests == []


def test_a_regular_file_at_the_socket_path_is_refused(tmp_path):
    path = tmp_path / "s.sock"
    path.write_bytes(b"")
    with pytest.raises(signer_client.SocketRefused):
        SignerClient(path, os.geteuid(), timeouts=FAST).status()


def test_a_socket_owned_by_another_user_is_refused_before_sending(fake):
    with pytest.raises(signer_client.SocketRefused):
        SignerClient(fake.socket_path, os.geteuid() + 1, timeouts=FAST).status()
    assert fake.requests == []


def test_a_peer_with_another_uid_is_refused_before_sending(fake):
    with pytest.raises(signer_client.PeerRefused):
        client(fake, peer_uid=os.geteuid() + 1).status()
    time.sleep(0.1)
    assert fake.requests == []


def test_a_missing_socket_is_a_typed_connection_error(tmp_path):
    with pytest.raises(signer_client.SignerUnavailable):
        SignerClient(tmp_path / "absent.sock", os.geteuid(), timeouts=FAST).status()


def test_the_default_peer_check_reads_the_real_peer_uid(fake, monkeypatch):
    seen = []

    def spy(sock: socket.socket, _real=signer_client.read_peer_uid) -> int:
        seen.append(_real(sock))
        return seen[-1]

    monkeypatch.setattr(signer_client, "read_peer_uid", spy)
    client(fake).status()
    assert seen == [os.geteuid()]


def test_peer_uid_only_sets_the_expected_uid_and_cannot_switch_the_check_off(fake):
    with pytest.raises(TypeError):
        client(fake, peer_uid=lambda _sock: os.geteuid())
    assert client(fake, peer_uid=os.geteuid()).window_close() == {"was_open": True}


# Answers (fail closed)


def raw(body: bytes):
    return fake_signer.Script(raw=lambda _rid: body)


def with_rid(make):
    return fake_signer.Script(raw=make)


BAD_ANSWERS = {
    "oversize": (raw(b" " * proto.ANSWER_CAP + b"\n"), proto.AnswerTooLarge),
    "oversize-no-newline": (raw(b"x" * (proto.ANSWER_CAP + 10)), proto.AnswerTooLarge),
    "not-json": (raw(b"hello\n"), proto.AnswerNotJson),
    "no-newline": (raw(b'{"v":1}'), proto.AnswerNotJson),
    "closed-without-answer": (fake_signer.Script(close=True), proto.AnswerNotJson),
    "byte-after-newline": (with_rid(lambda r: fake_signer.answer(r, {"was_open": True}) + b"x"), proto.AnswerNotJson),
    "duplicate-key": (
        with_rid(lambda r: f'{{"v":1,"v":1,"rid":"{r}","ok":true,"was_open":true}}\n'.encode()),
        proto.AnswerNotJson,
    ),
    "wrong-rid": (with_rid(lambda _r: fake_signer.answer("0" * 32, {"was_open": True})), proto.RidMismatch),
    "v-2": (
        with_rid(lambda r: fake_signer.answer(r, {"was_open": True}).replace(b'"v": 1', b'"v": 2')),
        proto.AnswerSchemaError,
    ),
    "ok-missing": (with_rid(lambda r: f'{{"v":1,"rid":"{r}","was_open":true}}\n'.encode()), proto.AnswerSchemaError),
    "extra-key": (with_rid(lambda r: fake_signer.answer(r, {"was_open": True, "x": 1})), proto.AnswerSchemaError),
    "missing-key": (with_rid(lambda r: fake_signer.answer(r, {})), proto.AnswerSchemaError),
    "unknown-error-code": (fake_signer.Script(error="no_such_code"), proto.AnswerSchemaError),
}


@pytest.mark.parametrize("case", sorted(BAD_ANSWERS))
def test_a_malformed_answer_is_a_typed_error(fake, case):
    script, error = BAD_ANSWERS[case]
    fake.scripts["window_close"] = script
    with pytest.raises(error):
        client(fake).window_close()
    assert fake.ops == ["window_close"]


def test_a_rate_limited_answer_carries_retry_after(fake):
    fake.scripts["status"] = fake_signer.Script(error="rate_limited", extras={"retry_after_s": 30})
    with pytest.raises(proto.SignerRefusal) as info:
        client(fake).status()
    assert (info.value.code, info.value.retry_after_s, info.value.registry_revoked) == ("rate_limited", 30, None)


def test_a_crl_failure_after_the_tombstone_carries_registry_revoked(fake):
    fake.scripts["revoke"] = fake_signer.Script(error="crl_failed", extras={"registry_revoked": True})
    with pytest.raises(proto.SignerRefusal) as info:
        client(fake).revoke(fake_signer.DEVICE_ID)
    assert (info.value.code, info.value.retry_after_s, info.value.registry_revoked) == ("crl_failed", None, True)


# Deadlines and no retry


def test_the_default_timeouts_are_2_10_and_340_seconds(tmp_path):
    assert Timeouts() == Timeouts(connect_s=2.0, op_s=10.0, enrol_s=340.0)
    assert SignerClient(tmp_path / "s.sock", 1).timeouts == Timeouts()


def test_an_op_that_answers_too_late_is_a_timeout(fake):
    fake.scripts["status"] = fake_signer.Script(delay_s=2.0)
    with pytest.raises(signer_client.SignerTimeout):
        client(fake, timeouts=Timeouts(op_s=0.3, enrol_s=5.0)).status()


def test_enrol_waits_for_the_enrol_timeout_not_the_op_timeout(fake):
    fake.scripts["enrol"] = fake_signer.Script(delay_s=0.5)
    assert client(fake, timeouts=Timeouts(op_s=0.2, enrol_s=5.0)).enrol(SPKI, CODE, SIG).device_id


def test_an_enrol_that_answers_too_late_is_a_timeout_and_is_sent_exactly_once(fake):
    fake.scripts["enrol"] = fake_signer.Script(delay_s=2.0)
    with pytest.raises(signer_client.SignerTimeout):
        client(fake, timeouts=Timeouts(op_s=5.0, enrol_s=0.3)).enrol(SPKI, CODE, SIG)
    assert fake.ops == ["enrol"]


@pytest.mark.parametrize("script", [fake_signer.Script(close=True), fake_signer.Script(error="internal")])
def test_a_failed_enrol_is_never_retried(fake, script):
    fake.scripts["enrol"] = script
    with pytest.raises(proto.SignerError):
        client(fake).enrol(SPKI, CODE, SIG)
    time.sleep(0.1)
    assert fake.ops == ["enrol"]


def test_a_slowly_dripping_answer_cannot_extend_the_deadline(fake):
    fake.scripts["window_close"] = fake_signer.Script(drip_s=0.05)
    started = time.monotonic()
    with pytest.raises(signer_client.SignerTimeout):
        client(fake, timeouts=Timeouts(op_s=0.5)).window_close()
    assert time.monotonic() - started < 1.5


def test_a_connect_that_does_not_finish_in_time_is_a_timeout(fake, monkeypatch):
    seen = []

    class StalledSocket(socket.socket):
        def connect(self, address):
            seen.append(self.gettimeout())
            raise TimeoutError  # what a blocking connect raises when its timeout runs out

    monkeypatch.setattr(signer_client.socket, "socket", StalledSocket)
    with pytest.raises(signer_client.SignerTimeout):
        SignerClient(fake.socket_path, os.geteuid(), timeouts=Timeouts(connect_s=0.3, op_s=5.0)).status()
    assert seen == [pytest.approx(0.3)]
    assert fake.requests == []


def fill_backlog(path: Path, queued: list[socket.socket]) -> None:
    for sock in queued:
        sock.setblocking(False)
        with contextlib.suppress(BlockingIOError):
            sock.connect(str(path))


def test_a_signer_with_a_full_backlog_fails_at_once_without_hanging(fake):
    path = fake.socket_path.parent / "q.sock"
    queued = [socket.socket(socket.AF_UNIX) for _ in range(2)]
    try:
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            server.listen(0)
            fill_backlog(path, queued)
            started = time.monotonic()
            with pytest.raises(signer_client.SignerUnavailable):
                SignerClient(path, os.geteuid(), timeouts=Timeouts(connect_s=0.3, op_s=5.0)).status()
            assert time.monotonic() - started < 2.0
    finally:
        for sock in queued:
            sock.close()
        path.unlink()


# Ops: the client half of the allowlist, and a round trip each


def test_the_public_methods_are_exactly_the_seven_allowed_ops():
    methods = {name for name in dir(SignerClient) if not name.startswith("_") and callable(getattr(SignerClient, name))}
    assert methods == proto.OPS
    assert not methods & {"window_open", "window_confirm", "ca_init", "rotate_begin", "rotate_finish", "renew"}


CALLS = {
    "status": lambda c: c.status(),
    "enrol": lambda c: c.enrol(SPKI, CODE, SIG),
    "revoke": lambda c: c.revoke(fake_signer.DEVICE_ID),
    "crl_generate": lambda c: c.crl_generate(),
    "window_status": lambda c: c.window_status(),
    "window_close": lambda c: c.window_close(),
    "rotate_status": lambda c: c.rotate_status(),
}


def test_the_client_sends_only_the_seven_allowed_ops(fake):
    for call in CALLS.values():
        call(client(fake))
    assert set(fake.ops) == proto.OPS == set(CALLS)
    assert len(fake.ops) == 7


@pytest.mark.parametrize("op", sorted(CALLS))
def test_each_op_round_trips_against_the_fake(fake, op):
    result = CALLS[op](client(fake))
    (request,) = fake.requests
    assert request.parsed is not None and request.parsed["op"] == op and request.raw.endswith(b"\n")
    expected = fake_signer.canned(op, request.parsed)
    assert result == (signer_client.EnrolResult(**expected) if op == "enrol" else expected)


@pytest.mark.parametrize("rotation", ["none", "overlap"])
def test_status_and_rotate_status_report_the_rotation_state(rotation):
    with fake_signer.running(rotation) as fake:
        assert client(fake).status()["rotation"] == {
            "state": rotation,
            "devices_on_previous": int(rotation == "overlap"),
        }
        assert client(fake).rotate_status()["state"] == rotation


def test_an_invalid_field_is_refused_before_connecting(fake):
    with pytest.raises(proto.InvalidField):
        client(fake).revoke("not-a-device-id")
    assert fake.requests == []
