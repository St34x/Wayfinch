# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin revoke`` bounds (T1.15e): an unexpected signer-client exception still runs the plugin leg, the
admin-socket call is bounded by its connect timeout and its one deadline, and the answer cap stops a peer that holds
the connection or floods. Runs ``admin.main`` or ``call_admin_socket``
against the fake signer, the fake admin socket and raw sockets."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import fake_admin_socket
import fake_signer
import pytest
from fake_admin_socket import AdminScript, confirm

from glasses import admin, admin_devices, admin_protocol, signer_client
from glasses.signer_client import SignerClient, Timeouts

FAST = Timeouts(connect_s=1.0, op_s=0.5, enrol_s=1.0)
DEV = "0123456789abcdef"
MARKER = "planted-marker-s2-must-not-show"
UNEXPECTED = "error: revoke: the signer call failed (unexpected error)\n"
NOT_BY_SIGNER = "error: revoke: revoked in the plugin, not confirmed by the signer\n"
NOT_IN_TIME = "error: revoke: the plugin did not answer in time\n"
NOT_VALID = "error: revoke: the plugin's answer was not valid\n"


@pytest.fixture
def fake():
    with fake_signer.running() as running:
        yield running


@pytest.fixture
def plugin():
    with fake_admin_socket.running() as running:
        yield running


def write_config(directory: Path, state_dir: Path) -> Path:
    path = directory / "config.yaml"
    path.write_text(
        "gateway:\n  platforms:\n    glasses:\n      extra:\n"
        f"        state_dir: {state_dir}\n"
        "        signer:\n          socket: /run/device-signer/signer.sock\n          user: nobody\n",
        encoding="utf-8",
    )
    return path


def main(tmp_path: Path, state_dir: Path) -> tuple[int, str]:
    out = io.StringIO()
    code = admin.main(["revoke", DEV, "--config", str(write_config(tmp_path, state_dir))], env={}, out=out)
    return code, out.getvalue()


def use_signer(monkeypatch, signer_socket: Path) -> None:
    client = SignerClient(signer_socket, os.geteuid(), timeouts=FAST)
    monkeypatch.setattr(signer_client, "client_from_config", lambda _cfg: client)


# --- an unexpected exception in the signer call ----------------------------------------------------------------


class _Broken:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def revoke(self, _device_id: str) -> dict[str, object]:
        raise self.error


@pytest.mark.parametrize("where", ["revoke", "client_from_config"])
@pytest.mark.parametrize("kind", [RuntimeError, TypeError, KeyError])
def test_an_unexpected_signer_exception_still_runs_the_plugin_leg(tmp_path, plugin, monkeypatch, capsys, where, kind):
    def factory(_cfg):
        if where == "client_from_config":
            raise kind(MARKER)
        return _Broken(kind(MARKER))

    monkeypatch.setattr(signer_client, "client_from_config", factory)
    code, out = main(tmp_path, plugin.state_dir)
    seen = capsys.readouterr()
    assert code == 1 and out.startswith(UNEXPECTED) and out.endswith(NOT_BY_SIGNER)
    assert plugin.requests == [admin_protocol.revoke_request(DEV)]  # the plugin leg still ran
    assert MARKER not in out + seen.out + seen.err and "Traceback" not in out + seen.out + seen.err


def test_a_keyboard_interrupt_in_the_signer_call_is_not_swallowed(tmp_path, plugin, monkeypatch):
    monkeypatch.setattr(signer_client, "client_from_config", lambda _cfg: _Broken(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        main(tmp_path, plugin.state_dir)
    assert plugin.connections == 0


# --- the one deadline and the connect timeout ------------------------------------------------------------------


def test_a_trickling_plugin_exits_1_within_the_call_deadline(tmp_path, fake, plugin, monkeypatch):
    use_signer(monkeypatch, fake.socket_path)
    monkeypatch.setattr(admin_protocol, "CALL_TIMEOUT_S", 0.5)
    plugin.script = AdminScript(trickle_s=0.05)  # ~110 bytes: about 5.5 s to finish
    start = time.monotonic()
    code, out = main(tmp_path, plugin.state_dir)
    took = time.monotonic() - start
    assert code == 1 and NOT_IN_TIME in out and took < 3.0


def _fill_backlog(path: Path) -> list[socket.socket]:
    """Pending connections on a ``listen(0)`` socket that never accepts, until the kernel refuses one more."""
    pending: list[socket.socket] = []
    for _ in range(64):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.setblocking(False)
        pending.append(client)
        try:
            client.connect(str(path))
        except (BlockingIOError, InterruptedError):
            return pending
    raise AssertionError("the backlog never filled")


@contextmanager
def full_backlog() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="ab-"))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(directory / "admin.sock"))
    listener.listen(0)
    pending = _fill_backlog(directory / "admin.sock")
    try:
        yield directory
    finally:
        for client in pending:
            client.close()
        listener.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_a_full_backlog_exits_1_within_the_connect_timeout(tmp_path, fake, monkeypatch):
    use_signer(monkeypatch, fake.socket_path)
    monkeypatch.setattr(admin_protocol, "CONNECT_TIMEOUT_S", 0.3)
    result: list[tuple[int, str]] = []
    with full_backlog() as state:
        start = time.monotonic()
        worker = threading.Thread(target=lambda: result.append(main(tmp_path, state)), daemon=True)
        worker.start()
        worker.join(5.0)  # a client without a connect timeout blocks here for good
        took = time.monotonic() - start
    assert not worker.is_alive() and took < 5.0
    code, out = result[0]
    assert code == 1 and out.count("error: revoke: the plugin") == 1 and "admin.sock" not in out


def test_an_over_cap_answer_then_holding_is_rejected_before_the_deadline(tmp_path, fake, plugin, monkeypatch):
    use_signer(monkeypatch, fake.socket_path)
    monkeypatch.setattr(admin_protocol, "CALL_TIMEOUT_S", 3.0)
    plugin.script = AdminScript(raw=lambda _d: b" " * (admin_protocol.ANSWER_CAP + 1), hold=True)
    start = time.monotonic()
    code, out = main(tmp_path, plugin.state_dir)
    took = time.monotonic() - start
    assert code == 1 and NOT_VALID in out and took < 2.0


# --- a flooding peer -------------------------------------------------------------------------------------------


@contextmanager
def flooding_peer(sent: list[int]) -> Iterator[Path]:
    """A peer that reads the request, then sends without end until the client goes away (or 8 s pass)."""
    directory = Path(tempfile.mkdtemp(prefix="af-"))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(directory / "admin.sock"))
    listener.listen(1)

    def flood() -> None:
        with contextlib.suppress(OSError):
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                conn.settimeout(8.0)
                end = time.monotonic() + 8.0
                while time.monotonic() < end:
                    sent.append(conn.send(b"x" * 8192))

    thread = threading.Thread(target=flood, daemon=True)
    thread.start()
    try:
        yield directory
    finally:
        listener.close()
        thread.join(10.0)
        shutil.rmtree(directory, ignore_errors=True)


def test_the_client_stops_reading_a_flooding_peer_at_the_cap(monkeypatch):
    monkeypatch.setattr(admin_protocol, "CALL_TIMEOUT_S", 4.0)
    sent: list[int] = []
    with flooding_peer(sent) as state:
        start = time.monotonic()
        with pytest.raises(admin_protocol.AdminAnswerRejected):
            admin_devices.call_admin_socket(state, DEV)
        took = time.monotonic() - start
    assert took < 2.0 and sum(sent) >= admin_protocol.ANSWER_CAP


def test_a_confirming_plugin_that_trickles_inside_the_deadline_is_confirmed(plugin, monkeypatch):
    monkeypatch.setattr(admin_protocol, "CALL_TIMEOUT_S", 5.0)
    plugin.script = AdminScript(raw=confirm, trickle_s=0.001)
    assert admin_devices.call_admin_socket(plugin.state_dir, DEV) == {"sessions_closed": 1}
