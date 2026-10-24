# SPDX-License-Identifier: Apache-2.0
"""URL-shaped path arguments of ``config check``, ``crl`` and ``revoke`` at the edges: the real CLI as a subprocess
(argv and ``os.environ`` wiring, stdout and stderr), argparse spellings of the flags, odd characters around the URL,
ordinary paths that merely look similar (``//``, ``:/``), and the order of the checks (a bad device id first)."""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
from pathlib import Path

import pytest

from glasses import admin

from conftest import PLUGIN_ROOT

KEY = "KEY-must-not-show-4c1e"
URL = f"scheme://{KEY}"
DEVICE_ID = "0123456789abcdef"
COMMANDS = {"config check": ["config", "check"], "crl": ["crl"], "revoke": ["revoke", DEVICE_ID]}
REFUSAL = "not a local path (it contains '://'; the value is not shown)"


def run(argv: list[str], env: dict[str, str]) -> tuple[int, list[str]]:
    out = io.StringIO()
    return admin.main(argv, env, out), out.getvalue().splitlines()


def cli(argv: list[str], home, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """``python -m glasses.admin ARGV`` as an operator runs it: a minimal environment, the real ``os.environ``."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "glasses.admin", *argv],
        cwd=PLUGIN_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("command", COMMANDS)
def test_the_real_cli_refuses_a_url_flag_on_both_streams(tmp_path, command):
    proc = cli([*COMMANDS[command], "--config", URL], tmp_path)
    assert proc.returncode == 2
    assert proc.stdout.splitlines() == [f"error: --config: {REFUSAL}"] and proc.stderr == ""
    assert KEY not in proc.stdout + proc.stderr


@pytest.mark.parametrize("command", COMMANDS)
def test_the_real_cli_refuses_a_url_in_the_process_environment(tmp_path, command):
    proc = cli(COMMANDS[command], tmp_path, {"HERMES_HOME": URL})
    assert proc.returncode == 2
    assert proc.stdout.splitlines() == [f"error: $HERMES_HOME: {REFUSAL}"] and proc.stderr == ""
    assert KEY not in proc.stdout + proc.stderr


@pytest.mark.parametrize("command", COMMANDS)
def test_the_real_cli_reads_an_ordinary_home_from_the_process_environment(tmp_path, command):
    proc = cli(COMMANDS[command], tmp_path, {"HERMES_HOME": str(tmp_path)})
    assert proc.returncode == 2 and "config.yaml: cannot read (FileNotFoundError)" in proc.stdout, proc.stdout
    assert "not a local path" not in proc.stdout and proc.stderr == ""


@pytest.mark.parametrize("flag", ["--config=", "--hermes-home=", "--conf=", "--hermes-h="])
@pytest.mark.parametrize("command", COMMANDS)
def test_argparse_spellings_of_the_flags_are_refused_too(command, flag):
    code, lines = run([*COMMANDS[command], flag + URL], {})
    assert code == 2 and len(lines) == 1 and REFUSAL in lines[0] and KEY not in lines[0], lines


@pytest.mark.parametrize("command", COMMANDS)
def test_the_last_occurrence_of_a_repeated_flag_decides(tmp_path, command):
    ordinary = str(tmp_path / "missing.yaml")
    code, lines = run([*COMMANDS[command], "--config", ordinary, "--config", URL], {})
    assert code == 2 and lines == [f"error: --config: {REFUSAL}"]
    code, lines = run([*COMMANDS[command], "--config", URL, "--config", ordinary], {})
    assert code == 2 and lines == [f"error: {ordinary}: cannot read (FileNotFoundError)"]


@pytest.mark.parametrize("value", [f"a\n{URL}", f" {URL}", f"{URL}\n", f"/ok/{URL}", f"{URL}/../x", f"{URL}\x00"])
@pytest.mark.parametrize("command", COMMANDS)
def test_a_url_surrounded_by_other_characters_is_still_one_refusal_line(command, value):
    code, lines = run([*COMMANDS[command], "--hermes-home", value], {})
    assert code == 2 and lines == [f"error: --hermes-home: {REFUSAL}"], lines


@pytest.mark.parametrize(
    "path", ["/nonexistent//dir/config.yaml", "/nonexistent/a:/config.yaml", "/nonexistent/a:b/c.yaml"]
)
@pytest.mark.parametrize("command", COMMANDS)
def test_a_path_with_a_double_slash_or_a_colon_is_not_taken_for_a_url(command, path):
    code, lines = run([*COMMANDS[command], "--config", path], {})
    assert code == 2 and lines == [f"error: {Path(path)}: cannot read (FileNotFoundError)"], lines  # Path folds '//'


def test_revoke_checks_the_device_id_before_the_path_arguments():
    code, lines = run(["revoke", "not-an-id", "--config", URL], {"HERMES_HOME": URL})
    assert code == 2 and lines == ["error: revoke: not a device id (16 lower-case hex)"]


@pytest.mark.parametrize("command", COMMANDS)
def test_a_url_flag_wins_the_message_over_a_url_in_the_environment(command):
    code, lines = run([*COMMANDS[command], "--config", URL], {"HERMES_HOME": f"other://{KEY}"})
    assert code == 2 and lines == [f"error: --config: {REFUSAL}"], lines


@pytest.mark.parametrize("source", ["--hermes-home", "$HERMES_HOME"])
@pytest.mark.parametrize("command", COMMANDS)
def test_a_home_holding_an_at_sign_is_hidden_not_refused(command, source):
    home = f"/nonexistent/u:{KEY}@h"
    argv = [*COMMANDS[command], source, home] if source == "--hermes-home" else COMMANDS[command]
    code, lines = run(argv, {"HERMES_HOME": home} if source == "$HERMES_HOME" else {})
    assert code == 2 and lines == [
        "error: <path not shown: it contains '@' or '://'>: cannot read (FileNotFoundError)"
    ], lines


def leaf_commands_with_a_config_flag() -> set[str]:
    """Names of the registered leaf subcommands that declare ``--config`` (walks the parser, no command list)."""
    found: set[str] = set()

    def walk(parser: argparse.ArgumentParser, prefix: str) -> None:
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        for action in subs:
            for name, sub in action.choices.items():
                walk(sub, f"{prefix} {name}".strip())
        if not subs and any("--config" in a.option_strings for a in parser._actions):
            found.add(prefix)

    walk(admin.build_parser(), "")
    return found


def test_every_subcommand_with_a_config_flag_is_covered_by_the_url_tests():
    assert leaf_commands_with_a_config_flag() == set(COMMANDS), "a new command with --config needs its URL cases"
