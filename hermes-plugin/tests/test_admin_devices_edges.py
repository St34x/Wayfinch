# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin revoke`` edges: exit codes of an unusable config and of argparse, fixed-text output, the admin
socket's single deadline, a peer that never answers / closes mid-line / streams without a newline, the answer cap,
a socket swapped after the ``lstat`` check, and unchained client errors."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import fake_admin_socket
import fake_signer
import pytest
from fake_admin_socket import confirm
from fake_signer import Script

from glasses import admin, admin_devices, admin_protocol, signer_client
from glasses.signer_client import SignerClient, Timeouts

FAST = Timeouts(connect_s=1.0, op_s=0.5, enrol_s=1.0)
DEV = "0123456789abcdef"
NOT_BY_PLUGIN = (
    "error: revoke: revoked by the signer, new connections are refused; "
    "an open session stays until the plugin's live cut or a gateway restart\n"
)


@pytest.fixture
def fake():
    with fake_signer.running() as running:
        yield running


@pytest.fixture
def plugin():
    with fake_admin_socket.running() as running:
        yield running


@pytest.fixture
def short_dir():
    directory = Path(tempfile.mkdtemp(prefix="ae-"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def write_config(tmp_path: Path, state_dir: Path, extra: str = "") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "gateway:\n  platforms:\n    glasses:\n      extra:\n"
        f"        state_dir: {state_dir}\n"
        "        signer:\n          socket: /run/device-signer/signer.sock\n          user: nobody\n" + extra,
        encoding="utf-8",
    )
    return path


def patch_signer(monkeypatch, signer_socket: Path) -> None:
    client = SignerClient(signer_socket, os.geteuid(), timeouts=FAST)
    monkeypatch.setattr(signer_client, "client_from_config", lambda _cfg: client)


def main(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    code = admin.main(argv, env={}, out=out)
    return code, out.getvalue()


# --- exit codes of the steps before the signer -------------------------------------------------------------------


def test_an_unreadable_config_exits_2_and_neither_leg_runs(tmp_path, fake, plugin, monkeypatch):
    patch_signer(monkeypatch, fake.socket_path)
    code, out = main(["revoke", DEV, "--config", str(tmp_path / "missing.yaml")])
    assert code == 2 and out.startswith("error: ") and out.count("\n") == 1
    assert fake.requests == [] and plugin.connections == 0


def test_a_config_with_errors_exits_1_and_neither_leg_runs(tmp_path, fake, plugin, monkeypatch):
    patch_signer(monkeypatch, fake.socket_path)
    config = write_config(tmp_path, plugin.state_dir, extra="        max_sessions: not-a-number\n        port: -5\n")
    code, out = main(["revoke", DEV, "--config", str(config)])
    assert (code, out) == (
        1,
        "error: revoke: the glasses config has errors - run glasses-admin config check\n"
        "error: revoke: neither the signer nor the plugin was asked; fix the config first, "
        "or the open session stays until a gateway restart\n",
    )
    assert str(tmp_path) not in out and "not-a-number" not in out
    assert fake.requests == [] and plugin.connections == 0


@pytest.mark.parametrize(
    "argv", [["revoke"], ["revoke", DEV, "--force"], ["revoke", DEV, "--json"], ["revoke", DEV, DEV]]
)
def test_a_usage_error_exits_2_and_sends_nothing(tmp_path, fake, plugin, monkeypatch, argv):
    patch_signer(monkeypatch, fake.socket_path)
    with pytest.raises(SystemExit) as caught:
        main([*argv, "--config", str(write_config(tmp_path, plugin.state_dir))])
    assert caught.value.code == 2 and fake.requests == [] and plugin.connections == 0


# --- fixed-text output ---------------------------------------------------------------------------------------


def test_an_absent_signer_and_a_confirming_plugin_print_exactly_three_fixed_lines(tmp_path, plugin, monkeypatch):
    patch_signer(monkeypatch, tmp_path / "nope.sock")
    code, out = main(["revoke", DEV, "--config", str(write_config(tmp_path, plugin.state_dir))])
    lines = out.splitlines()
    assert code == 1 and len(lines) == 3 and lines[0].startswith("error: revoke: ")
    assert lines[1:] == [
        f"plugin: live cut confirmed for {DEV} (sessions_closed=1)",
        "error: revoke: revoked in the plugin, not confirmed by the signer",
    ]
    assert str(tmp_path) not in out and "nope" not in out and "Errno" not in out


def test_a_refused_signer_user_still_calls_the_plugin_with_a_fixed_line(tmp_path, plugin, monkeypatch):
    def refused(_cfg):
        raise signer_client.SignerUserRefused()

    monkeypatch.setattr(signer_client, "client_from_config", refused)
    code, out = main(["revoke", DEV, "--config", str(write_config(tmp_path, plugin.state_dir))])
    assert code == 1 and len(plugin.requests) == 1
    assert out.startswith(f"error: revoke: {signer_client.SignerUserRefused.message}\n")


def test_the_counts_are_printed_sorted_by_name(tmp_path, fake, plugin, monkeypatch):
    patch_signer(monkeypatch, fake.socket_path)
    plugin.script = fake_admin_socket.AdminScript(raw=lambda d: confirm(d, {"zeta": 2, "alpha": 0, "mid_1": 7}))
    code, out = main(["revoke", DEV, "--config", str(write_config(tmp_path, plugin.state_dir))])
    assert code == 0 and f"plugin: live cut confirmed for {DEV} (alpha=0, mid_1=7, zeta=2)\n" in out


@pytest.mark.parametrize("kind", ["stale", "timeout"])
def test_a_plugin_failure_prints_no_path_and_writes_no_file(tmp_path, short_dir, fake, monkeypatch, kind):
    patch_signer(monkeypatch, fake.socket_path)
    state = short_dir / "st"
    state.mkdir()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(state / "admin.sock"))
    if kind == "stale":
        listener.close()  # the file stays, nobody listens: ECONNREFUSED
    else:
        listener.listen(1)  # accepted by the kernel, never answered
        monkeypatch.setattr(admin_protocol, "CALL_TIMEOUT_S", 0.3)
    config = write_config(tmp_path, state)
    before = sorted(p.name for p in state.iterdir()), sorted(p.name for p in tmp_path.iterdir())
    try:
        code, out = main(["revoke", DEV, "--config", str(config)])
    finally:
        listener.close()
    message = {
        "stale": "the plugin is not reachable (is the gateway running?)",
        "timeout": "the plugin did not answer in time",
    }
    assert code == 1 and f"error: revoke: {message[kind]}\n" in out and out.endswith(NOT_BY_PLUGIN)
    assert str(short_dir) not in out and "admin.sock" not in out and "Errno" not in out
    assert (sorted(p.name for p in state.iterdir()), sorted(p.name for p in tmp_path.iterdir())) == before


# --- the admin-socket client against hostile peers ---------------------------------------------------------------


@contextmanager
def raw_server(serve: Callable[[socket.socket], None]) -> Iterator[Path]:
    """One-connection AF_UNIX server in a short directory running *serve(conn)*."""
    directory = Path(tempfile.mkdtemp(prefix="ar-"))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(directory / "admin.sock"))
    listener.listen(4)
    stop = threading.Event()

    def run() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            with contextlib.suppress(OSError):
                serve(conn)
            stop.wait(5)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield directory
    finally:
        stop.set()
        listener.close()
        thread.join(6)
        shutil.rmtree(directory, ignore_errors=True)


def timed(state_dir: Path) -> tuple[type[BaseException] | None, BaseException | None, float]:
    start = time.monotonic()
    try:
        admin_devices.call_admin_socket(state_dir, DEV)
    except admin_protocol.AdminError as exc:
        return type(exc), exc, time.monotonic() - start
    return None, None, time.monotonic() - start


def test_a_peer_that_accepts_and_never_reads_or_answers_times_out(monkeypatch):
    monkeypatch.setattr(admin_protocol, "CALL_TIMEOUT_S", 0.4)
    with raw_server(lambda conn: None) as state:
        kind, exc, took = timed(state)
    assert kind is admin_devices.AdminTimeout and took < 2.0
    assert exc.__cause__ is None and exc.__context__ is None


def test_a_partial_line_then_close_is_rejected():
    with raw_server(
        lambda conn: (conn.recv(4096), conn.sendall(b'{"v":1,"ok":tr'), conn.shutdown(socket.SHUT_RDWR))
    ) as state:
        kind, _exc, _took = timed(state)
    assert kind is admin_protocol.AdminAnswerRejected


def test_a_huge_line_without_a_newline_is_rejected_without_waiting_for_the_deadline(monkeypatch):
    monkeypatch.setattr(admin_protocol, "CALL_TIMEOUT_S", 5.0)
    sent = []

    def flood(conn: socket.socket) -> None:
        conn.recv(4096)
        conn.settimeout(3)
        chunk = b"x" * 8192
        total = 0
        while total < 4 * admin_protocol.ANSWER_CAP:
            total += conn.send(chunk)
            sent.append(total)

    with raw_server(flood) as state:
        kind, _exc, took = timed(state)
    assert kind is admin_protocol.AdminAnswerRejected and took < 4.0


def test_one_deadline_covers_the_whole_call_while_bytes_trickle(monkeypatch):
    monkeypatch.setattr(admin_protocol, "CALL_TIMEOUT_S", 0.5)

    def trickle(conn: socket.socket) -> None:
        conn.recv(4096)
        for _ in range(40):
            conn.sendall(b" ")
            time.sleep(0.1)

    with raw_server(trickle) as state:
        kind, _exc, took = timed(state)
    assert kind is admin_devices.AdminTimeout and took < 1.5


def test_an_answer_line_without_the_server_closing_is_not_confirmed(monkeypatch):
    monkeypatch.setattr(admin_protocol, "CALL_TIMEOUT_S", 0.4)
    with raw_server(lambda conn: (conn.recv(4096), conn.sendall(confirm(DEV)))) as state:
        kind, _exc, _took = timed(state)
    assert kind is admin_devices.AdminTimeout


def test_an_answer_of_exactly_the_cap_is_accepted_and_one_byte_more_is_rejected():
    head = confirm(DEV)[:-2]  # without "}\n"
    exact = head + b" " * (admin_protocol.ANSWER_CAP - len(head) - 2) + b"}\n"
    assert len(exact) == admin_protocol.ANSWER_CAP
    with raw_server(lambda conn: (conn.recv(4096), conn.sendall(exact))) as state:
        assert admin_devices.call_admin_socket(state, DEV) == {"sessions_closed": 1}
    over = head + b" " * (admin_protocol.ANSWER_CAP - len(head) - 1) + b"}\n"
    with raw_server(lambda conn: (conn.recv(4096), conn.sendall(over))) as state:
        kind, _exc, _took = timed(state)
    assert kind is admin_protocol.AdminAnswerRejected


def test_the_request_is_exactly_the_wire_line(plugin):
    admin_devices.call_admin_socket(plugin.state_dir, DEV)
    assert plugin.requests == [b'{"deviceId":"0123456789abcdef","op":"revoke","v":1}\n']


class _Recording(socket.socket):
    timeouts: list[float | None] = []

    def settimeout(self, value):  # type: ignore[override]
        if self.family == socket.AF_UNIX:
            _Recording.timeouts.append(value)
        super().settimeout(value)


def test_connect_uses_the_connect_timeout_read_at_call_time(plugin, monkeypatch):
    monkeypatch.setattr(admin_protocol, "CONNECT_TIMEOUT_S", 0.75)
    _Recording.timeouts = []
    monkeypatch.setattr(admin_devices.socket, "socket", _Recording)
    admin_devices.call_admin_socket(plugin.state_dir, DEV)
    assert _Recording.timeouts and _Recording.timeouts[0] == 0.75
    assert all(t is not None and t <= admin_protocol.CALL_TIMEOUT_S for t in _Recording.timeouts)


@pytest.mark.parametrize("kind", ["absent", "stale"])
def test_client_errors_are_unchained_and_name_no_path(short_dir, kind):
    if kind == "stale":
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(short_dir / "admin.sock"))
        listener.close()
    with pytest.raises(admin_devices.AdminUnreachable) as caught:
        admin_devices.call_admin_socket(short_dir, DEV)
    exc = caught.value
    assert exc.__cause__ is None and exc.__context__ is None and str(short_dir) not in str(exc)


def test_a_socket_swapped_for_a_symlink_after_the_check_gets_no_request_from_a_foreign_peer(short_dir, monkeypatch):
    """lstat sees the real socket; the path is then swapped for a symlink to another listener whose peer is foreign."""
    with fake_admin_socket.running() as first, fake_admin_socket.running() as second:
        real = os.lstat

        def lstat_then_swap(path, *a, **k):
            info = real(path, *a, **k)
            if str(path) == str(first.socket_path):
                os.unlink(path)
                os.symlink(second.socket_path, path)
            return info

        monkeypatch.setattr(admin_devices.os, "lstat", lstat_then_swap)
        monkeypatch.setattr(admin_devices, "read_peer_uid", lambda s: os.geteuid() + 1)
        with pytest.raises(admin_devices.AdminSocketRefused):
            admin_devices.call_admin_socket(first.state_dir, DEV)
        time.sleep(0.1)
        assert [r for r in second.requests if r] == [] and [r for r in first.requests if r] == []


def test_unknown_device_from_the_signer_prints_no_device_id_on_its_own_line(tmp_path, short_dir, fake, monkeypatch):
    patch_signer(monkeypatch, fake.socket_path)
    fake.scripts["revoke"] = Script(error="unknown_device")
    config = write_config(tmp_path, short_dir / "state")  # no admin socket: no plugin line names the id
    code, out = main(["revoke", DEV, "--config", str(config)])
    assert code == 1 and DEV not in out and "the signer does not know this device\n" in out


def test_unknown_device_with_a_refusing_plugin_exits_1_and_names_the_leg(tmp_path, fake, plugin, monkeypatch):
    patch_signer(monkeypatch, fake.socket_path)
    fake.scripts["revoke"] = Script(error="unknown_device")
    plugin.script = fake_admin_socket.AdminScript(raw=lambda _dev: b'{"v":1,"ok":false,"error":"bad_request"}\n')
    code, out = main(["revoke", DEV, "--config", str(write_config(tmp_path, plugin.state_dir))])
    assert code == 1 and "the plugin refused (bad_request)\n" in out and len(plugin.requests) == 1
    assert out.endswith("error: revoke: not confirmed by the signer or the plugin\n")


@pytest.mark.parametrize(("size", "refused"), [(108, True), (107, False)])
def test_the_socket_path_limit_is_107_bytes(short_dir, size, refused):
    """A path of 108 bytes is refused before any lstat; 107 bytes is checked (absent here: not reachable)."""
    pad = size - len(os.fsencode(short_dir)) - len(b"/x/admin.sock")
    state = short_dir / ("x" * (pad + 1))
    assert len(os.fsencode(state / "admin.sock")) == size
    expected = admin_devices.AdminSocketRefused if refused else admin_devices.AdminUnreachable
    with pytest.raises(expected):
        admin_devices.call_admin_socket(state, DEV)


@pytest.mark.parametrize("tail", [b"\n", b" \n", b"\r\n"])
def test_a_blank_second_line_after_the_answer_is_rejected(tail):
    with pytest.raises(admin_protocol.AdminAnswerRejected):
        admin_protocol.decode_revoke_answer(DEV, confirm(DEV) + tail)


@pytest.mark.parametrize("data", [bytearray(confirm(DEV)), memoryview(confirm(DEV))])
def test_a_non_bytes_buffer_answer_is_rejected(data):
    with pytest.raises(admin_protocol.AdminAnswerRejected):
        admin_protocol.decode_revoke_answer(DEV, data)  # type: ignore[arg-type]


def test_a_plugin_error_code_outside_the_set_is_not_a_refusal():
    line = b'{"v":1,"ok":false,"error":"unknown_device"}\n'
    with pytest.raises(admin_protocol.AdminAnswerRejected):
        admin_protocol.decode_revoke_answer(DEV, line)


def test_a_count_name_of_32_characters_and_the_count_bounds_are_accepted():
    counts = {"a" * 32: 0, "b": admin_protocol.MAX_COUNT}
    assert admin_protocol.decode_revoke_answer(DEV, confirm(DEV, counts)) == counts
