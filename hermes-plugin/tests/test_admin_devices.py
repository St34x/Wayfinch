# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin revoke``: the signer's ``revoke`` first, then the plugin's admin socket (SAT-SC-08, SAT-CA-46..50
as adapted for T1.15e). Runs ``admin.main`` against the fake signer and the fake admin socket."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from pathlib import Path

import fake_admin_socket
import fake_signer
import pytest
from fake_admin_socket import AdminScript, confirm
from fake_signer import Script, answer

from glasses import admin, admin_devices, admin_protocol, signer_client
from glasses.signer_client import SignerClient, Timeouts

FAST = Timeouts(connect_s=1.0, op_s=0.5, enrol_s=1.0)
DEV = "0123456789abcdef"
MARKER = "planted-marker-must-not-show"
NOT_BY_SIGNER = "error: revoke: revoked in the plugin, not confirmed by the signer\n"
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
    """A short directory: AF_UNIX paths hold at most 107 bytes, pytest's tmp_path can be longer."""
    directory = Path(tempfile.mkdtemp(prefix="ad-"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def write_config(tmp_path: Path, state_dir: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "gateway:\n  platforms:\n    glasses:\n      extra:\n"
        f"        state_dir: {state_dir}\n"
        "        signer:\n          socket: /run/device-signer/signer.sock\n          user: nobody\n",
        encoding="utf-8",
    )
    return path


def run(monkeypatch, tmp_path: Path, signer_socket: Path, state_dir: Path, device_id: str = DEV) -> tuple[int, str]:
    client = SignerClient(signer_socket, os.geteuid(), timeouts=FAST)
    monkeypatch.setattr(signer_client, "client_from_config", lambda _cfg: client)
    out = io.StringIO()
    argv = ["revoke", device_id, "--config", str(write_config(tmp_path, state_dir))]
    code = admin.main(argv, env={}, out=out)
    return code, out.getvalue()


def test_both_confirm_exit_0_with_counts_and_the_signer_ran_first(tmp_path, fake, plugin, monkeypatch):
    seen: list[list[object]] = []
    plugin.on_request = lambda: seen.append(list(fake.ops))
    code, out = run(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    assert code == 0
    assert out == (
        f"signer: revoked {DEV}, CRL number 8\n"
        f"plugin: live cut confirmed for {DEV} (sessions_closed=1)\n"
        f"revoke: {DEV} revoked (signer and plugin confirmed)\n"
    )
    assert seen == [["revoke"]]  # SAT-SC-08 / SAT-CA-46: signer sent and answered before the socket call
    assert plugin.requests == [admin_protocol.revoke_request(DEV)]


def test_already_revoked_and_a_confirming_plugin_exit_0(tmp_path, fake, plugin, monkeypatch):
    body = {"device_id": DEV, "already_revoked": True, "crl_number": 9}
    fake.scripts["revoke"] = Script(raw=lambda rid: answer(rid, body))
    code, out = run(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    assert code == 0 and f"signer: {DEV} was already revoked, CRL number 9\n" in out
    assert len(plugin.requests) == 1


SIGNER_FAILURES = {
    "timeout": (Script(delay_s=1.0), "error: revoke: signer did not answer in time\n"),
    "close": (Script(close=True), None),
    "crl-failed": (
        Script(error="crl_failed", extras={"registry_revoked": True}),
        "error: revoke: the signer wrote the tombstone but could not rebuild the CRL (crl_failed)\n",
    ),
    "crl-failed-no-flag": (Script(error="crl_failed"), "error: revoke: the signer refused (crl_failed)\n"),
    "internal": (Script(error="internal"), "error: revoke: the signer refused (internal)\n"),
    "forbidden": (Script(error="forbidden"), "error: revoke: the signer refused (forbidden)\n"),
    "marker-answer": (Script(raw=lambda rid: f"{MARKER}\n".encode()), None),
}


@pytest.mark.parametrize("case", sorted(SIGNER_FAILURES))
def test_a_signer_failure_still_calls_the_plugin_and_exits_1(tmp_path, fake, plugin, monkeypatch, case):
    script, first = SIGNER_FAILURES[case]
    fake.scripts["revoke"] = script
    code, out = run(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    assert code == 1 and out.endswith(NOT_BY_SIGNER) and len(plugin.requests) == 1
    assert fake.ops == ["revoke"] and MARKER not in out
    if first is not None:
        assert out.startswith(first)


def test_an_absent_signer_socket_still_calls_the_plugin(tmp_path, plugin, monkeypatch):
    code, out = run(monkeypatch, tmp_path, tmp_path / "no-signer.sock", plugin.state_dir)
    assert code == 1 and out.endswith(NOT_BY_SIGNER) and len(plugin.requests) == 1


def test_an_unset_signer_key_still_calls_the_plugin(tmp_path, plugin, monkeypatch):
    def not_configured(_cfg):
        raise signer_client.NotConfigured("signer.socket")

    monkeypatch.setattr(signer_client, "client_from_config", not_configured)
    out = io.StringIO()
    code = admin.main(["revoke", DEV, "--config", str(write_config(tmp_path, plugin.state_dir))], env={}, out=out)
    assert code == 1 and out.getvalue().endswith(NOT_BY_SIGNER) and len(plugin.requests) == 1


UNKNOWN = "error: revoke: the signer does not know this device\n"


def test_unknown_device_still_makes_the_live_cut_and_exits_1(tmp_path, fake, plugin, monkeypatch):
    fake.scripts["revoke"] = Script(error="unknown_device")
    code, out = run(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    assert code == 1  # never 0: the signer did not confirm
    assert out == UNKNOWN + f"plugin: live cut confirmed for {DEV} (sessions_closed=1)\n" + NOT_BY_SIGNER
    assert plugin.requests == [admin_protocol.revoke_request(DEV)]  # exactly one request, for that id


def test_unknown_device_with_the_admin_socket_down_exits_1_not_confirmed_at_all(tmp_path, short_dir, fake, monkeypatch):
    fake.scripts["revoke"] = Script(error="unknown_device")
    code, out = run(monkeypatch, tmp_path, fake.socket_path, short_dir / "state")
    assert code == 1 and out.startswith(UNKNOWN)
    assert out.endswith("error: revoke: not confirmed by the signer or the plugin\n")


@pytest.mark.parametrize("bad", ["../x", ";", "0" * 1000, DEV.upper(), DEV[:15], DEV + "0", "\x1b[2J"])
def test_a_bad_device_id_exits_2_and_sends_nothing(tmp_path, fake, plugin, monkeypatch, bad):
    code, out = run(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir, device_id=bad)
    assert (code, out) == (2, "error: revoke: not a device id (16 lower-case hex)\n")
    assert fake.requests == [] and plugin.connections == 0


def test_both_legs_failing_exit_1(tmp_path, short_dir, fake, monkeypatch):
    fake.scripts["revoke"] = Script(error="internal")
    code, out = run(monkeypatch, tmp_path, fake.socket_path, short_dir / "state")
    assert code == 1 and out.endswith("error: revoke: not confirmed by the signer or the plugin\n")


def test_an_absent_admin_socket_exits_1_after_the_signer(tmp_path, short_dir, fake, monkeypatch):
    code, out = run(monkeypatch, tmp_path, fake.socket_path, short_dir / "state")
    assert code == 1 and fake.ops == ["revoke"]
    assert "error: revoke: the plugin is not reachable (is the gateway running?)\n" in out
    assert out.endswith(NOT_BY_PLUGIN) and str(tmp_path) not in out


def test_a_slow_plugin_times_out(tmp_path, fake, plugin, monkeypatch):
    monkeypatch.setattr(admin_protocol, "CALL_TIMEOUT_S", 0.3)
    plugin.script = AdminScript(delay_s=2.0)
    code, out = run(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    assert code == 1 and "error: revoke: the plugin did not answer in time\n" in out
    assert out.endswith(NOT_BY_PLUGIN) and fake.ops == ["revoke"]


def answer_line(body: str) -> AdminScript:
    return AdminScript(raw=lambda _dev: body.encode())


PLUGIN_FAILURES = {
    "close": (AdminScript(close=True), "the plugin's answer was not valid"),
    "bad_request": (answer_line('{"v":1,"ok":false,"error":"bad_request"}\n'), "the plugin refused (bad_request)"),
    "unknown_op": (answer_line('{"v":1,"ok":false,"error":"unknown_op"}\n'), "the plugin refused (unknown_op)"),
    "internal": (answer_line('{"v":1,"ok":false,"error":"internal"}\n'), "the plugin refused (internal)"),
    "other-device": (AdminScript(raw=lambda _d: confirm("fedcba9876543210")), "the plugin's answer was not valid"),
    "over-64k": (AdminScript(raw=lambda d: confirm(d, {"a": 1})[:-2] + b" " * 70_000 + b"}\n"), None),
    "not-json": (answer_line(f"{MARKER}\n"), None),
    "duplicate-key": (answer_line(f'{{"v":1,"v":1,"ok":true,"deviceId":"{DEV}","counts":{{"a":1}}}}\n'), None),
    "nan": (answer_line(f'{{"v":1,"ok":true,"deviceId":"{DEV}","counts":{{"a":NaN}}}}\n'), None),
    "v-true": (answer_line(f'{{"v":true,"ok":true,"deviceId":"{DEV}","counts":{{"a":1}}}}\n'), None),
    "ok-1": (answer_line(f'{{"v":1,"ok":1,"deviceId":"{DEV}","counts":{{"a":1}}}}\n'), None),
    "bool-count": (AdminScript(raw=lambda d: confirm(d, {"a": True})), None),
    "17-counts": (AdminScript(raw=lambda d: confirm(d, {f"c{i}": 1 for i in range(17)})), None),
    "extra-key": (answer_line(f'{{"v":1,"ok":true,"deviceId":"{DEV}","counts":{{"a":1}},"{MARKER}":1}}\n'), None),
    "byte-after-newline": (AdminScript(raw=lambda d: confirm(d) + MARKER.encode()), None),
}


@pytest.mark.parametrize("case", sorted(PLUGIN_FAILURES))
def test_a_bad_plugin_answer_exits_1_after_the_signer(tmp_path, fake, plugin, monkeypatch, case):
    script, message = PLUGIN_FAILURES[case]
    plugin.script = script
    code, out = run(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    expected = message or "the plugin's answer was not valid"
    assert code == 1 and f"error: revoke: {expected}\n" in out and out.endswith(NOT_BY_PLUGIN)
    assert fake.ops == ["revoke"] and MARKER not in out and len(plugin.requests) == 1


REFUSED = "error: revoke: the admin socket is not a socket of this user\n"


def test_a_symlink_to_a_live_socket_is_refused(tmp_path, short_dir, fake, plugin, monkeypatch):
    state = short_dir / "s"
    state.mkdir()
    (state / "admin.sock").symlink_to(plugin.socket_path)
    code, out = run(monkeypatch, tmp_path, fake.socket_path, state)
    assert code == 1 and REFUSED in out and plugin.connections == 0 and fake.ops == ["revoke"]


@pytest.mark.parametrize("kind", ["file", "fifo", "dir"])
def test_a_non_socket_admin_path_is_refused(tmp_path, short_dir, fake, monkeypatch, kind):
    state = short_dir / "s"
    state.mkdir()
    target = state / "admin.sock"
    {"file": lambda: target.write_text("x"), "fifo": lambda: os.mkfifo(target), "dir": target.mkdir}[kind]()
    code, out = run(monkeypatch, tmp_path, fake.socket_path, state)
    assert code == 1 and REFUSED in out and fake.ops == ["revoke"]


def test_a_socket_of_another_uid_is_refused_without_connecting(tmp_path, fake, plugin, monkeypatch):
    real = os.lstat

    def lstat(path, *a, **k):
        info = real(path, *a, **k)
        if str(path).endswith("admin.sock"):
            return os.stat_result((info.st_mode, 0, 0, 1, os.geteuid() + 1, 0, 0, 0, 0, 0))
        return info

    monkeypatch.setattr(admin_devices.os, "lstat", lstat)
    code, out = run(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    assert code == 1 and REFUSED in out and plugin.connections == 0 and fake.ops == ["revoke"]


def test_a_peer_of_another_uid_gets_no_request(tmp_path, fake, plugin, monkeypatch):
    monkeypatch.setattr(admin_devices, "read_peer_uid", lambda s: os.geteuid() + 1)
    code, out = run(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    assert code == 1 and REFUSED in out and fake.ops == ["revoke"]
    assert plugin.wait_handled(1, timeout_s=2.0)  # the handler has recorded what it read, else [] would pass
    assert plugin.requests == [b""]  # connected, closed before a byte was sent


def test_an_admin_socket_path_over_107_bytes_is_refused_without_connecting(tmp_path, fake, plugin, monkeypatch):
    state = plugin.state_dir / ("d" * (108 - len(os.fsencode(plugin.state_dir))))
    state.mkdir()
    code, out = run(monkeypatch, tmp_path, fake.socket_path, state)
    assert code == 1 and REFUSED in out and plugin.connections == 0


def test_the_help_names_the_exit_codes(capsys):
    with pytest.raises(SystemExit):
        admin.main(["revoke", "--help"], env={}, out=io.StringIO())
    text = " ".join(capsys.readouterr().out.split())
    assert "signer's tombstone and CRL first" in text and "exit 0 both confirmed" in text
    assert "1 a leg not confirmed, unknown device or config errors" in text
    assert "2 bad device id, unreadable config, missing module or usage error" in text


def test_the_help_epilog_says_unknown_device_still_gets_the_live_cut(capsys):
    with pytest.raises(SystemExit):
        admin.main(["revoke", "--help"], env={}, out=io.StringIO())
    text = " ".join(capsys.readouterr().out.split())
    assert "does not know the device still gets the plugin's live cut asked for; the exit is then 1, never 0" in text


def test_the_help_says_a_config_with_errors_stops_revoke_before_both_steps(capsys):
    with pytest.raises(SystemExit):
        admin.main(["revoke", "--help"], env={}, out=io.StringIO())
    text = " ".join(capsys.readouterr().out.split())
    assert (
        "A config with errors stops revoke before both steps: fix the config first "
        "(`glasses-admin config check`), or the open session stays until a gateway restart." in text
    )
