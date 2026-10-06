# SPDX-License-Identifier: Apache-2.0
"""T1.54: ``glasses-admin config check`` (static part). No Hermes import (conftest.py)."""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from glasses import admin

from conftest import PLUGIN_ROOT

EXAMPLE = PLUGIN_ROOT.parent / "deploy" / "examples" / "config.example.yaml"
SECRET = "s3cr3t-value-never-shown"


def run(argv, env=None):
    out = io.StringIO()
    code = admin.main(argv, env=env if env is not None else {}, out=out)
    return code, out.getvalue()


def write_config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_passes():
    code, out = run(["config", "check", "--config", str(EXAMPLE)])
    assert code == admin.EXIT_OK, out
    assert out.strip().splitlines() == ["config check: OK (0 error(s), 0 warning(s))"]


def test_planted_mistake_is_printed_with_the_glasses_prefix(tmp_path):
    text = EXAMPLE.read_text(encoding="utf-8").replace("host: 127.0.0.1", "host: 0.0.0.0")
    code, out = run(["config", "check", "--config", str(write_config(tmp_path, text))])
    assert code == admin.EXIT_ERRORS
    errors = [line for line in out.splitlines() if line.startswith("error: ")]
    assert errors == [errors[0]] and errors[0].startswith("error: glasses.host: must be a loopback IP literal")
    assert '(got "0.0.0.0")' in errors[0] and out.rstrip().endswith("config check: FAILED (1 error(s), 0 warning(s))")


def test_hermes_home_env_selects_the_default_file(tmp_path):
    write_config(tmp_path, "gateway:\n  platforms:\n    glasses:\n      enabled: true\n      extra: {port: 0}\n")
    code, out = run(["config", "check"], env={"HERMES_HOME": str(tmp_path)})
    assert code == admin.EXIT_ERRORS and "error: glasses.port: must be an integer 1-65535 (got 0)" in out


def test_hermes_home_flag_wins_over_env(tmp_path):
    write_config(tmp_path, "gateway:\n  platforms:\n    glasses:\n      enabled: true\n")
    code, out = run(["config", "check", "--hermes-home", str(tmp_path)], env={"HERMES_HOME": "/nonexistent"})
    assert code == admin.EXIT_OK, out


def test_missing_file_exit_2(tmp_path):
    code, out = run(["config", "check", "--config", str(tmp_path / "absent.yaml")])
    assert code == admin.EXIT_UNREADABLE and "cannot read (FileNotFoundError)" in out


def test_bad_yaml_reports_position_not_content(tmp_path):
    path = write_config(tmp_path, f"gateway:\n  platforms: [\n    token: {SECRET}\n  x: ]\n")
    code, out = run(["config", "check", "--config", str(path)])
    assert code == admin.EXIT_UNREADABLE and "not valid YAML at line" in out and SECRET not in out


@pytest.mark.parametrize(
    "text",
    ["", "gateway: 1\n", "gateway:\n  platforms:\n    telegram: {}\n", "gateway:\n  platforms:\n    glasses: [1]\n"],
)
def test_no_glasses_section_exit_2(tmp_path, text):
    code, out = run(["config", "check", "--config", str(write_config(tmp_path, text))])
    assert code == admin.EXIT_UNREADABLE and "no gateway.platforms.glasses mapping" in out


def test_not_enabled_is_a_warning(tmp_path):
    path = write_config(tmp_path, "gateway:\n  platforms:\n    glasses:\n      extra: {}\n")
    code, out = run(["config", "check", "--config", str(path)])
    assert code == admin.EXIT_OK
    assert "warning: gateway.platforms.glasses.enabled: not true" in out


@pytest.mark.parametrize(
    "env,name",
    [
        ({"GLASSES_ALLOW_ALL_USERS": SECRET}, "GLASSES_ALLOW_ALL_USERS"),
        ({"GLASSES_ALLOWED_USERS": "dev-1,*"}, "GLASSES_ALLOWED_USERS"),
        ({"GATEWAY_ALLOW_ALL_USERS": "true"}, "GATEWAY_ALLOW_ALL_USERS"),
    ],
)
def test_allow_all_in_the_environment_is_refused(env, name):
    code, out = run(["config", "check", "--config", str(EXAMPLE)], env=env)
    assert code == admin.EXIT_ERRORS and f"error: environment: {name}" in out and SECRET not in out


def test_allow_all_key_in_the_config_is_refused_without_echo(tmp_path):
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "        port: 8765", f"        allowed_users: '{SECRET}'\n        port: 8765"
    )
    code, out = run(["config", "check", "--config", str(write_config(tmp_path, text))])
    assert code == admin.EXIT_ERRORS and "error: glasses.allowed_users: not allowed" in out and SECRET not in out


def test_usage_errors_exit_2():
    with pytest.raises(SystemExit) as exc:
        admin.main(["config"], env={}, out=io.StringIO())
    assert exc.value.code == 2


def test_runs_as_a_module():
    proc = subprocess.run(
        [sys.executable, "-m", "glasses.admin", "config", "check", "--config", str(EXAMPLE)],
        cwd=PLUGIN_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "config check: OK" in proc.stdout
