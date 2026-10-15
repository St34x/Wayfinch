# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin config check`` robustness: ``enabled`` is coerced the way the gateway coerces it (a bool-ish
string starts the platform, so it is not reported as disabled), and a deeply nested ``config.yaml`` is "not valid
YAML" (exit 2, one line, no traceback) whatever the nesting looks like."""

from __future__ import annotations

import io

import pytest

from glasses import admin, hermes_facts

from test_config_check_edges import EXTRA, check_file


def enabled_file(tmp_path, value):
    return check_file(tmp_path, {"gateway": {"platforms": {"glasses": {"enabled": value, "extra": EXTRA}}}})


@pytest.mark.parametrize("value", ["yes", "on", "1", "true", "TRUE", " On ", 1, True, 2, ["x"]])
def test_enabled_values_the_gateway_starts_are_not_reported_as_disabled(tmp_path, value):
    code, out = enabled_file(tmp_path, value)
    assert code == admin.EXIT_OK and "enabled: not true" not in out, out


@pytest.mark.parametrize("value", [False, "false", "no", "off", "0", "FALSE", "", "  ", "maybe", 0, [], None])
def test_enabled_values_the_gateway_does_not_start_still_warn(tmp_path, value):
    code, out = enabled_file(tmp_path, value)
    assert code == admin.EXIT_OK
    assert "warning: gateway.platforms.glasses.enabled: not true — the gateway will not start the platform" in out


def test_a_platform_without_an_enabled_key_still_warns(tmp_path):
    code, out = check_file(tmp_path, {"gateway": {"platforms": {"glasses": {"extra": EXTRA}}}})
    assert code == admin.EXIT_OK and "enabled: not true" in out, out


@pytest.mark.parametrize(
    ("value", "starts"),
    [
        ("yes", True),
        ("ON", True),
        ("0", False),
        ("off", False),
        ("garbage", False),
        (None, False),
        (1, True),
        (0, False),
    ],
)
def test_gateway_enabled_follows_the_gateways_coercion(value, starts):
    assert hermes_facts.gateway_enabled(value) is starts


NESTED = {
    "flow sequence": "gateway: " + "[" * 20000 + "]" * 20000,
    "flow mapping": "gateway: " + "{a: " * 20000 + "1" + "}" * 20000,
    "inside the glasses block": "gateway: {platforms: {glasses: {extra: " + "[" * 20000 + "]" * 20000 + "}}}",
}


@pytest.mark.parametrize("text", NESTED.values(), ids=NESTED.keys())
@pytest.mark.parametrize("live", [False, True])
def test_deeply_nested_yaml_is_one_line_exit_2_without_the_content(tmp_path, text, live):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    out = io.StringIO()
    code = admin.main(["config", "check", "--hermes-home", str(tmp_path), *(["--live"] if live else [])], {}, out)
    assert code == admin.EXIT_UNREADABLE
    assert out.getvalue().splitlines() == [f"error: {path}: not valid YAML"]
