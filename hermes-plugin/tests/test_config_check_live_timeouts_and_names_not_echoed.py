# SPDX-License-Identifier: Apache-2.0
"""``config check --live``: a timeout that came out of a ``${VAR}`` is described, never printed as a number, and a
routed profile name equal to a ``.env`` or process-environment value is hidden only when the profile is not served.
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
    assert proc.returncode == admin.EXIT_ERRORS, proc.stdout
    assert "approvals.timeout resolves to a negative number" in proc.stdout, proc.stdout
    _assert_absent(proc, pin.lstrip("-"))


def test_real_live_a_route_to_a_served_profile_is_not_hidden_by_an_equal_dotenv_value():
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to("${WHO}"))
        proc = _run(home, user, "WHO=work\n")
    assert proc.returncode == admin.EXIT_OK and proc.stdout.startswith("config check: OK"), proc.stdout
    assert HIDDEN not in proc.stdout and proc.stderr == "", proc.stderr


@pytest.mark.parametrize("where", ["dotenv", "process"])
def test_real_live_a_route_to_an_unserved_profile_equal_to_an_environment_value_stays_an_error_with_the_name_hidden(
    where,
):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to("ghost"))
        proc = _run(
            home,
            user,
            "X=ghost\n" if where == "dotenv" else "",
            env_extra={"X": "ghost"} if where == "process" else None,
        )
    assert proc.returncode == admin.EXIT_ERRORS and HIDDEN in proc.stdout, proc.stdout
    assert "'ghost'" not in proc.stdout and "does not serve" in proc.stdout, proc.stdout


def test_real_live_a_route_to_an_unserved_profile_with_no_equal_value_names_the_profile():
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to("ghost"))
        proc = _run(home, user, "X=other\n")
    assert proc.returncode == admin.EXIT_ERRORS and "routed to profile 'ghost'" in proc.stdout, proc.stdout


@pytest.mark.parametrize("dotenv", ["EMPTY=\n", "X=gh\n", "X=hos\n"], ids=["empty value", "prefix", "inner part"])
def test_real_live_only_the_whole_name_is_compared_with_the_environment_values(dotenv):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to("ghost"))
        proc = _run(home, user, dotenv)
    assert proc.returncode == admin.EXIT_ERRORS and "routed to profile 'ghost'" in proc.stdout, proc.stdout


@pytest.mark.xfail(
    strict=True,
    reason="known gap (T1.196): a .env value padded with white space inside quotes, used as a whole route profile "
    "name, is printed stripped, because Hermes strips the name and the mask compares the raw value",
)
@pytest.mark.parametrize("value", [" padcanary9 ", "padcanary9 ", " abc"], ids=["both sides", "trailing", "short"])
def test_real_live_a_padded_dotenv_value_used_as_a_route_profile_is_not_printed(value):
    with _scratch() as tmp:
        home, user = _fresh(tmp)
        _edit(home, _route_to("${CANARY}"))
        proc = _run(home, user, f'CANARY="{value}"\n')
    assert proc.returncode == admin.EXIT_ERRORS, proc.stdout
    _assert_absent(proc, value.strip())
