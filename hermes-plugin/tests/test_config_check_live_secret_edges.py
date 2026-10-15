# SPDX-License-Identifier: Apache-2.0
"""``config check --live`` against real Hermes: edges of what a ``.env`` value, a config-supplied name, an
installed-but-unrouted profile and a read-only home may do to the output and to the home.

A ``.env`` value of fewer than four characters is not masked as text (it would blank ordinary words): it is hidden
where it is the whole routed profile name, and a timeout number is never printed. Inside a longer name it is not.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from glasses import admin

from test_config_check_edges import _live_scratch, _real_live, _scratch
from test_config_check_env_secrets import _assert_absent, _edit, _run

CANARY = "plainCanaryValue987"
UNROUTED = pytest.mark.parametrize(
    "gateway", [{}, {"standalone": True}, {"parked": True}], ids=["installed", "standalone", "parked"]
)


def _route_to(ref: str):
    def edit(config):
        config["gateway"]["profile_routes"][0]["profile"] = ref

    return edit


def _telegram_list(ref: str):
    return lambda config: config["platform_toolsets"].__setitem__("telegram", ["web", ref])


def _fresh(tmp: str) -> tuple[Path, Path]:
    home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
    user.mkdir()
    return home, user


# -- a .env value in the other places a name can come from ---------------------------------------------------------


def test_real_live_a_dotenv_value_in_the_telegram_tool_list_is_not_printed():
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _telegram_list("${CANARY}"))
        proc = _run(home, user, f"CANARY={CANARY}\n")
    assert proc.returncode == admin.EXIT_ERRORS and "missing on glasses: " in proc.stdout, proc.stdout
    _assert_absent(proc, CANARY)


@pytest.mark.parametrize("ref", ["${env:CANARY}", "pre-${CANARY}-suffix", "${CANARY}${CANARY}"])
def test_real_live_other_reference_forms_of_a_dotenv_value_in_a_route_are_not_printed(ref):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to(ref))
        proc = _run(home, user, f"CANARY={CANARY}\n")
    assert "is routed to profile" in proc.stdout and proc.stderr == "", (proc.stdout, proc.stderr)
    _assert_absent(proc, CANARY)


def test_real_live_a_dotenv_value_that_is_a_plain_word_blanks_that_word_but_not_the_verdict():
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        proc = _run(home, user, "GATEWAY_ALLOW_ALL_USERS=true\nWORD=profile\nLEVEL=error\nSTATE=FAILED\n")
    lines = proc.stdout.splitlines()
    assert proc.returncode == admin.EXIT_ERRORS, proc.stdout
    assert lines[-1] == "config check: FAILED (2 error(s), 0 warning(s))", lines
    assert lines[0].startswith("error: allow-all gate: active scope: "), lines
    assert lines[1].startswith("error: allow-all gate: <.env value> default: "), lines


@pytest.mark.parametrize("secret", ["abc", "Zq9"])
def test_real_live_a_short_dotenv_value_as_a_route_profile_is_not_printed(secret):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to("${CANARY}"))
        proc = _run(home, user, f"CANARY={secret}\n")
    _assert_absent(proc, secret)


@pytest.mark.parametrize("secret", ["987", "00123456", "+123456"])
def test_real_live_a_numeric_dotenv_value_resolved_as_a_timeout_is_not_printed(secret):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, lambda c: c["agent"].__setitem__("clarify_timeout", "${PIN}"))
        proc = _run(home, user, f"PIN={secret}\n")
    assert "clarify timeout resolves to" in proc.stdout, proc.stdout
    _assert_absent(proc, secret.lstrip("+0"))


def test_real_live_a_process_environment_value_as_a_route_profile_is_not_printed():
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to("${PROCSECRET}"))
        proc = _real_live(home, user, env_extra={"PROCSECRET": "procSecretValue12"})
    _assert_absent(proc, "procSecretValue12")


# -- allow-all grants Hermes does not count, and profiles the gateway does not route --------------------------------


@pytest.mark.parametrize("value", ["on", " yes ", "enabled", "2"])
def test_real_live_a_value_hermes_does_not_bridge_is_not_a_grant(value):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, lambda c: c.__setitem__("allow_all_users", value))
        proc = _real_live(home, user)
    assert proc.returncode == admin.EXIT_OK, proc.stdout


def test_real_live_an_empty_top_level_key_falls_back_to_the_gateway_one():
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, lambda c: c.update({"allow_all_users": None, "gateway": {**c["gateway"], "allow_all_users": True}}))
        proc = _real_live(home, user)
    assert proc.returncode == admin.EXIT_ERRORS and proc.stdout.count("error: allow-all gate: ") == 2, proc.stdout


def test_real_live_a_grant_read_through_a_dotenv_reference_counts():
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, lambda c: c.__setitem__("allow_all_users", "${GRANT}"))
        proc = _run(home, user, "GRANT=true\n")
    assert proc.returncode == admin.EXIT_ERRORS and proc.stdout.count("error: allow-all gate: ") == 2, proc.stdout


def test_real_live_a_grant_only_in_a_routed_profile_config_is_not_the_gateways_grant():
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home / "profiles" / "work", lambda c: c.__setitem__("allow_all_users", True))
        proc = _real_live(home, user)
    assert proc.returncode == admin.EXIT_OK, proc.stdout


def _unrouted_profile(home: Path, **gateway) -> Path:
    other = home / "profiles" / "other"
    other.mkdir()
    (other / "config.yaml").write_text(yaml.safe_dump({"gateway": gateway} if gateway else {"agent": {}}))
    return other


@UNROUTED
def test_real_live_an_allow_all_dotenv_of_an_unrouted_installed_profile_is_still_an_error(gateway):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        (_unrouted_profile(home, **gateway) / ".env").write_text("GATEWAY_ALLOW_ALL_USERS=true\n")
        proc = _real_live(home, user)
    assert proc.returncode == admin.EXIT_ERRORS, proc.stdout
    assert "error: allow-all gate: profile other: GATEWAY_ALLOW_ALL_USERS is enabled" in proc.stdout, proc.stdout


@UNROUTED
def test_real_live_a_fifo_dotenv_of_an_unrouted_installed_profile_is_one_error_not_a_hang(gateway):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        os.mkfifo(_unrouted_profile(home, **gateway) / ".env")
        started = time.monotonic()
        proc = _real_live(home, user, timeout=90)
        elapsed = time.monotonic() - started
    assert proc.returncode == admin.EXIT_ERRORS and elapsed < 60, (elapsed, proc.stdout)
    errors = [line for line in proc.stdout.splitlines() if line.startswith("error: ")]
    assert len(errors) == 1 and "profile other: .env is not a regular file" in errors[0], proc.stdout


# -- a home that cannot be written, plugins inside the checked home ----------------------------------------------


def test_real_live_runs_on_a_home_nothing_can_be_written_to():
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        (home / ".env").write_text(f"CANARY={CANARY}\n")
        everything = [home, *home.rglob("*")]
        try:
            for path in everything:
                path.chmod(0o555 if path.is_dir() else 0o444)
            proc = _real_live(home, user)
        finally:
            for path in everything:
                path.chmod(0o755 if path.is_dir() else 0o644)
    assert proc.returncode == admin.EXIT_OK and proc.stderr == "", (proc.stdout, proc.stderr[-2000:])
    assert proc.stdout == "config check: OK (0 error(s), 0 warning(s))\n"


def _marker_plugin(root: Path, marker: Path) -> Path:
    plugin = root / "boom"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: boom\nversion: 1.0.0\nkind: backend\ndescription: probe\n")
    (plugin / "__init__.py").write_text(f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n")
    return plugin


def test_real_live_a_plugin_installed_under_the_checked_home_is_never_run(tmp_path):
    marker, control_marker = tmp_path / "ran-in-home", tmp_path / "ran-from-bundled-dir"
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _marker_plugin(home / "plugins", marker)
        _marker_plugin(home / "profiles" / "work" / "plugins", marker)
        _edit(home, lambda c: c.__setitem__("plugins", {"enabled": ["boom"]}))
        proc = _real_live(home, user)
        # control: the planted plugin is real, a plugin directory named by the process environment does run
        bundled = Path(tmp) / "bundled"
        _marker_plugin(bundled, control_marker)
        control = _real_live(home, user, env_extra={"HERMES_BUNDLED_PLUGINS": str(bundled)})
    assert proc.returncode == admin.EXIT_OK and not marker.exists(), (proc.stdout, proc.stderr[-2000:])
    assert control.returncode == admin.EXIT_OK and control_marker.exists(), control.stdout + control.stderr[-2000:]
    assert not marker.exists()
