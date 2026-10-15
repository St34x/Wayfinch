# SPDX-License-Identifier: Apache-2.0
"""``config check --live`` and the values of ``<home>/.env``: they are data. A ``${VAR}`` of a config file expands to
one, and Hermes returns the result as a tool or profile name or quotes it in a log line: none of it reaches stdout or
stderr (the values are masked, tool names not in Hermes' toolset table are only counted, profile names must be plain
identifiers, Hermes' log is silent). Names that reach a line cannot forge a ``config check:`` line. What a ``.env``
may make the checker do (run code, hang, be followed out of the home) is in test_config_check_env_overlay.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from glasses import admin
from glasses import hermes_env as he

from test_config_check_edges import GOOD, _live_scratch, _real_live, _scratch, live_home, run_live

KEYLIKE = "plainCanaryValue"  # a valid profile name once lower-cased, and an identifier
WITH_DIGITS = "plainCanaryValue987"
BASE64ISH = "Ab+Cd/Ef==9XyZ"  # not a valid profile name: Hermes' own log quotes it
UNSAFE = (
    "error: allow-all gate: profile default: .env is not a regular file of at most 1048576 bytes - "
    "the allow-all gate was not checked, fix or remove it"
)


def _edit(home: Path, change) -> dict:
    path = home / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(config)
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return config


def _run(home: Path, user: Path, dotenv: str, work_dotenv: str = "", **kwargs):
    (home / ".env").write_text(dotenv, encoding="utf-8")
    if work_dotenv:
        (home / "profiles" / "work" / ".env").write_text(work_dotenv, encoding="utf-8")
    return _real_live(home, user, **kwargs)


def _assert_absent(proc, *values: str) -> None:
    text = (proc.stdout + proc.stderr).lower()
    for value in values:
        assert value.lower() not in text, (value, proc.stdout, proc.stderr[-2000:])


def _dir(path: Path) -> Path:
    path.mkdir()
    return path


def _toolset_with(ref: str):
    return lambda config: config["platform_toolsets"].__setitem__("glasses", ["web", ref])


# -- real Hermes: no .env value on stdout or stderr -----------------------------------------------------------------


@pytest.mark.parametrize("canary", [KEYLIKE, WITH_DIGITS, BASE64ISH])
def test_real_live_a_dotenv_value_in_a_toolset_name_of_the_launch_home_is_not_printed(canary):
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        _edit(home, _toolset_with("${CANARY}"))
        proc = _run(home, user, f"CANARY={canary}\n")
    assert proc.returncode == admin.EXIT_ERRORS, proc.stdout + proc.stderr[-2000:]
    assert "only on glasses: <1 unknown toolset(s)>" in proc.stdout, proc.stdout
    _assert_absent(proc, canary)


@pytest.mark.parametrize("canary", [KEYLIKE, WITH_DIGITS])
def test_real_live_a_dotenv_value_in_a_routed_profile_is_not_printed(canary):
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        # the routed profile's config and its OWN .env
        work = _edit(
            home / "profiles" / "work", lambda c: c["platform_toolsets"].__setitem__("glasses", ["web", "${W}"])
        )
        assert work["platform_toolsets"]["glasses"] == ["web", "${W}"]
        proc = _run(home, user, "", f"W={canary}\n")
    assert proc.returncode == admin.EXIT_ERRORS, proc.stdout + proc.stderr[-2000:]
    assert "error: profile work: the glasses tool set differs from telegram" in proc.stdout, proc.stdout
    _assert_absent(proc, canary)


@pytest.mark.parametrize("canary", [KEYLIKE, WITH_DIGITS, BASE64ISH])
def test_real_live_a_dotenv_value_as_a_route_profile_is_not_printed_and_hermes_logs_nothing(canary):
    def route(config):
        config["gateway"]["profile_routes"][0]["profile"] = "${CANARY}"

    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        _edit(home, route)
        proc = _run(home, user, f"CANARY={canary}\n")
    _assert_absent(proc, canary)
    assert proc.stderr == "", proc.stderr  # Hermes' own "Skipping profile route ... invalid profile name" is silent
    if canary != BASE64ISH:  # a valid name: the lane is routed to a profile nobody serves
        assert proc.returncode == admin.EXIT_ERRORS and "is routed to profile" in proc.stdout, proc.stdout


def test_real_live_the_dotenv_values_of_every_served_profile_are_hidden_where_they_land_in_a_name():
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        _edit(home / "profiles" / "work", _toolset_with("${W}"))
        proc = _run(home, user, "LAUNCH=launchSecretWord\n", "W=workSecretWord\n")
    _assert_absent(proc, "launchSecretWord", "workSecretWord")


# -- unit: what is shown ----------------------------------------------------------------------------------------


def _facts(**kw):
    base = dict(
        served=frozenset({"default", "work"}),
        routes={"work": "work"},
        profiles={"default": GOOD, "work": GOOD},
        known_toolsets=frozenset({"web", "todo", "terminal"}),
    )
    return admin.HermesFacts(**{**base, **kw})


def _profile(**kw):
    return admin.ProfileFacts(**{**GOOD.__dict__, **kw})


def test_mask_values_hides_every_case_of_every_value_longest_first():
    assert he.mask_values("a SeCrEt-longer b secret c", ["secret", "secret-longer"]) == f"a {he.MASK} b {he.MASK} c"
    assert he.mask_values("nothing here", []) == "nothing here"
    assert he.secret_values({"A": "abc", "B": "abcd", "C": ""}) == {"abcd"}  # shorter values would blank words


def test_a_value_in_a_name_that_is_known_and_an_identifier_is_still_masked(tmp_path):
    odd = _profile(glasses_tools=frozenset({"web", "terminal"}), telegram_tools=frozenset({"web", "todo"}))
    facts = _facts(profiles={"default": GOOD, "work": odd}, env_values=frozenset({"terminal"}))
    code, out = run_live(live_home(tmp_path), facts)
    assert code == admin.EXIT_ERRORS and "terminal" not in out and he.MASK in out, out


def test_unknown_toolsets_are_counted_not_named(tmp_path):
    names = frozenset({"web", "x-secret-one", "y-secret-two"})
    facts = _facts(profiles={"default": GOOD, "work": _profile(glasses_tools=names)})
    _code, out = run_live(live_home(tmp_path), facts)
    assert "only on glasses: <2 unknown toolset(s)>" in out and "secret" not in out, out


def test_known_toolsets_are_listed_sorted_and_capped(tmp_path):
    many = frozenset(f"tool{chr(97 + i)}" for i in range(14))
    facts = _facts(profiles={"default": GOOD, "work": _profile(glasses_tools=many)}, known_toolsets=many | {"web"})
    _code, out = run_live(live_home(tmp_path), facts)
    assert "only on glasses: toola, toolb, toolc, toold, toole, toolf, toolg, toolh, tooli, toolj (+4 more)" in out, out


def test_without_a_known_set_no_tool_name_is_shown(tmp_path):
    facts = _facts(
        known_toolsets=frozenset(),
        profiles={"default": GOOD, "work": _profile(glasses_tools=frozenset({"web", "terminal"}))},
    )
    _code, out = run_live(live_home(tmp_path), facts)
    assert "<1 unknown toolset(s)>" in out and "terminal" not in out, out


# -- a name cannot forge a line, and is not echoed whole ----------------------------------------------------------

FORGED = "x\n\nconfig check: OK (0 error(s), 0 warning(s))\x1b[2J"


def _is_report_line(line: str) -> bool:
    return line.startswith(("error: ", "warning: ", "config check: "))


def _assert_no_forgery(out: str) -> None:
    lines = out.splitlines()
    assert all(_is_report_line(line) for line in lines), lines
    assert "\x1b" not in out and [line for line in lines if line.startswith("config check:")] == lines[-1:], lines
    assert lines[-1].startswith("config check: FAILED"), lines


def test_a_name_with_a_line_break_and_an_escape_sequence_forges_no_line(tmp_path):
    odd = _profile(glasses_tools=frozenset({FORGED}), approvals_timeout=0)
    gate = (f"profile {FORGED}: GLASSES_ALLOW_ALL_USERS is set", f"profile {FORGED!r}: x")
    facts = _facts(
        served=frozenset({"default", FORGED}),
        routes={"work": FORGED},
        profiles={"default": GOOD, FORGED: odd},
        gate_problems=gate,
    )
    code, out = run_live(live_home(tmp_path), facts)
    assert code == admin.EXIT_ERRORS
    _assert_no_forgery(out)


def test_a_very_long_name_is_not_echoed(tmp_path):
    long = "a" * 5000
    odd = _profile(glasses_tools=frozenset({long, "web"}), approvals_timeout=0)
    facts = _facts(
        served=frozenset({"default", long}),
        routes={"work": long},
        profiles={"default": GOOD, long: odd},
        gate_problems=(f"profile {long}: GLASSES_ALLOW_ALL_USERS is set",),
    )
    code, out = run_live(live_home(tmp_path), facts)
    assert code == admin.EXIT_ERRORS and long[:40] not in out and max(map(len, out.splitlines())) < 400, out


def test_a_profile_name_too_long_for_an_identifier_is_not_shown_but_a_plain_one_is(tmp_path):
    home = live_home(tmp_path)
    _code, out = run_live(home, _facts(routes={"work": "w" * 41}))
    assert "w" * 41 not in out and "routed to profile <a profile name not shown" in out, out
    _code, out = run_live(home, _facts(routes={"work": "ghost"}))
    assert "routed to profile 'ghost'" in out, out
