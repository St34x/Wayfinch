# SPDX-License-Identifier: Apache-2.0
"""``config check --live``: no byte of its output (stdout, stderr, exit status) depends on a value of ``<home>/.env``
or of the process environment, whether config-derived text equals, contains or is padded around it, or the
``config.yaml`` holds a ``${VAR}`` reference to it. A reference is not evaluated (``glasses.hermes_refs``): the field
prints one fixed line and its checks are skipped, so a reader cannot test a guess against a secret (a tool set
compared with the ``telegram`` one, a route ``chat_id`` compared with a lane name).

Each scenario runs the same ``config.yaml`` twice, once without the canary and once with it, and the two outputs must
be byte-identical. Where the config itself holds a ``${VAR}`` the pair is "the canary vs another value of the same
shape". The pinned-Hermes runs need ``.local/venv314`` (``pytest -rs`` lists a skip); the fakes at the end need
nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from glasses import admin
from glasses import config as gcfg

from test_config_check_edges import _live_scratch, _scratch
from test_config_check_env_secrets import _edit, _facts, _profile, _run, _toolset_with
from test_config_check_live_fakes import GOOD, probes, write_home

NC = "uses an environment reference, not checked"
PROFILE_LINES = tuple(
    f"warning: profile default: {field}: {NC}"
    for field in ("platform_toolsets", "stt.provider", "stt.language", "agent.clarify_timeout", "approvals.timeout")
)
GATEWAY_LINES = (
    f"warning: gateway.profile_routes: {NC}",
    f"warning: gateway.multiplex_profiles: {NC}",
    f"error: allow-all gate: active scope: allow_all_users in config.yaml {NC}",
)


def _route(name: str):
    return lambda c: c["gateway"]["profile_routes"][0].__setitem__("profile", name)


def _kitchen(c):
    c["gateway"]["platforms"]["glasses"]["extra"]["lanes"] = ["assistant", "kitchen"]
    c["gateway"]["profile_routes"][0].update(chat_id="glasses:kitchen", profile="ghost")


def _approvals_ref(c):
    c["approvals"]["timeout"] = "${C}"


def _clarify_ref(c):
    c["agent"]["clarify_timeout"] = "${C}"


def _toolset_oracle(c):  # the tool set of glasses equals the telegram one iff C equals the guess written next to it
    c["platform_toolsets"].update(glasses=["web", "${C}"], telegram=["web", "hunter2guess"])


def _route_oracle(c):  # the route matches the lane iff C is the lane's name
    c["gateway"]["profile_routes"][0] = {"platform": "glasses", "chat_id": "glasses:${C}", "profile": "ghost"}


def _ancestor_ref(key: str):  # the whole mapping a checked key lives in is one reference
    return lambda c: c.__setitem__(key, "${C}")


def _every_profile_field(c):
    c["platform_toolsets"].update(glasses=["web", "${C}"], telegram=["web", "guess"])
    c["stt"].update(provider="${C}", language="${C}")
    _clarify_ref(c)
    _approvals_ref(c)


def _every_gateway_field(c):
    c["gateway"]["profile_routes"][0]["chat_id"] = "glasses:${C}"
    c["gateway"]["multiplex_profiles"] = "${C}"
    c["allow_all_users"] = "${C}"


# config id -> (what it edits, lines the baseline output must hold so the comparison is not vacuous, canary is a
# served / known / lane name that may be printed)
SETUPS = {
    "unserved route": (lambda h: _edit(h, _route("ghost")), ("does not serve",), False),
    "reference as route": (lambda h: _edit(h, _route("${C}")), (f"gateway.profile_routes: {NC}",), False),
    "reference inside a route": (lambda h: _edit(h, _route("pre-${C}-suf")), (f"gateway.profile_routes: {NC}",), False),
    "served route": (
        lambda h: _edit(h / "profiles" / "work", lambda c: c["stt"].pop("provider")),
        ("profile work: stt.provider is not set",),
        True,
    ),
    "toolset": (lambda h: _edit(h, _toolset_with("terminal")), ("only on glasses: terminal",), True),
    "lane key": (lambda h: _edit(h, _kitchen), ("lane 'kitchen' is routed",), True),
    "approvals timeout": (lambda h: _edit(h, _approvals_ref), ("approvals.timeout: " + NC,), False),
    "clarify timeout": (lambda h: _edit(h, _clarify_ref), ("agent.clarify_timeout: " + NC,), False),
    "mcp_servers as a reference": (
        lambda h: _edit(h, _ancestor_ref("mcp_servers")),
        (f"platform_toolsets: {NC}",),
        False,
    ),
    "clarify as a reference": (lambda h: _edit(h, _ancestor_ref("clarify")), (f"agent.clarify_timeout: {NC}",), False),
    "toolset oracle": (lambda h: _edit(h, _toolset_oracle), (f"platform_toolsets: {NC}",), False),
    "route oracle": (lambda h: _edit(h, _route_oracle), (f"gateway.profile_routes: {NC}",), False),
    "every profile field": (lambda h: _edit(h, _every_profile_field), PROFILE_LINES, True),
    "every gateway field": (lambda h: _edit(h, _every_gateway_field), GATEWAY_LINES, True),
}

# two values of the same ``${C}`` for the configs that hold one in every field kind: a guess and its miss, a
# language, numbers of both signs, a served profile name, a truthy and a falsy word, empty, padded
EVERY_FIELD_PAIRS = [
    ("hunter2guess", "otherguess1"),
    ("en", "zz"),
    ("-5", "7"),
    ("0", "12345"),
    ("work", "wxrk"),
    ("true", "nope"),
    ("", "x"),
    (" padded ", "other"),
]

# (config id, where the value is, canary, the other value of a ``${C}`` pair or None when the config holds none)
TRIALS = [
    *[("unserved route", where, "ghost", None) for where in ("dotenv", "process")],
    *[("unserved route", "dotenv", short, None) for short in ("g", "gh", "gho")],
    *[("unserved route", where, padded, None) for where in ("dotenv", "process") for padded in (" ghost ", "ghost ")],
    *[("unserved route", "dotenv", padded, None) for padded in (" ghost", " GHOST ", "   ", "GHOST")],
    ("served route", "dotenv", "work", None),
    ("served route", "process", "work", None),
    ("served route", "dotenv", " work ", None),
    ("toolset", "dotenv", "terminal", None),
    ("toolset", "process", "terminal", None),
    ("lane key", "dotenv", "kitchen", None),
    ("lane key", "process", "kitchen", None),
    *[("reference as route", w, "canarytoken1", "othertoken22") for w in ("dotenv", "process")],
    ("reference as route", "dotenv", " padcanary9 ", " qrstuvwxy8 "),
    ("reference as route", "dotenv", "padcanary9 ", "qrstuvwxy8 "),
    ("reference as route", "dotenv", " padcanary9", " qrstuvwxy8"),
    ("reference as route", "dotenv", " PADCANARY9 ", " QRSTUVWXY8 "),
    ("reference as route", "process", " padcanary9 ", " qrstuvwxy8 "),
    ("reference as route", "dotenv", " abc", " xyz"),
    ("reference as route", "dotenv", "   ", " \t "),
    *[("reference inside a route", w, "abc", "xyz") for w in ("dotenv", "process")],
    *[("reference inside a route", w, "abcdef", "ghijkl") for w in ("dotenv", "process")],
    *[("approvals timeout", w, "-98765", "-54321") for w in ("dotenv", "process")],
    *[("clarify timeout", w, "123456", "654321") for w in ("dotenv", "process")],
    # a reference in an ancestor of a checked key: Hermes fails on any non-empty string there and accepts the empty one
    *[
        (cfg, w, "", "x")
        for cfg in ("mcp_servers as a reference", "clarify as a reference")
        for w in ("dotenv", "process")
    ],
    # the two guess-equality oracles: the guess is in the config next to the reference
    *[("toolset oracle", w, "hunter2guess", "otherguess1") for w in ("dotenv", "process")],
    *[("route oracle", w, "work", "wxrk") for w in ("dotenv", "process")],
    *[(cfg, "dotenv", a, b) for cfg in ("every profile field", "every gateway field") for a, b in EVERY_FIELD_PAIRS],
    *[(cfg, "process", "hunter2guess", "otherguess1") for cfg in ("every profile field", "every gateway field")],
]
_BASELINES: dict[tuple[str, str | None], tuple[int, str, str]] = {}


def _output(config: str, where: str, value: str | None, name: str) -> tuple[int, str, str]:
    """The exit status, stdout and stderr of one real run; *value* is under *name* in the ``.env`` or the process."""
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        SETUPS[config][0](home)
        dotenv = f'{name}="{value}"\n' if value is not None and where == "dotenv" else ""
        env = {name: value} if value is not None and where == "process" else None
        proc = _run(home, user, dotenv, env_extra=env)
    return proc.returncode, proc.stdout, proc.stderr


def _baseline(config: str, other: str | None) -> tuple[int, str, str]:
    key = (config, other)
    if key not in _BASELINES:
        _BASELINES[key] = _output(config, "process", other, "C")  # no canary anywhere, or the other value of a pair
    return _BASELINES[key]


@pytest.mark.parametrize("config, where, canary, other", TRIALS, ids=[f"{t[0]}-{t[1]}-{t[2]!r}" for t in TRIALS])
def test_real_live_output_is_byte_identical_with_and_without_the_canary(config, where, canary, other):
    with_canary = _output(config, where, canary, "C" if other is not None else "CANARY")
    without = _baseline(config, other)
    assert with_canary == without, (with_canary, without)
    for line in SETUPS[config][1]:  # the scenario prints what it is meant to print
        assert line in without[1], (line, without)
    text = (with_canary[1] + with_canary[2]).lower()
    # A canary that is a served / known / lane name may be printed; one of 1-3 characters can occur in ordinary output
    # (byte-identity is the proof there), and the padded short one is pinned in
    # test_config_check_live_timeouts_and_names_not_echoed.py.
    if not SETUPS[config][2] and len(canary.strip().lstrip("-")) >= 4:
        assert canary.strip().lstrip("-").lower() not in text, with_canary


# -- fakes: nothing the facts carry changes a line ---------------------------------------------------------------


@pytest.mark.parametrize("value", ["work", "assistant", "profile", "terminal", "localhost", "stt.provider", "wss"])
def test_a_value_in_the_facts_changes_no_line_of_the_live_report(tmp_path, value):
    write_home(tmp_path)
    odd = _profile(stt_provider="", glasses_tools=frozenset({"web", "terminal"}), telegram_tools=frozenset({"web"}))
    cfg = gcfg.load({"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant", "work", "ghost"]})

    def report(values):
        facts = _facts(profiles={"default": GOOD, "work": odd}, env_values=values)
        return admin.live_problems(cfg, tmp_path, probes(lambda _c: facts))

    reports = [report(frozenset()), report(frozenset({value}))]
    assert reports[0] == reports[1] and reports[0].errors and reports[0].warnings, reports
