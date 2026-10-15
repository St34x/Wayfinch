# SPDX-License-Identifier: Apache-2.0
"""Failure behaviour of the signer socket client: one deadline per call that signals and slow peers cannot stretch, no
retry of any op, typed errors that echo no input and carry no printable chain, the device token kept out of errors and
logs, the test guard that keeps every test off a real signer, and the test fake kept out of production code."""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path

import fake_signer
import pytest

from glasses import config, signer_client
from glasses import signer_protocol as proto
from glasses.signer_client import SignerClient, Timeouts
from glasses.signer_client import public_file as public_file_bound_at_import

from conftest import StateDirOutsideTmpError

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SPKI, CODE, SIG = "AAAA", "A" * 22, "AAAA"
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


def rid_of(line: bytes) -> str:
    return str(json.loads(line)["rid"])


# One deadline per call, no retry


def test_a_silent_signer_times_out_at_the_op_deadline_and_is_asked_once(fake):
    fake.scripts["crl_generate"] = fake_signer.Script(delay_s=3.0)
    started = time.monotonic()
    with pytest.raises(signer_client.SignerTimeout) as info:
        client(fake.socket_path, timeouts=Timeouts(connect_s=0.2, op_s=0.6, enrol_s=5.0)).crl_generate()
    elapsed = time.monotonic() - started
    assert 0.55 <= elapsed < 0.6 + SLACK_S
    time.sleep(0.2)
    assert fake.ops == ["crl_generate"]
    assert_clean(info.value)


def test_an_enrol_deadline_is_the_enrol_timeout_from_connect(fake):
    fake.scripts["enrol"] = fake_signer.Script(delay_s=3.0)
    started = time.monotonic()
    with pytest.raises(signer_client.SignerTimeout):
        client(fake.socket_path, timeouts=Timeouts(connect_s=0.1, op_s=0.1, enrol_s=0.7)).enrol(SPKI, CODE, SIG)
    assert 0.65 <= time.monotonic() - started < 0.7 + SLACK_S
    time.sleep(0.2)
    assert fake.ops == ["enrol"]


def test_a_dripping_answer_inside_the_deadline_succeeds(fake):
    fake.scripts["window_close"] = fake_signer.Script(drip_s=0.002)
    assert client(fake.socket_path, timeouts=Timeouts(op_s=5.0)).window_close() == {"was_open": True}


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs setitimer")
def test_signals_during_the_wait_neither_break_nor_stretch_the_deadline(fake):
    ticks = []
    previous = signal.signal(signal.SIGALRM, lambda *_a: ticks.append(1))
    signal.setitimer(signal.ITIMER_REAL, 0.01, 0.01)
    try:
        fake.scripts["window_close"] = fake_signer.Script(delay_s=0.3, drip_s=0.001)
        assert client(fake.socket_path, timeouts=Timeouts(op_s=3.0)).window_close() == {"was_open": True}
        fake.scripts["window_close"] = fake_signer.Script(delay_s=3.0)
        started = time.monotonic()
        with pytest.raises(signer_client.SignerTimeout):
            client(fake.socket_path, timeouts=Timeouts(op_s=0.5)).window_close()
        assert time.monotonic() - started < 0.5 + SLACK_S
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0, 0)
        signal.signal(signal.SIGALRM, previous)
    assert len(ticks) > 10


class CountingSocket(socket.socket):
    connects: list[object] = []

    def connect(self, address):
        CountingSocket.connects.append(address)
        return super().connect(address)


FAILURES = {
    "timeout": (fake_signer.Script(delay_s=2.0), signer_client.SignerTimeout),
    "close": (fake_signer.Script(close=True), proto.AnswerNotJson),
    "internal": (fake_signer.Script(error="internal"), proto.SignerRefusal),
    "rate-limited": (fake_signer.Script(error="rate_limited", extras={"retry_after_s": 1}), proto.SignerRefusal),
    "not-json": (fake_signer.Script(raw=lambda _r: b"nope\n"), proto.AnswerNotJson),
}
OPS = {
    "status": lambda c: c.status(),
    "enrol": lambda c: c.enrol(SPKI, CODE, SIG),
    "revoke": lambda c: c.revoke(fake_signer.DEVICE_ID),
    "crl_generate": lambda c: c.crl_generate(),
    "window_status": lambda c: c.window_status(),
    "window_close": lambda c: c.window_close(),
    "rotate_status": lambda c: c.rotate_status(),
}


@pytest.mark.parametrize("failure", sorted(FAILURES))
@pytest.mark.parametrize("op", sorted(OPS))
def test_no_op_is_retried_after_any_failure(fake, monkeypatch, op, failure):
    script, error = FAILURES[failure]
    fake.scripts[op] = script
    monkeypatch.setattr(CountingSocket, "connects", [])
    monkeypatch.setattr(signer_client.socket, "socket", CountingSocket)
    with pytest.raises(error):
        OPS[op](client(fake.socket_path, timeouts=Timeouts(connect_s=0.3, op_s=0.4, enrol_s=0.4)))
    time.sleep(0.15)
    assert len(CountingSocket.connects) == 1
    assert fake.ops == [op]


def test_every_call_uses_a_fresh_random_rid(fake):
    c = client(fake.socket_path)
    for _ in range(3):
        c.status()
    rids = [rid_of(r.raw) for r in fake.requests]
    assert len(set(rids)) == 3
    assert all(len(r) == 32 and set(r) <= set("0123456789abcdef") for r in rids)


def test_concurrent_calls_on_one_client_each_get_their_own_answer(fake):
    c, results, errors = client(fake.socket_path, timeouts=Timeouts(op_s=5.0)), [], []

    def call() -> None:
        try:
            results.append(c.revoke(fake_signer.DEVICE_ID)["device_id"])
        except proto.SignerError as exc:  # pragma: no cover - reported by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == [] and results == [fake_signer.DEVICE_ID] * 6


# Typed errors: no input echo, no printable chain


def test_a_missing_socket_error_does_not_print_the_path(tmp_path):
    path = tmp_path / ("absent-" + MARKER + ".sock")
    with pytest.raises(signer_client.SignerUnavailable) as info:
        client(path).status()
    assert_clean(info.value, MARKER)


def test_a_connection_error_keeps_no_context_at_all(tmp_path):
    with pytest.raises(signer_client.SignerUnavailable) as info:
        client(tmp_path / ("absent-" + MARKER + ".sock")).status()
    assert info.value.__context__ is None


def test_an_error_answer_with_planted_input_echoes_none_of_it(fake):
    fake.scripts["status"] = fake_signer.Script(raw=lambda rid: fake_signer.answer(rid, {"error": MARKER}, ok=False))
    with pytest.raises(proto.SignerError) as info:
        client(fake.socket_path).status()
    assert_clean(info.value, MARKER)


def test_a_socket_creation_failure_is_a_typed_error(fake, monkeypatch):
    def no_fds(*_args: object, **_kwargs: object) -> socket.socket:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(signer_client.socket, "socket", no_fds)
    with pytest.raises(signer_client.SignerUnavailable):
        client(fake.socket_path).status()


# The device token


TOKEN = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()


def enrol_answer(rid: str, **extra: object) -> bytes:
    body = {"device_id": fake_signer.DEVICE_ID, "cert": fake_signer.CERT_PEM, "token": TOKEN, **extra}
    return fake_signer.answer(rid, body)


def test_the_device_token_is_returned_and_never_logged(fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.scripts["enrol"] = fake_signer.Script(raw=enrol_answer)
    c = client(fake.socket_path)
    assert c.enrol(SPKI, CODE, SIG).token == TOKEN
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(c) and TOKEN not in str(c)


@pytest.mark.parametrize(
    "make",
    [
        lambda rid: enrol_answer(rid, extra=TOKEN),
        lambda rid: enrol_answer("0" * 32),
        lambda rid: enrol_answer(rid)[:-1],
        lambda rid: enrol_answer(rid) + enrol_answer(rid),
        lambda rid: enrol_answer(rid).replace(b'"ok": true', b'"ok": true, "ok": true'),
    ],
    ids=["extra-key", "wrong-rid", "no-newline", "two-lines", "duplicate-key"],
)
def test_a_refused_enrol_answer_never_shows_the_token(fake, caplog, make):
    caplog.set_level(logging.DEBUG)
    fake.scripts["enrol"] = fake_signer.Script(raw=make)
    with pytest.raises(proto.SignerError) as info:
        client(fake.socket_path).enrol(SPKI, CODE, SIG)
    assert_clean(info.value, TOKEN)
    assert TOKEN not in caplog.text


def test_the_enrol_result_does_not_show_the_token_in_repr(fake):
    fake.scripts["enrol"] = fake_signer.Script(raw=enrol_answer)
    result = client(fake.socket_path).enrol(SPKI, CODE, SIG)
    assert TOKEN not in repr(result) and TOKEN not in str(result)


# The test guard (no test reaches a real signer)


def test_the_guard_follows_a_symlink_in_tmp_path_to_a_real_socket_path(tmp_path):
    link = tmp_path / "s.sock"
    link.symlink_to("/run/device-signer/signer.sock")
    with pytest.raises(StateDirOutsideTmpError):
        SignerClient(link, 1)


def test_the_guard_checks_a_client_made_by_dataclass_replace(fake):
    with pytest.raises(StateDirOutsideTmpError):
        replace(client(fake.socket_path), socket_path=Path("/run/device-signer/signer.sock"))


def test_the_guard_checks_the_configured_client(monkeypatch):
    monkeypatch.setattr(signer_client.pwd, "getpwnam", lambda _n: type("P", (), {"pw_uid": os.geteuid() + 3}))
    settings = config.load({"signer": {"socket": "/run/device-signer/signer.sock", "user": "signer"}})
    with pytest.raises(StateDirOutsideTmpError):
        signer_client.client_from_config(settings)


def test_the_guard_refuses_a_path_beside_the_session_temp_dir(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    with pytest.raises(StateDirOutsideTmpError):
        SignerClient(base.parent / (base.name + "x") / "s.sock", 1)


def test_the_guard_covers_public_file_bound_by_name_at_import():
    settings = config.load({"signer": {"public_dir": "/var/lib/device-signer/public"}})
    with pytest.raises(StateDirOutsideTmpError):
        public_file_bound_at_import(settings, proto.PUBLIC_CRL)


# The fake stays a test tool


def test_no_production_module_names_the_fake_signer():
    hits = [p for p in (PLUGIN_ROOT / "glasses").rglob("*.py") if "fake_signer" in p.read_text(encoding="utf-8")]
    assert hits == []


def test_the_fake_signer_is_not_importable_from_the_plugin_package_path(tmp_path):
    probe = (
        "import sys; sys.path[:0] = [sys.argv[1]]\n"
        "import glasses.signer_client\n"
        "try:\n    import fake_signer\nexcept ModuleNotFoundError:\n    print('absent')\nelse:\n    print('present')\n"
        "try:\n    import glasses.fake_signer\nexcept ModuleNotFoundError:\n    print('absent')\n"
    )
    script = tmp_path / "probe.py"
    script.write_text(probe, encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-I", str(script), str(PLUGIN_ROOT)], capture_output=True, text=True, check=True, cwd=tmp_path
    )
    assert out.stdout.split() == ["absent", "absent"]
