# SPDX-License-Identifier: Apache-2.0
"""GLASSES_ALLOW_ALL_USERS and the allowlist variables: any allow-all request refuses the platform."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import glasses
from glasses import config as gcfg


def pc(extra=None, enabled=True):
    return SimpleNamespace(enabled=enabled, extra=extra)


# --------------------------------------------------------------------------------------------
# GLASSES_ALLOW_ALL_USERS: any non-blank value refuses the platform (stricter than Hermes)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", "n", " 0 ", "true", "TRUE", "1", "yes", "*"])
def test_allow_all_any_non_blank_value_refuses(monkeypatch, value):
    # Hermes treats only true/1/yes as allow-all (gateway/authz_mixin.py _TRUTHY); the plugin refuses ANY value,
    # so even "0"/"false" fail closed — documented as "must never be set".
    monkeypatch.setenv("GLASSES_ALLOW_ALL_USERS", value)
    assert gcfg.allow_all_requested() is True
    assert glasses.validate_config(pc({})) is False
    assert glasses.is_connected(pc({}, enabled=True)) is False


@pytest.mark.parametrize("value", ["", " ", "\t", "\n"])
def test_allow_all_blank_is_unset(monkeypatch, value):
    monkeypatch.setenv("GLASSES_ALLOW_ALL_USERS", value)
    assert glasses.validate_config(pc({})) is True


def test_allow_all_refusal_is_logged_without_value(monkeypatch, caplog):
    monkeypatch.setenv("GLASSES_ALLOW_ALL_USERS", "false-but-secret-looking-value")
    with caplog.at_level("WARNING"):
        assert glasses.validate_config(pc({})) is False
    assert "GLASSES_ALLOW_ALL_USERS" in caplog.text
    assert "secret-looking" not in caplog.text


def test_allow_all_explicit_environ_mapping():
    assert gcfg.allow_all_requested({"GLASSES_ALLOW_ALL_USERS": "false"}) is True
    assert gcfg.allow_all_requested({"GLASSES_ALLOW_ALL_USERS": ""}) is False
    assert gcfg.allow_all_requested({}) is False


def test_allowed_users_env_does_not_affect_validation(monkeypatch, no_allow_all):
    for value in ("", "dev_1,dev_2", "\n"):
        monkeypatch.setenv("GLASSES_ALLOWED_USERS", value)
        assert glasses.validate_config(pc({})) is True
    # A wildcard allowlist is refused like the allow-all switch.
    monkeypatch.setenv("GLASSES_ALLOWED_USERS", "*")
    assert glasses.validate_config(pc({})) is False
