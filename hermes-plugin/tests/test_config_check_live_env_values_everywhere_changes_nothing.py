# SPDX-License-Identifier: Apache-2.0
"""``config check --live``: places where a value of a ``.env`` or of the process environment used to be masked or
compared (the profile's own ``.env``, the lane key, the host, the state dir, a ``.env`` line written in another form)
still change no byte of the output, and a route name that is not a plain identifier is never printed.

The pinned-Hermes runs need ``.local/venv314`` (``pytest -rs`` lists a skip).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_config_check_edges import _live_scratch, _scratch
from test_config_check_env_secrets import _edit, _run


def _output(setup, dotenv: str = "", work_dotenv: str = "", env_extra=None) -> tuple[int, str, str]:
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        setup(home)
        proc = _run(home, user, dotenv, work_dotenv, env_extra=env_extra)
    return proc.returncode, proc.stdout, proc.stderr


def _route(name: str):
    return lambda home: _edit(home, lambda c: c["gateway"]["profile_routes"][0].__setitem__("profile", name))


def _served_with_a_tool_difference(home: Path) -> None:
    _edit(home / "profiles" / "work", lambda c: c["platform_toolsets"].__setitem__("glasses", ["web", "terminal"]))


def _public_url_ref(home: Path) -> None:
    def edit(config):
        config["gateway"]["platforms"]["glasses"]["extra"]["public_url"] = "wss://${C}.localhost/ws"

    _edit(home, edit)


# (what the scenario edits, a text its baseline must print so the comparison is not vacuous)
SCENARIOS = {
    "unserved route": (_route("ghost"), "lane 'work' is routed to profile <a profile name not shown"),
    "served route": (_served_with_a_tool_difference, "only on glasses: terminal"),
}
# words the output prints of the plugin's config, of the scratch home and of Hermes' tables
PRINTED = ["work", "default", "terminal", "localhost", "assistant", "stt.provider", "profile", "error", "state", "web"]


@pytest.mark.parametrize("value", PRINTED)
@pytest.mark.parametrize("where", ["launch dotenv", "profile dotenv", "process"])
@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_real_live_a_value_equal_to_a_printed_word_changes_nothing(scenario, where, value):
    setup, expected = SCENARIOS[scenario]
    quiet = _output(setup)
    held = _output(
        setup,
        f"V={value}\n" if where == "launch dotenv" else "",
        f"V={value}\n" if where == "profile dotenv" else "",
        {"V": value} if where == "process" else None,
    )
    assert held == quiet, (held, quiet)
    assert expected in quiet[1] and quiet[0] == 1, quiet


@pytest.mark.parametrize(
    "line",
    ["export V=ghostword", "V='ghostword'", 'V="ghostword"', "V=ghostword # note", "V=ghostword\r"],
    ids=["export", "single quotes", "double quotes", "comment", "carriage return"],
)
def test_real_live_a_dotenv_line_in_any_form_changes_nothing(line):
    assert _output(_route("ghostword"), line + "\n") == _output(_route("ghostword"))


def test_real_live_a_reference_inside_a_route_name_prints_the_same_for_any_value_of_the_same_shape():
    other = _output(_route("pre-${C}-suf"), "C=xyz\n")
    assert _output(_route("pre-${C}-suf"), "C=abc\n") == other
    assert _output(_route("pre-${C}-suf"), env_extra={"C": "abc"}) == other
    assert _output(_route("pre-${C}-suf")) == other  # not set: the reference is not looked at either
    assert "abc" not in other[1] and "gateway.profile_routes: uses an environment reference, not checked" in other[1], (
        other
    )


@pytest.mark.parametrize("value", ["canary9", "other99"])
def test_real_live_a_reference_in_the_plugin_config_is_not_expanded_or_masked(value):
    first = _output(_public_url_ref, f"C={value}\n")
    second = _output(_public_url_ref, "C=zzzzzzz\n")
    assert first == second, (first, second)
    assert value not in first[1] + first[2]


@pytest.mark.parametrize(
    "name",
    ["a\nerror: forged", "x" * 200, "https://user:pw@host/x", "ghost\x1b[31m", "Ünï", "a b", "-lead"],
    ids=["line break", "long", "url with credentials", "escape", "non ascii", "space", "leading dash"],
)
def test_real_live_a_route_name_that_is_not_a_plain_identifier_is_never_printed(name):
    code, out, err = _output(_route(name))
    assert err == "" and "\x1b" not in out, (out, err)
    assert all(line.startswith(("error: ", "warning: ", "config check: ")) for line in out.splitlines()), out
    for fragment in ("forged", "x" * 20, "user:pw", "Ünï", "-lead", "a b"):
        if fragment in name:
            assert fragment not in out, out
