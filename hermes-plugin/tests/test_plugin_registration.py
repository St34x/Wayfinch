# SPDX-License-Identifier: Apache-2.0
"""The plugin as the gateway sees it: the validate_config and is_connected hooks, check_fn, registration and
the manifest.

No Hermes import anywhere (enforced by conftest.py).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

import glasses
from glasses import config as gcfg


def pc(extra=None, enabled=True):
    """A stand-in for Hermes' PlatformConfig (only the attributes the plugin reads)."""
    return SimpleNamespace(enabled=enabled, extra=extra)


# --------------------------------------------------------------------------------------------
# the gateway hooks validate_config and is_connected
# --------------------------------------------------------------------------------------------


def test_validate_config_accepts_dict_and_object(no_allow_all):
    assert glasses.validate_config(pc({})) is True
    assert glasses.validate_config(pc(None)) is True
    assert glasses.validate_config({"extra": {"host": "::1"}}) is True
    assert glasses.validate_config({"extra": {"host": "0.0.0.0"}}) is False


def test_validate_config_never_raises(no_allow_all):
    assert glasses.validate_config(object()) is True  # no extra attribute -> defaults
    assert glasses.validate_config(pc(42)) is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "operator"])
def test_allow_all_switch_refuses_config(monkeypatch, value):
    monkeypatch.setenv("GLASSES_ALLOW_ALL_USERS", value)
    assert glasses.validate_config(pc({})) is False


def test_allow_all_empty_is_ignored(monkeypatch):
    monkeypatch.setenv("GLASSES_ALLOW_ALL_USERS", "")
    assert glasses.validate_config(pc({})) is True


def test_validate_config_logs_reasons(caplog, no_allow_all):
    with caplog.at_level("WARNING"):
        assert glasses.validate_config(pc({"host": "0.0.0.0"})) is False
    assert "host:" in caplog.text and "loopback" in caplog.text


def test_is_connected(no_allow_all):
    assert glasses.is_connected(pc({}, enabled=True)) is True
    assert glasses.is_connected(pc({}, enabled=False)) is False
    assert glasses.is_connected(pc({"host": "0.0.0.0"}, enabled=True)) is False


# --------------------------------------------------------------------------------------------
# check_fn
# --------------------------------------------------------------------------------------------


def test_check_requirements_is_passive_and_bool(monkeypatch):
    assert glasses.check_requirements() in (True, False)
    seen = []

    def fake_find_spec(name):
        seen.append(name)
        return None if name == "cryptography" else object()

    monkeypatch.setattr(glasses.importlib.util, "find_spec", fake_find_spec)
    assert glasses.check_requirements() is False
    assert set(seen) <= {"aiohttp", "jsonschema", "cryptography"}
    monkeypatch.setattr(glasses.importlib.util, "find_spec", lambda name: object())
    assert glasses.check_requirements() is True


# --------------------------------------------------------------------------------------------
# registration with the gateway
# --------------------------------------------------------------------------------------------

# PlatformEntry fields at the pinned Hermes a4648c5f58 (gateway/platform_registry.py:43-112);
# register_platform() forwards extra kwargs to PlatformEntry and unknown keys raise TypeError.
PLATFORM_ENTRY_FIELDS = {
    "name",
    "label",
    "adapter_factory",
    "check_fn",
    "validate_config",
    "ensure_deps_fn",
    "is_connected",
    "required_env",
    "install_hint",
    "setup_fn",
    "source",
    "plugin_name",
    "allowed_users_env",
    "allow_all_env",
    "max_message_length",
    "pii_safe",
    "emoji",
    "allow_update_command",
    "platform_hint",
    "env_enablement_fn",
    "apply_yaml_config_fn",
    "cron_deliver_env_var",
    "parse_target_ref_fn",
    "validate_target_ref_fn",
    "send_message_handler",
    "standalone_sender_fn",
}


class FakeCtx:
    def __init__(self):
        self.calls = []

    def register_platform(
        self,
        name,
        label,
        adapter_factory,
        check_fn,
        validate_config=None,
        required_env=None,
        install_hint="",
        **entry_kwargs,
    ):
        self.calls.append(
            dict(
                name=name,
                label=label,
                adapter_factory=adapter_factory,
                check_fn=check_fn,
                validate_config=validate_config,
                required_env=required_env,
                install_hint=install_hint,
                **entry_kwargs,
            )
        )
        return object()


def registered():
    ctx = FakeCtx()
    glasses.register(ctx)
    assert len(ctx.calls) == 1
    return ctx.calls[0]


def test_register_platform_glasses():
    call = registered()
    assert call["name"] == "glasses"
    assert call["label"]
    assert callable(call["adapter_factory"])
    assert call["check_fn"] is glasses.check_requirements
    assert call["validate_config"] is glasses.validate_config
    assert call["is_connected"] is glasses.is_connected
    assert call["allowed_users_env"] == "GLASSES_ALLOWED_USERS"
    # Never registered: the gateway honours a registered allow_all_env per event (authz_mixin.py:645-651).
    assert "allow_all_env" not in call
    assert call["max_message_length"] == gcfg.MAX_MESSAGE_LENGTH
    assert isinstance(call["platform_hint"], str) and call["platform_hint"].strip()
    assert call["allow_update_command"] is False
    assert call["required_env"] == []


def test_register_uses_only_known_platform_entry_fields():
    assert set(registered()) <= PLATFORM_ENTRY_FIELDS


def test_platform_hint_is_generic():
    hint = registered()["platform_hint"].lower()
    assert "glasses" in hint
    for word in ("telegram", "operator"):
        assert word not in hint


def test_register_does_not_import_adapter_or_hermes():
    import sys

    sys.modules.pop("glasses.adapter", None)
    registered()
    assert "glasses.adapter" not in sys.modules


# --------------------------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------------------------


def test_manifest(plugin_dir):
    text = (plugin_dir / "plugin.yaml").read_text()
    assert text.splitlines()[0] == "# SPDX-License-Identifier: Apache-2.0"
    manifest = yaml.safe_load(text)
    assert manifest["name"] == "glasses"
    assert manifest["kind"] == "platform"
    assert manifest["version"]
    assert manifest["requires_env"] == []
    names = [e["name"] for e in manifest.get("optional_env", [])]
    assert "GLASSES_ALLOWED_USERS" in names
    assert "GLASSES_ALLOW_ALL_USERS" not in names  # documented as never set, never prompted for


def test_source_files_carry_spdx(plugin_dir):
    for path in sorted(plugin_dir.glob("*.py")):
        first = path.read_text().splitlines()[0]
        assert first == "# SPDX-License-Identifier: Apache-2.0", path.name
