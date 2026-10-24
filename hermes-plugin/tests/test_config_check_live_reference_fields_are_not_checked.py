# SPDX-License-Identifier: Apache-2.0
"""``config check --live`` on the pinned Hermes: every raw ``config.yaml`` key the check reads for a field makes the
field "not checked" when the key's text holds ``${``, in the launch home and in a routed profile, at the top level
and under ``gateway:``. One run per value of the variable, two values chosen so that evaluating the field would
change the output; the two outputs must be byte-identical, hold the one fixed line, and a text that is not a
reference (a comment, a mapping key, ``$C``, a field the check does not read) must leave the check running. The
pure parts of the rule are in test_config_check_env_references.py, the scenario matrix in
test_config_check_live_output_independent_of_env.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from glasses import admin

from test_config_check_edges import _live_scratch, _scratch
from test_config_check_env_secrets import _edit, _run

NC = "uses an environment reference, not checked"
ALLOW_ALL = f"error: allow-all gate: active scope: allow_all_users in config.yaml {NC}"
WORK = "profiles/work/config.yaml"


def _work(change):
    def apply(home: Path) -> None:
        _edit(home / "profiles" / "work", change)

    return apply


def _launch(change):
    return lambda home: _edit(home, change)


def _hints_de(config) -> None:  # a language of "en" would then be an error, so evaluating stt.language shows
    config["gateway"]["platforms"]["glasses"]["extra"]["meeting"] = {"language_hints": ["de"]}


def _move_up(key: str):
    def change(config) -> None:
        config[key] = config["gateway"].pop(key)

    return change


def _set(path: str, value):
    keys = path.split(".")

    def change(config) -> None:
        node = config
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value

    return change


def _chain(*changes):
    return lambda config: [change(config) for change in changes]


REF = "${C}"
# id -> (edit of the scratch home, line the output must hold, exit status, the two values of C)
REFERENCES = {
    "clarify.timeout": (
        _work(_set("clarify.timeout", REF)),
        f"warning: profile work: agent.clarify_timeout: {NC}",
        0,
        ("30", "0"),
    ),
    "known_plugin_toolsets": (
        _work(_set("known_plugin_toolsets", {"glasses": [REF]})),
        f"warning: profile work: platform_toolsets: {NC}",
        0,
        ("terminal", "web"),
    ),
    "known_builtin_toolsets": (
        _work(_set("known_builtin_toolsets", {"glasses": [REF]})),
        f"warning: profile work: platform_toolsets: {NC}",
        0,
        ("terminal", "web"),
    ),
    "legacy toolsets": (
        _work(_set("toolsets", [REF])),
        f"warning: profile work: platform_toolsets: {NC}",
        0,
        ("kanban", "web"),
    ),
    "context.engine": (
        _work(_set("context.engine", REF)),
        f"warning: profile work: platform_toolsets: {NC}",
        0,
        ("lcm", "compressor"),
    ),
    "agent.disabled_toolsets": (
        _work(_set("agent.disabled_toolsets", REF)),
        f"warning: profile work: platform_toolsets: {NC}",
        0,
        ("web", "nothing-disabled"),
    ),
    "mcp_servers enabled": (
        _work(_set("mcp_servers", {"m": {"enabled": REF}})),
        f"warning: profile work: platform_toolsets: {NC}",
        0,
        ("false", "true"),
    ),
    "stt.<provider>.language": (
        _work(_chain(_set("stt.local.language", REF), _set("stt.language", ""))),
        f"warning: profile work: stt.language: {NC}",
        0,
        ("en", "de"),
    ),
    "stt.<provider>.language_code": (
        _work(_chain(_set("stt.provider", "elevenlabs"), _set("stt.elevenlabs.language_code", REF))),
        f"warning: profile work: stt.language: {NC}",
        0,
        ("en", "de"),
    ),
    "stt.provider": (_work(_set("stt.provider", REF)), f"warning: profile work: stt.provider: {NC}", 0, ("local", "")),
    "top-level profile_routes": (
        _launch(_chain(_move_up("profile_routes"), lambda c: c["profile_routes"][0].update(chat_id="glasses:" + REF))),
        f"warning: gateway.profile_routes: {NC}",
        0,
        ("work", "wxrk"),
    ),
    "top-level multiplex_profiles": (
        _launch(_chain(_move_up("multiplex_profiles"), _set("multiplex_profiles", REF))),
        f"warning: gateway.multiplex_profiles: {NC}",
        0,
        ("true", "false"),
    ),
    "gateway.allow_all_users": (_launch(_set("gateway.allow_all_users", REF)), ALLOW_ALL, 1, ("true", "false")),
    "allow_all_users false at the top, a reference under gateway": (
        _launch(_chain(_set("allow_all_users", False), _set("gateway.allow_all_users", REF))),
        ALLOW_ALL,
        1,
        ("true", "false"),
    ),
}
NOT_REFERENCES = {
    "a key": _launch(_set("platform_toolsets", {"glasses": ["web"], "telegram": ["web"], REF: ["web"]})),
    "a dollar without a brace": _launch(_set("platform_toolsets", {"glasses": ["$C"], "telegram": ["$C"]})),
    "a field the check does not read": _launch(
        _set("model", {"api_key": REF, "base_url": f"https://{REF}.example.com"})
    ),
    "a comment": lambda home: (home / "config.yaml").open("a", encoding="utf-8").write(f"# approvals: {REF}\n"),
}


def _output(setup, value: str) -> tuple[int, str, str]:
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        _edit(home, _hints_de)
        setup(home)
        proc = _run(home, user, f'C="{value}"\n')
    return proc.returncode, proc.stdout, proc.stderr


@pytest.mark.parametrize("name", REFERENCES)
def test_real_live_a_key_the_check_reads_holding_a_reference_is_not_checked_and_prints_one_fixed_line(name):
    setup, line, code, (first, second) = REFERENCES[name]
    one, other = _output(setup, first), _output(setup, second)
    assert one == other, (one, other)
    assert one[0] == code and one[2] == "", one
    assert sum(shown.startswith(line) for shown in one[1].splitlines()) == 1, one
    assert f"config check: {'FAILED' if code else 'OK'}" in one[1], one


@pytest.mark.parametrize("name", NOT_REFERENCES)
def test_real_live_text_that_is_not_a_reference_leaves_the_check_running(name):
    one, other = _output(NOT_REFERENCES[name], "canaryvalue1"), _output(NOT_REFERENCES[name], "canaryvalue2")
    assert one == other and one[0] == admin.EXIT_OK, (one, other)
    assert "not checked" not in one[1] and "config check: OK (0 error(s), 0 warning(s))" in one[1], one
