# SPDX-License-Identifier: Apache-2.0
"""``config check``, ``crl``, ``crl --check`` and ``revoke`` refuse a ``scheme://`` in ``--config``, ``--hermes-home``,
``$HERMES_HOME`` and the ``$HOME`` that supplies the default home: exit 2, one line, the value not shown, nothing
read. ``Path`` collapses ``scheme://KEY`` to ``scheme:/KEY`` during argument parsing (and in ``Path.home()``), so the
test runs on the raw string (``admin_common.local_paths``). ``Path.home()`` reads the real environment, so the
``$HOME`` cases set it with ``monkeypatch`` as well as in the ``env`` argument."""

from __future__ import annotations

import argparse
import io
import socket
from pathlib import Path

import pytest

from glasses import admin, admin_common, admin_devices, signer_client

KEY = "KEY-must-not-show-9f3a"
VALUES = [f"scheme://{KEY}", f"x://u:{KEY}@h/a", f"://{KEY}", f"{KEY}://"]
DEVICE_ID = "0123456789abcdef"
COMMANDS = {
    "config check": ["config", "check"],
    "crl": ["crl"],
    "crl --check": ["crl", "--check"],
    "revoke": ["revoke", DEVICE_ID],
}
SOURCES = ["--config", "--hermes-home", "$HERMES_HOME"]


def refusal(source: str) -> str:
    return f"error: {source}: not a local path (it contains '://'; the value is not shown)"


@pytest.fixture
def nothing_is_read(monkeypatch):
    """Every way these commands reach a file, the signer or the admin socket fails the test."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a command read something after a URL path argument")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(signer_client, "client_from_config", forbidden)
    monkeypatch.setattr(admin_devices, "call_admin_socket", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def run(argv: list[str], env: dict[str, str]) -> tuple[int, str]:
    out = io.StringIO()
    return admin.main(argv, env, out), out.getvalue()


def command_line(command: str, source: str, value: str) -> tuple[list[str], dict[str, str]]:
    argv = list(COMMANDS[command])
    if source == "$HERMES_HOME":
        return argv, {"HERMES_HOME": value}
    return [*argv, source, value], {}


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("command", COMMANDS)
def test_a_url_is_refused_in_one_line_without_the_value_and_nothing_is_read(nothing_is_read, command, source, value):
    argv, env = command_line(command, source, value)
    code, text = run(argv, env)
    assert code == admin.EXIT_UNREADABLE and text.splitlines() == [refusal(source)], text
    assert KEY not in text


@pytest.mark.parametrize("command", ["crl", "revoke"])
def test_a_url_in_the_environment_is_refused_next_to_an_ordinary_config_path(nothing_is_read, tmp_path, command):
    argv, env = command_line(command, "$HERMES_HOME", VALUES[0])
    code, text = run([*argv, "--config", str(tmp_path / "config.yaml")], env)
    assert code == admin.EXIT_UNREADABLE and text.splitlines() == [refusal("$HERMES_HOME")], text


@pytest.mark.parametrize("command", COMMANDS)
def test_a_url_in_the_environment_is_ignored_when_the_flag_names_the_home(tmp_path, command):
    argv, _env = command_line(command, "--hermes-home", str(tmp_path))
    code, text = run(argv, {"HERMES_HOME": VALUES[0]})
    assert code == admin.EXIT_UNREADABLE and "cannot read" in text and KEY not in text, text


@pytest.mark.parametrize("command", COMMANDS)
def test_an_ordinary_path_is_read_as_before(tmp_path, command):
    argv, env = command_line(command, "--config", str(tmp_path / "missing.yaml"))
    code, text = run(argv, env)
    assert code == admin.EXIT_UNREADABLE and "missing.yaml: cannot read (FileNotFoundError)" in text, text


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("command", COMMANDS)
def test_a_path_holding_an_at_sign_is_hidden_not_refused(command, source):
    argv, env = command_line(command, source, f"/nonexistent/u:{KEY}@h")
    code, text = run(argv, env)
    assert code == admin.EXIT_UNREADABLE and "path not shown" in text and "not a local path" not in text, text
    assert KEY not in text


def test_local_paths_returns_paths_and_leaves_the_arguments_alone():
    args = argparse.Namespace(config="/a/config.yaml", hermes_home=None, other=1)
    result = admin_common.local_paths(args, {"HERMES_HOME": "/h"})
    assert (result.config, result.hermes_home, result.other) == (Path("/a/config.yaml"), None, 1)
    assert args.config == "/a/config.yaml"  # the caller's namespace is not changed


@pytest.mark.parametrize("flag", ["config", "hermes_home"])
def test_local_paths_refuses_a_url_flag_with_the_flag_name(flag):
    args = argparse.Namespace(config=None, hermes_home=None)
    setattr(args, flag, VALUES[0])
    with pytest.raises(admin_common.ConfigFileError) as caught:
        admin_common.local_paths(args, {})
    assert str(caught.value) == refusal("--" + flag.replace("_", "-"))[len("error: ") :]
    assert KEY not in str(caught.value)


def test_local_paths_refuses_a_url_environment_value_only_when_it_is_the_home_in_use():
    with pytest.raises(admin_common.ConfigFileError):
        admin_common.local_paths(argparse.Namespace(config=None, hermes_home=None), {"HERMES_HOME": VALUES[1]})
    admin_common.local_paths(argparse.Namespace(config=None, hermes_home="/h"), {"HERMES_HOME": VALUES[1]})


def test_local_paths_ignores_the_environment_when_the_flag_is_an_empty_string():
    args = argparse.Namespace(config=None, hermes_home="")
    assert admin_common.local_paths(args, {"HERMES_HOME": VALUES[0]}).hermes_home == Path("")


@pytest.fixture
def environment(monkeypatch):
    """``set(**values)``: the real environment (``Path.home()`` reads it) and the ``env`` argument, equal."""

    def set_values(**values: str) -> dict[str, str]:
        monkeypatch.delenv("HERMES_HOME", raising=False)
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        return dict(values)

    return set_values


@pytest.mark.parametrize("hermes_home", [None, ""], ids=["unset", "empty"])
@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("command", COMMANDS)
def test_a_url_in_home_is_refused_when_it_supplies_the_default_home(
    nothing_is_read, environment, capsys, command, value, hermes_home
):
    env = environment(HOME=value, **({} if hermes_home is None else {"HERMES_HOME": hermes_home}))
    code, text = run(COMMANDS[command], env)
    assert code == admin.EXIT_UNREADABLE and text.splitlines() == [refusal("$HOME")], text
    assert KEY not in text + "".join(capsys.readouterr())


@pytest.mark.parametrize("command", COMMANDS)
def test_a_url_in_home_is_refused_next_to_an_ordinary_config_path(nothing_is_read, environment, tmp_path, command):
    env = environment(HOME=VALUES[0])
    code, text = run([*COMMANDS[command], "--config", str(tmp_path / "config.yaml")], env)
    assert code == admin.EXIT_UNREADABLE and text.splitlines() == [refusal("$HOME")], text


@pytest.mark.parametrize("by", ["flag", "environment"])
@pytest.mark.parametrize("command", COMMANDS)
def test_a_url_in_home_is_ignored_when_another_source_names_the_home(environment, tmp_path, command, by):
    env = environment(HOME=VALUES[1], **({"HERMES_HOME": str(tmp_path)} if by == "environment" else {}))
    argv = [*COMMANDS[command], *(["--hermes-home", str(tmp_path)] if by == "flag" else [])]
    code, text = run(argv, env)
    assert code == admin.EXIT_UNREADABLE and "cannot read" in text and "not a local path" not in text, text
    assert KEY not in text


@pytest.mark.parametrize("command", COMMANDS)
def test_an_ordinary_home_gives_the_default_home_as_before(environment, tmp_path, command):
    code, text = run(COMMANDS[command], environment(HOME=str(tmp_path)))
    assert code == admin.EXIT_UNREADABLE and f"{tmp_path}/.hermes/config.yaml: cannot read" in text, text


@pytest.mark.parametrize("command", COMMANDS)
def test_a_home_holding_an_at_sign_is_hidden_not_refused(environment, command):
    code, text = run(COMMANDS[command], environment(HOME=f"/nonexistent/u:{KEY}@h"))
    assert code == admin.EXIT_UNREADABLE and "path not shown" in text and "not a local path" not in text, text
    assert KEY not in text


@pytest.mark.parametrize(
    ("args", "env", "refused"),
    [
        (argparse.Namespace(config=None, hermes_home=None), {"HOME": VALUES[0]}, True),
        (argparse.Namespace(config=None, hermes_home=None), {"HOME": VALUES[0], "HERMES_HOME": ""}, True),
        (argparse.Namespace(config="/c", hermes_home=None), {"HOME": VALUES[0]}, True),
        (argparse.Namespace(config=None, hermes_home=None), {"HOME": VALUES[0], "HERMES_HOME": "/h"}, False),
        (argparse.Namespace(config=None, hermes_home="/h"), {"HOME": VALUES[0]}, False),
        (argparse.Namespace(config=None, hermes_home=""), {"HOME": VALUES[0]}, False),
        (argparse.Namespace(config=None, hermes_home=None), {"HOME": "/srv/u"}, False),
        (argparse.Namespace(config=None, hermes_home=None), {}, False),
    ],
    ids=["unset", "empty-env", "config-flag", "env-home", "flag-home", "empty-flag-home", "ordinary", "no-home"],
)
def test_local_paths_tests_home_exactly_when_it_supplies_the_default_home(args, env, refused):
    if not refused:
        admin_common.local_paths(args, env)
        return
    with pytest.raises(admin_common.ConfigFileError) as caught:
        admin_common.local_paths(args, env)
    assert str(caught.value) == refusal("$HOME")[len("error: ") :]
