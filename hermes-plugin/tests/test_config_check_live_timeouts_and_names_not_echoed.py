# SPDX-License-Identifier: Apache-2.0
"""``config check --live``: a timeout the fakes carry is described, never printed as a number; a ``${VAR}`` in a
timeout or a route is not evaluated (one fixed line, no value printed); a route to a profile this gateway does not
serve is shown as a fixed placeholder whatever its name is; a served profile is always named. The identity of the
output with and without an environment value is in test_config_check_live_output_independent_of_env.py.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from glasses import admin

from test_config_check_edges import _live_scratch, _scratch
from test_config_check_env_secrets import _assert_absent, _edit, _run
from test_config_check_live_fakes import GOOD

HIDDEN = "<a profile name not shown"


def _fresh(tmp: str) -> tuple[Path, Path]:
    home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
    user.mkdir()
    return home, user


def _set(section: str, key: str, ref: str):
    return lambda config: config[section].__setitem__(key, ref)


def _process(where: str, value: str):
    return {"CANARY": value} if where == "process" else None


def _route_to(name: str):
    return lambda config: config["gateway"]["profile_routes"][0].__setitem__("profile", name)


# -- the line itself (fakes) --------------------------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [-1, -987, -3600])
def test_a_negative_approvals_timeout_is_described_not_printed(seconds):
    line = admin.profile_problems("work", replace(GOOD, approvals_timeout=seconds), []).errors[0]
    assert "approvals.timeout resolves to a negative number" in line and str(abs(seconds)) not in line


def test_a_zero_approvals_timeout_is_printed_as_zero():
    line = admin.profile_problems("work", replace(GOOD, approvals_timeout=0), []).errors[0]
    assert "approvals.timeout resolves to 0 " in line


@pytest.mark.parametrize("seconds", [7, 987, 123456])
def test_a_positive_clarify_timeout_is_described_not_printed(seconds):
    line = admin.profile_problems("work", replace(GOOD, clarify_timeout=seconds), []).errors[0]
    assert "clarify timeout resolves to a positive number of seconds" in line and str(seconds) not in line


# -- real Hermes ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("pin", ["-987", "-123456"])
def test_real_live_a_negative_dotenv_number_as_approvals_timeout_is_not_printed(pin):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _set("approvals", "timeout", "${PIN}"))
        proc = _run(home, user, f'PIN="{pin}"\n')
    assert proc.returncode == admin.EXIT_OK, proc.stdout
    assert "profile default: approvals.timeout: uses an environment reference, not checked" in proc.stdout, proc.stdout
    _assert_absent(proc, pin.lstrip("-"))


def test_real_live_a_route_reference_is_not_evaluated_whatever_its_value_is():
    def run(who: str):
        with _scratch() as tmp:
            home, user = _fresh(tmp)
            _edit(home, _route_to("${WHO}"))
            return _run(home, user, f"WHO={who}\n")

    proc, other = run("work"), run("ghost")
    assert (proc.returncode, proc.stdout, proc.stderr) == (other.returncode, other.stdout, other.stderr)
    assert (
        proc.returncode == admin.EXIT_OK
        and "warning: gateway.profile_routes: uses an environment reference, not checked" in proc.stdout
    ), proc.stdout
    assert HIDDEN not in proc.stdout and "work" not in proc.stdout and proc.stderr == "", proc.stdout


@pytest.mark.parametrize("where", ["dotenv", "process", "none"])
def test_real_live_a_route_to_an_unserved_profile_stays_an_error_with_the_name_hidden_equal_to_a_value_or_not(where):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to("ghost"))
        proc = _run(
            home,
            user,
            "X=ghost\n" if where == "dotenv" else "X=other\n" if where == "none" else "",
            env_extra={"X": "ghost"} if where == "process" else None,
        )
    assert proc.returncode == admin.EXIT_ERRORS and HIDDEN in proc.stdout, proc.stdout
    assert "'ghost'" not in proc.stdout and "does not serve" in proc.stdout, proc.stdout


@pytest.mark.parametrize("name", ["ghost", "ghost2", "a-typo", "work2"])
def test_real_live_every_unserved_route_name_prints_the_same_placeholder(name):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to(name))
        proc = _run(home, user, "EMPTY=\n")
    assert proc.returncode == admin.EXIT_ERRORS and "lane 'work' is routed to profile " + HIDDEN in proc.stdout
    assert f"'{name}'" not in proc.stdout and "does not serve" in proc.stdout, proc.stdout


@pytest.mark.parametrize(
    "value, where",
    [
        (" padcanary9 ", "dotenv"),
        ("padcanary9 ", "dotenv"),
        (" padcanary9", "dotenv"),
        (" PADCANARY9 ", "dotenv"),
        (" abc", "dotenv"),
        (" padcanary9 ", "process"),
    ],
    ids=["both sides", "trailing", "leading", "upper case", "short", "process environment"],
)
def test_real_live_a_padded_value_used_as_a_route_profile_is_not_printed(value, where):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to("${CANARY}"))
        proc = _run(home, user, f'CANARY="{value}"\n' if where == "dotenv" else "", env_extra=_process(where, value))
    assert (
        proc.returncode == admin.EXIT_OK
        and "gateway.profile_routes: uses an environment reference, not checked" in proc.stdout
    ), proc.stdout
    _assert_absent(proc, value.strip())


@pytest.mark.parametrize("where", ["dotenv", "process"])
def test_real_live_a_white_space_only_value_used_as_a_route_profile_changes_nothing(where):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to("${CANARY}"))
        proc = _run(home, user, "")  # the reference is not set: it stays literal
        padded = _run(home, user, 'CANARY="   "\n' if where == "dotenv" else "", env_extra=_process(where, "   "))
    assert padded.returncode == proc.returncode == admin.EXIT_OK, padded.stdout  # Hermes skips an empty route name
    assert padded.stdout == proc.stdout and padded.stderr == "", padded.stdout
