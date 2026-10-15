# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin revoke`` when the signer does not know the device: the live cut still runs, the exit is never 0,
every plugin outcome is named, a bad id or a config with errors still sends nothing, and the help says so. Also one
run of the command as a separate process (the way an operator calls it)."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import fake_admin_socket
import fake_signer
import pytest
from fake_admin_socket import AdminScript, confirm
from fake_signer import Script

from glasses import admin, admin_protocol, signer_client
from glasses.signer_client import SignerClient, Timeouts

FAST = Timeouts(connect_s=1.0, op_s=0.5, enrol_s=1.0)
DEV = "0123456789abcdef"
MARKER = "planted-marker-must-not-show"
UNKNOWN = "error: revoke: the signer does not know this device\n"
NOT_BY_SIGNER = "error: revoke: revoked in the plugin, not confirmed by the signer\n"
NOT_AT_ALL = "error: revoke: not confirmed by the signer or the plugin\n"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent


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
    directory = Path(tempfile.mkdtemp(prefix="au-"))
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


def revoke(monkeypatch, tmp_path: Path, signer: Path, state_dir: Path, device_id: str = DEV) -> tuple[int, str]:
    client = SignerClient(signer, os.geteuid(), timeouts=FAST)
    monkeypatch.setattr(signer_client, "client_from_config", lambda _cfg: client)
    out = io.StringIO()
    code = admin.main(["revoke", device_id, "--config", str(write_config(tmp_path, state_dir))], env={}, out=out)
    return code, out.getvalue()


PLUGIN_ANSWERS = {
    "confirms": (AdminScript(), f"plugin: live cut confirmed for {DEV} (sessions_closed=1)\n", NOT_BY_SIGNER),
    "confirms-with-zero-counts": (
        AdminScript(raw=lambda dev: confirm(dev, {"sessions_closed": 0, "tokens_killed": 0})),
        f"plugin: live cut confirmed for {DEV} (sessions_closed=0, tokens_killed=0)\n",
        NOT_BY_SIGNER,
    ),
    "refuses-bad-request": (
        AdminScript(raw=lambda _dev: b'{"v":1,"ok":false,"error":"bad_request"}\n'),
        "error: revoke: the plugin refused (bad_request)\n",
        NOT_AT_ALL,
    ),
    "refuses-unknown-op": (
        AdminScript(raw=lambda _dev: b'{"v":1,"ok":false,"error":"unknown_op"}\n'),
        "error: revoke: the plugin refused (unknown_op)\n",
        NOT_AT_ALL,
    ),
    "refuses-internal": (
        AdminScript(raw=lambda _dev: b'{"v":1,"ok":false,"error":"internal"}\n'),
        "error: revoke: the plugin refused (internal)\n",
        NOT_AT_ALL,
    ),
    "closes-without-an-answer": (AdminScript(close=True), None, NOT_AT_ALL),
    "answers-garbage": (AdminScript(raw=lambda _dev: f"{MARKER}\n".encode()), None, NOT_AT_ALL),
    "answers-for-another-device": (
        AdminScript(raw=lambda _dev: confirm("fedcba9876543210")),
        "error: revoke: the plugin's answer was not valid\n",
        NOT_AT_ALL,
    ),
}


@pytest.mark.parametrize("case", sorted(PLUGIN_ANSWERS))
def test_unknown_device_exits_1_whatever_the_plugin_answers(tmp_path, fake, plugin, monkeypatch, case):
    script, plugin_line, summary = PLUGIN_ANSWERS[case]
    plugin.script = script
    fake.scripts["revoke"] = Script(error="unknown_device")
    code, out = revoke(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    assert code == 1  # never 0: the signer did not confirm
    assert out.startswith(UNKNOWN) and out.endswith(summary) and MARKER not in out
    if plugin_line is not None:
        assert out == UNKNOWN + plugin_line + summary
    assert fake.ops == ["revoke"] and plugin.requests == [admin_protocol.revoke_request(DEV)]


SIGNER_NOT_CONFIRMING = {
    "unknown-device": Script(error="unknown_device"),
    "crl-failed": Script(error="crl_failed", extras={"registry_revoked": True}),
    "internal": Script(error="internal"),
    "closed": Script(close=True),
    "garbage": Script(raw=lambda _rid: f"{MARKER}\n".encode()),
}


@pytest.mark.parametrize("signer_case", sorted(SIGNER_NOT_CONFIRMING))
@pytest.mark.parametrize("plugin_case", ["confirms", "refuses-internal", "closes-without-an-answer"])
def test_a_signer_that_does_not_confirm_never_gives_exit_0(
    tmp_path, fake, plugin, monkeypatch, signer_case, plugin_case
):
    fake.scripts["revoke"] = SIGNER_NOT_CONFIRMING[signer_case]
    plugin.script = PLUGIN_ANSWERS[plugin_case][0]
    code, out = revoke(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    assert code == 1 and out.endswith((NOT_BY_SIGNER, NOT_AT_ALL)) and MARKER not in out
    assert len(plugin.requests) == 1


def test_unknown_device_asks_the_plugin_only_after_the_signer_answered(tmp_path, fake, plugin, monkeypatch):
    fake.scripts["revoke"] = Script(error="unknown_device")
    seen: list[list[object]] = []
    plugin.on_request = lambda: seen.append(list(fake.ops))
    code, _out = revoke(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    assert code == 1 and seen == [["revoke"]]


def test_unknown_device_with_an_absent_admin_socket_says_not_confirmed_at_all(tmp_path, short_dir, fake, monkeypatch):
    fake.scripts["revoke"] = Script(error="unknown_device")
    code, out = revoke(monkeypatch, tmp_path, fake.socket_path, short_dir / "no-state")
    assert code == 1
    assert out.splitlines()[0] == UNKNOWN.strip() and out.endswith(NOT_AT_ALL)
    assert str(short_dir) not in out and DEV not in out


def test_unknown_device_with_a_plugin_that_never_answers_ends_with_exit_1_inside_the_deadline(
    tmp_path, fake, plugin, monkeypatch
):
    monkeypatch.setattr(admin_protocol, "CALL_TIMEOUT_S", 0.5)
    fake.scripts["revoke"] = Script(error="unknown_device")
    plugin.script = AdminScript(delay_s=3.0)
    code, out = revoke(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir)
    plugin.stopping.set()
    assert code == 1 and out.endswith(NOT_AT_ALL) and "did not answer in time" in out


# --- the id check still comes before any socket call ---

BAD_IDS = [
    "",
    " ",
    " " + DEV[:15],
    DEV + "\n",
    DEV + " ",
    DEV[:8] + "\x00" + DEV[9:],
    "０１２３４５６７８９ａｂｃｄｅｆ",  # full-width digits and letters: 16 characters, not 16 ASCII hex
    "0x" + DEV[:14],
    DEV.upper(),
    "../" + DEV,
    DEV + "/../x",
    "$(touch x)",
]


@pytest.mark.parametrize("bad", BAD_IDS, ids=[f"bad{i}" for i in range(len(BAD_IDS))])
def test_a_bad_id_sends_nothing_when_the_signer_would_say_unknown_device(tmp_path, fake, plugin, monkeypatch, bad):
    fake.scripts["revoke"] = Script(error="unknown_device")
    code, out = revoke(monkeypatch, tmp_path, fake.socket_path, plugin.state_dir, device_id=bad)
    assert code == 2 and out == "error: revoke: not a device id (16 lower-case hex)\n"
    assert fake.requests == [] and plugin.connections == 0 and plugin.requests == []


def test_a_bad_id_beats_a_config_with_errors_and_a_missing_config(tmp_path, fake, plugin, monkeypatch):
    client = SignerClient(fake.socket_path, os.geteuid(), timeouts=FAST)
    monkeypatch.setattr(signer_client, "client_from_config", lambda _cfg: client)
    broken = write_config(tmp_path, plugin.state_dir, extra="        port: -5\n")
    for config in (broken, tmp_path / "missing.yaml"):
        out = io.StringIO()
        code = admin.main(["revoke", "nope", "--config", str(config)], env={}, out=out)
        assert code == 2 and out.getvalue() == "error: revoke: not a device id (16 lower-case hex)\n"
    assert fake.requests == [] and plugin.connections == 0


# --- a config with errors ---

CONFIG_ERROR_LINES = (
    "error: revoke: the glasses config has errors - run glasses-admin config check\n"
    "error: revoke: neither the signer nor the plugin was asked; fix the config first, "
    "or the open session stays until a gateway restart\n"
)


@pytest.mark.parametrize(
    "extra",
    [
        "        port: -5\n",
        f"        max_sessions: {MARKER}\n        port: -5\n",
        f"        port: {MARKER}\n",
        f"        port: {MARKER}\n        max_sessions: -1\n",
    ],
    ids=["one-error", "two-errors", "marker-as-port", "marker-and-negative-sessions"],
)
def test_a_config_with_errors_prints_the_two_fixed_lines_and_leaks_nothing(tmp_path, fake, plugin, monkeypatch, extra):
    fake.scripts["revoke"] = Script(error="unknown_device")
    client = SignerClient(fake.socket_path, os.geteuid(), timeouts=FAST)
    monkeypatch.setattr(signer_client, "client_from_config", lambda _cfg: client)
    config = write_config(tmp_path, plugin.state_dir, extra=extra)
    out = io.StringIO()
    code = admin.main(["revoke", DEV, "--config", str(config)], env={}, out=out)
    text = out.getvalue()
    assert (code, text) == (1, CONFIG_ERROR_LINES)
    for leaked in (MARKER, str(tmp_path), str(plugin.state_dir), "/run/device-signer", "nobody", "config.yaml"):
        assert leaked not in text
    assert fake.requests == [] and plugin.connections == 0 and plugin.requests == []


def test_a_config_with_errors_exits_1_and_prints_exactly_two_lines(tmp_path, fake, plugin, monkeypatch):
    client = SignerClient(fake.socket_path, os.geteuid(), timeouts=FAST)
    monkeypatch.setattr(signer_client, "client_from_config", lambda _cfg: client)
    config = write_config(tmp_path, plugin.state_dir, extra="        port: -5\n")
    out = io.StringIO()
    assert admin.main(["revoke", DEV, "--config", str(config)], env={}, out=out) == 1
    assert out.getvalue() == CONFIG_ERROR_LINES
    assert DEV not in out.getvalue()


def test_a_config_that_is_not_yaml_is_an_unreadable_config_with_no_content_in_the_output(tmp_path, fake, plugin):
    config = tmp_path / "config.yaml"
    config.write_text(f"{MARKER}: [unclosed\n  - : :\n", encoding="utf-8")
    out = io.StringIO()
    code = admin.main(["revoke", DEV, "--config", str(config)], env={}, out=out)
    assert code == 2 and out.getvalue().startswith("error: ") and MARKER not in out.getvalue()
    assert fake.requests == [] and plugin.connections == 0


# --- the help ---

WHEN_UNKNOWN = (
    "A signer that does not know the device still gets the plugin's live cut asked for; the exit is then 1, never 0."
)
WHEN_CONFIG = (
    "A config with errors stops revoke before both steps: fix the config first "
    "(`glasses-admin config check`), or the open session stays until a gateway restart."
)


@pytest.mark.parametrize("columns", ["30", "60", "80", "200"])
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_the_revoke_help_carries_both_sentences_at_any_terminal_width(capsys, monkeypatch, columns, flag):
    monkeypatch.setenv("COLUMNS", columns)
    with pytest.raises(SystemExit) as stop:
        admin.main(["revoke", flag], env={}, out=io.StringIO())
    text = " ".join(capsys.readouterr().out.split()).replace(
        "glasses- admin", "glasses-admin"
    )  # argparse may wrap at the hyphen
    assert stop.value.code == 0
    assert WHEN_UNKNOWN in text and WHEN_CONFIG in text
    assert "Traceback" not in text and "%" not in text


def test_the_top_level_help_still_lists_revoke(capsys):
    with pytest.raises(SystemExit) as stop:
        admin.main(["--help"], env={}, out=io.StringIO())
    assert stop.value.code == 0 and "revoke" in capsys.readouterr().out


def test_the_module_docstring_and_the_help_say_the_same_two_things():
    from glasses import admin_devices

    doc = " ".join((admin_devices.__doc__ or "").split())
    assert "unknown_device`` included" in doc and "never 0" in doc
    assert "A config with errors stops revoke before both steps: fix the config first" in doc
    assert "or the open session stays until a gateway restart" in doc


# --- the command as a separate process ---

DRIVER = textwrap.dedent(
    """
    import os, sys
    from glasses import admin, signer_client
    from glasses.signer_client import SignerClient, Timeouts

    client = SignerClient(sys.argv[1], os.geteuid(), timeouts=Timeouts(connect_s=1.0, op_s=1.0, enrol_s=1.0))
    signer_client.client_from_config = lambda _cfg: client
    sys.exit(admin.main(sys.argv[2:]))
    """
)


def run_process(tmp_path: Path, signer: Path, state_dir: Path, device_id: str) -> subprocess.CompletedProcess[str]:
    driver = tmp_path / "driver_revoke.py"
    driver.write_text(DRIVER, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": os.pathsep.join([str(PLUGIN_ROOT), str(TESTS_DIR)])}
    argv = [
        sys.executable,
        str(driver),
        str(signer),
        "revoke",
        device_id,
        "--config",
        str(write_config(tmp_path, state_dir)),
    ]
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env, cwd=tmp_path, check=False)


def test_the_process_exit_status_for_unknown_device_is_1_with_the_plugin_line_on_stdout(tmp_path, fake, plugin):
    fake.scripts["revoke"] = Script(error="unknown_device")
    done = run_process(tmp_path, fake.socket_path, plugin.state_dir, DEV)
    assert done.returncode == 1 and done.stderr == ""
    assert done.stdout == UNKNOWN + f"plugin: live cut confirmed for {DEV} (sessions_closed=1)\n" + NOT_BY_SIGNER
    assert plugin.requests == [admin_protocol.revoke_request(DEV)]


def test_the_process_exit_status_for_a_bad_id_is_2_and_nothing_is_sent(tmp_path, fake, plugin):
    done = run_process(tmp_path, fake.socket_path, plugin.state_dir, "../etc")
    assert done.returncode == 2 and done.stderr == ""
    assert done.stdout == "error: revoke: not a device id (16 lower-case hex)\n"
    assert fake.requests == [] and plugin.connections == 0


def test_the_process_exit_status_for_both_confirming_is_0(tmp_path, fake, plugin):
    done = run_process(tmp_path, fake.socket_path, plugin.state_dir, DEV)
    assert done.returncode == 0 and done.stdout.endswith(f"revoke: {DEV} revoked (signer and plugin confirmed)\n")
