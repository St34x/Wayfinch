# SPDX-License-Identifier: Apache-2.0
"""``glasses.hermes_refs``: which fields ``config check --live`` does not evaluate because ``config.yaml`` writes an
environment reference in them, decided from the raw text only, and what the report prints for them. The identity of
the real output for two values of a reference is in test_config_check_live_output_independent_of_env.py."""

from __future__ import annotations

import pytest

from glasses import admin
from glasses import hermes_refs as refs

from test_config_check_edges import live_home, run_live
from test_config_check_env_secrets import _facts, _profile

NC = "uses an environment reference, not checked"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("${C}", True),
        ("${env:C}", True),
        ("pre-${C}-suf", True),
        ("${C:-x}", True),  # Hermes keeps it literal; a superset is the safe side
        ("${", True),
        ("$C", False),
        ("{C}", False),
        ("plain", False),
        (7, False),
        (None, False),
        (["a", ["b", "${C}"]], True),
        ({"k": {"deeper": "${C}"}}, True),
        ({"${C}": "value"}, False),  # keys are never expanded
        (("x", "y"), False),
    ],
)
def test_a_value_is_a_reference_when_a_string_in_it_holds_the_opening(value, expected):
    assert refs.has_env_ref(value) is expected


@pytest.mark.parametrize(
    "config, field",
    [
        ({"platform_toolsets": {"glasses": ["web", "${C}"]}}, refs.TOOLSETS),
        ({"platform_toolsets": {"telegram": "${C}"}}, refs.TOOLSETS),
        ({"known_plugin_toolsets": {"glasses": ["${C}"]}}, refs.TOOLSETS),
        ({"known_builtin_toolsets": {"glasses": ["${C}"]}}, refs.TOOLSETS),
        ({"toolsets": ["${C}"]}, refs.TOOLSETS),
        ({"context": {"engine": "${C}"}}, refs.TOOLSETS),
        ({"agent": {"disabled_toolsets": "${C}"}}, refs.TOOLSETS),
        ({"mcp_servers": {"one": {"enabled": "${C}"}}}, refs.TOOLSETS),
        ({"mcp_servers": "${C}"}, refs.TOOLSETS),  # a reference in an ancestor of the key is a reference to it
        ({"stt": {"provider": "${C}"}}, refs.STT_PROVIDER),
        ({"stt": {"provider": "${C}"}}, refs.STT_LANGUAGE),  # the provider picks the language key
        ({"stt": {"language": "${C}"}}, refs.STT_LANGUAGE),
        ({"stt": {"local": {"language": "${C}"}}}, refs.STT_LANGUAGE),
        ({"stt": {"elevenlabs": {"language_code": "${C}"}}}, refs.STT_LANGUAGE),
        ({"clarify": {"timeout": "${C}"}}, refs.CLARIFY),
        ({"clarify": "${C}"}, refs.CLARIFY),
        ({"agent": {"clarify_timeout": "${C}"}}, refs.CLARIFY),
        ({"approvals": {"timeout": "${C}"}}, refs.APPROVALS),
    ],
)
def test_every_key_the_check_reads_for_a_field_makes_it_unchecked(config, field):
    assert field in refs.profile_unchecked(config)


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"mcp_servers": {"one": {"url": "${C}", "env": {"TOKEN": "${C}"}, "headers": {"A": "${C}"}}}},
        {"stt": {"openai": {"api_key": "${C}"}, "provider": "openai"}},
        {"model": {"api_key": "${C}", "base_url": "https://${C}.example.com"}},
        {"mcp_servers": "plain", "clarify": "plain", "stt": "plain"},  # a string ancestor without a reference
        {"platform_toolsets": {"glasses": ["web"]}, "agent": {"clarify_timeout": 0}, "approvals": {"timeout": 60}},
    ],
)
def test_a_reference_in_a_key_the_check_does_not_read_for_a_field_leaves_it_checked(config):
    assert refs.profile_unchecked(config) == frozenset()


def test_the_other_profile_fields_stay_checked_when_one_is_unchecked():
    config = {"approvals": {"timeout": "${C}"}, "agent": {"clarify_timeout": 0}, "stt": {"provider": "local"}}
    assert refs.profile_unchecked(config) == {refs.APPROVALS}


@pytest.mark.parametrize(
    "raw, field",
    [
        ({"gateway": {"profile_routes": [{"chat_id": "glasses:${C}", "profile": "x"}]}}, refs.ROUTES),
        ({"profile_routes": [{"profile": "${C}"}]}, refs.ROUTES),
        ({"gateway": {"profile_routes": [{"bot_profile": "${C}"}]}}, refs.ROUTES),
        ({"multiplex_profiles": "${C}"}, refs.MULTIPLEX),
        ({"gateway": {"multiplex_profiles": "${C}"}}, refs.MULTIPLEX),
        ({"allow_all_users": "${C}"}, refs.ALLOW_ALL),
        ({"gateway": {"allow_all_users": "${C}"}}, refs.ALLOW_ALL),
    ],
)
def test_a_reference_in_a_gateway_field_makes_it_unchecked(raw, field):
    assert refs.gateway_unchecked(raw) == {field}


def test_a_gateway_without_a_reference_in_those_fields_is_checked():
    raw = {"gateway": {"multiplex_profiles": True, "profile_routes": [{"profile": "work"}], "x": "${C}"}}
    assert refs.gateway_unchecked(raw) == frozenset()


def test_the_raw_yaml_of_a_missing_or_odd_file_is_empty_and_nothing_is_expanded(tmp_path, monkeypatch):
    assert refs.read_raw_yaml(tmp_path) == {}
    (tmp_path / "config.yaml").write_text("- a\n- b\n", encoding="utf-8")
    assert refs.read_raw_yaml(tmp_path) == {}
    monkeypatch.setenv("C", "expanded")
    (tmp_path / "config.yaml").write_text("approvals:\n  timeout: ${C}\n", encoding="utf-8")
    assert refs.read_raw_yaml(tmp_path) == {"approvals": {"timeout": "${C}"}}


def test_an_unchecked_field_is_never_computed():
    def boom():
        raise AssertionError("evaluated")

    assert refs.unless_unchecked("a", {"a"}, 5, boom) == 5
    assert refs.unless_unchecked("b", {"a"}, 5, lambda: 7) == 7


# -- the report ---------------------------------------------------------------------------------------------------


def test_a_profile_with_unchecked_fields_prints_one_fixed_warning_each_and_no_problem_from_them(tmp_path):
    odd = _profile(
        stt_provider=refs.NOT_CHECKED_PLACEHOLDER,
        glasses_tools=frozenset(),
        telegram_tools=frozenset(),
        clarify_timeout=0,
        approvals_timeout=1,
        unchecked=("approvals.timeout", "platform_toolsets", "stt.provider"),
    )
    code, out = run_live(live_home(tmp_path), _facts(profiles={"default": odd, "work": odd}))
    lines = out.splitlines()
    assert code == admin.EXIT_OK, out
    expected = [f"warning: profile {name}: {f}: {NC}" for name in ("default", "work") for f in odd.unchecked]
    assert lines[:6] == expected, out
    assert lines[-1] == "config check: OK (0 error(s), 6 warning(s))", out


def test_unchecked_routing_prints_one_warning_and_checks_no_lane_and_no_profile(tmp_path):
    bad = _profile(clarify_timeout=5, approvals_timeout=0)
    facts = _facts(
        served=frozenset(), routes={}, profiles={"default": bad, "work": bad}, unchecked=("gateway.profile_routes",)
    )
    code, out = run_live(live_home(tmp_path), facts)
    assert code == admin.EXIT_OK, out
    assert out.splitlines() == [f"warning: gateway.profile_routes: {NC}", "config check: OK (0 error(s), 1 warning(s))"]


def test_an_allow_all_reference_is_an_error_line_of_the_gate(tmp_path):
    facts = _facts(gate_problems=(refs.ALLOW_ALL_PROBLEM,))
    code, out = run_live(live_home(tmp_path), facts)
    assert code == admin.EXIT_ERRORS, out
    assert f"error: allow-all gate: active scope: allow_all_users in config.yaml {NC}" in out, out
