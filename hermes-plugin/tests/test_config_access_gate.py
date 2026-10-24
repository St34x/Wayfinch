# SPDX-License-Identifier: Apache-2.0
"""Glasses access gate: allow-all gate and the gateway auth keys. Message cap, long-answer threshold and strict
strings: test_config_limits_and_strings.py. No Hermes import (conftest.py enforces it); the Hermes readers are faked.
"""

from __future__ import annotations

import ast
import sys
from types import SimpleNamespace

import pytest

import glasses
from glasses import config as gcfg
from glasses import gate

from conftest import PLUGIN_DIR


def pc(extra=None, enabled=True):
    return SimpleNamespace(enabled=enabled, extra=extra)


def reader(**env):
    return lambda name: (env.get(name) or "").strip()


# --------------------------------------------------------------------------------------------
# 3. allow-all is forbidden: not registered, refused in every profile scope, re-checked at runtime
# --------------------------------------------------------------------------------------------


def test_allow_all_env_is_not_registered():
    calls = {}

    class Ctx:
        def register_platform(self, **kw):
            calls.update(kw)

    glasses.register(Ctx())
    assert "allow_all_env" not in calls
    assert calls["allowed_users_env"] == "GLASSES_ALLOWED_USERS"


@pytest.mark.parametrize("value", ["*", " * ", "dev_1,*", "*,dev_1", '["*"]', '["dev_1", " * "]', "dev*", "**"])
@pytest.mark.parametrize("name", ["GLASSES_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS"])
def test_wildcard_allowlist_is_a_violation(name, value):
    problems = gcfg.gate_violations(reader(**{name: value}))
    assert any(p.startswith(name) and "wildcard" in p for p in problems), problems


@pytest.mark.parametrize("value", ["dev_1", "dev_1,dev_2", '["dev_1","dev_2"]', "", " ", ","])
def test_explicit_allowlist_is_fine(value):
    assert gcfg.gate_violations(reader(GLASSES_ALLOWED_USERS=value)) == []


def test_parse_allowlist_matches_hermes_coerce_allow_set():
    # gateway/authz_mixin.py:71-78 + gateway/platforms/_shared.py:89-103
    assert gcfg.parse_allowlist(" a , b ,, ") == {"a", "b"}
    assert gcfg.parse_allowlist('["a", " b ", ""]') == {"a", "b"}
    assert gcfg.parse_allowlist("[not json") == {"[not json"}
    assert gcfg.parse_allowlist(None) == set()


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", " yes "])
def test_gateway_allow_all_with_empty_glasses_allowlist_is_a_violation(value):
    problems = gcfg.gate_violations(reader(GATEWAY_ALLOW_ALL_USERS=value))
    assert any(p.startswith("GATEWAY_ALLOW_ALL_USERS") for p in problems), problems
    # Not consulted for glasses once an allowlist exists (gateway/authz_mixin.py:667-676).
    assert gcfg.gate_violations(reader(GATEWAY_ALLOW_ALL_USERS=value, GLASSES_ALLOWED_USERS="dev_1")) == []
    assert gcfg.gate_violations(reader(GATEWAY_ALLOW_ALL_USERS=value, GATEWAY_ALLOWED_USERS="dev_1")) == []


@pytest.mark.parametrize("value", ["false", "0", "no", ""])
def test_gateway_allow_all_falsy_is_fine(value):
    assert gcfg.gate_violations(reader(GATEWAY_ALLOW_ALL_USERS=value)) == []


def test_gate_messages_never_echo_values():
    secretish = "dev_secret_value_*"
    problems = gcfg.gate_violations(reader(GLASSES_ALLOWED_USERS=secretish, GLASSES_ALLOW_ALL_USERS="maybe-secret"))
    assert len(problems) == 2
    assert not any("secret" in p for p in problems)


@pytest.mark.parametrize(
    "name,value",
    [
        ("GLASSES_ALLOWED_USERS", "*"),
        ("GLASSES_ALLOWED_USERS", "dev_1,*"),
        ("GATEWAY_ALLOWED_USERS", "*"),
        ("GATEWAY_ALLOW_ALL_USERS", "true"),
        ("GLASSES_ALLOW_ALL_USERS", "true"),
    ],
)
def test_validate_config_refuses_open_gate_in_process_env(monkeypatch, caplog, name, value):
    monkeypatch.setenv(name, value)
    with caplog.at_level("WARNING"):
        assert glasses.validate_config(pc({})) is False
    assert name in caplog.text
    assert glasses.is_connected(pc({}, enabled=True)) is False


def test_without_hermes_only_the_process_env_is_read(monkeypatch):
    monkeypatch.delitem(sys.modules, gate.HERMES_MARKER_MODULE, raising=False)
    assert gate._hermes_api() is None
    assert [label for label, _ in gate.sources()] == ["process environment"]


class FakeHermes:
    """Stands in for platform_gate_env + build_profile_secret_scope + profiles_to_serve."""

    def __init__(self, scopes, active=None, fail=False):
        self.scopes = scopes  # profile name -> mapping (the profile's .env as Hermes parses it)
        self.active = active or {}
        self.fail = fail
        self.built = []

    def api(self):
        def build(home):
            self.built.append(home.name)
            return self.scopes[home.name]

        def installed():
            if self.fail:
                raise OSError("profiles dir unreadable")
            return [(name, f"/fake/{name}") for name in self.scopes]

        return SimpleNamespace(
            platform_gate_env=lambda name, default="": (self.active.get(name) or default).strip(),
            build_profile_secret_scope=build,
            served_profiles=installed,
        )


def use(monkeypatch, fake):
    monkeypatch.setattr(gate, "_hermes_api", fake.api)


def test_profile_env_allow_all_refuses_the_platform(monkeypatch, caplog):
    # GLASSES_ALLOW_ALL_USERS only in a routed profile's .env, not in os.environ.
    use(monkeypatch, FakeHermes({"default": {}, "dev": {"GLASSES_ALLOW_ALL_USERS": "true"}}))
    with caplog.at_level("WARNING"):
        assert glasses.validate_config(pc({})) is False
    assert "profile dev: GLASSES_ALLOW_ALL_USERS is set" in caplog.text
    assert "profile default" not in caplog.text


def test_profile_env_wildcard_refuses_the_platform(monkeypatch):
    use(monkeypatch, FakeHermes({"default": {"GLASSES_ALLOWED_USERS": "dev_1, *"}}))
    (problem,) = gate.violations()
    assert problem.startswith("profile default: GLASSES_ALLOWED_USERS contains a wildcard '*'; list device ids ")
    assert glasses.validate_config(pc({})) is False


def test_active_scope_is_read_with_hermes_reader(monkeypatch):
    use(monkeypatch, FakeHermes({"default": {}}, active={"GLASSES_ALLOWED_USERS": "*"}))
    assert any(v.startswith("active scope: GLASSES_ALLOWED_USERS") for v in gate.violations())


def test_clean_profiles_pass_and_every_served_profile_is_read(monkeypatch):
    fake = FakeHermes({"default": {"GLASSES_ALLOWED_USERS": "dev_1"}, "dev": {}, "support": {"X": "1"}})
    use(monkeypatch, fake)
    assert gate.violations() == []
    assert fake.built == ["default", "dev", "support"]
    assert glasses.validate_config(pc({})) is True


def test_unreadable_profiles_fail_closed(monkeypatch):
    use(monkeypatch, FakeHermes({"default": {}}, fail=True))
    (problem,) = gate.violations()
    assert "unreadable" in problem and "OSError" in problem
    assert glasses.validate_config(pc({})) is False


def test_gate_is_re_read_on_every_call(monkeypatch):
    # Runtime re-check: a value added after the adapter was built is seen by the next check
    # (the adapter's cached check, test_gate.py, calls it again once its verdict has expired).
    scopes = {"default": {}, "dev": {}}
    use(monkeypatch, FakeHermes(scopes))
    assert glasses.validate_config(pc({})) is True
    scopes["dev"]["GLASSES_ALLOW_ALL_USERS"] = "1"
    assert gate.violations() == [
        "profile dev: GLASSES_ALLOW_ALL_USERS is set; it must never be set "
        "(list device ids in GLASSES_ALLOWED_USERS instead); or remove the switch from that profile"
    ]
    del scopes["dev"]["GLASSES_ALLOW_ALL_USERS"]
    assert gate.violations() == []


def test_hermes_api_uses_hermes_names_when_hermes_is_loaded(monkeypatch):
    # Wiring check against fake modules with the pinned names (no real Hermes import).
    calls = []
    shared = SimpleNamespace(platform_gate_env=lambda name, default="": "")
    secret_scope = SimpleNamespace(build_profile_secret_scope=lambda home: {"GLASSES_ALLOW_ALL_USERS": "x"})

    def profiles_to_serve(multiplex, *, include_standalone=False, include_parked=False):
        calls.append((multiplex, include_standalone, include_parked))
        return [("dev", "/fake/dev")]

    # A local context (not the fixture's teardown) so the fakes are gone before conftest's leak check.
    with monkeypatch.context() as mp:
        mp.setitem(sys.modules, "gateway.platforms._shared", shared)
        mp.setitem(sys.modules, "agent.secret_scope", secret_scope)
        mp.setitem(sys.modules, "hermes_cli.profiles", SimpleNamespace(profiles_to_serve=profiles_to_serve))
        for parent in ("gateway", "gateway.platforms", "agent", "hermes_cli"):
            mp.setitem(sys.modules, parent, sys.modules.get(parent) or SimpleNamespace())
        assert [label for label, _ in gate.sources()] == ["process environment", "active scope", "profile dev"]
        assert calls == [(True, False, False)]  # the served profiles only (test_gate.py)
        assert gate.violations()[0].startswith("profile dev: GLASSES_ALLOW_ALL_USERS is set")


def _adapter_class() -> ast.ClassDef:
    tree = ast.parse((PLUGIN_DIR / "adapter.py").read_text())
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GlassesAdapter")


def _method_calls(cls: ast.ClassDef, method: str) -> set[str]:
    fn = next(n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method)
    return {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}


def test_adapter_rechecks_gate_on_connect_and_every_inbound_event():
    # Inbound events reach the gate through hand_on: handle_message delegates to it and the frame router
    # (adapter_router.py) calls it directly, so hand_on must check the gate before the base class's handle_message.
    cls = _adapter_class()
    for method in ("connect", "hand_on"):
        calls = _method_calls(cls, method)
        assert "self.gate_ok_async" in calls
        assert "self.gate_ok" not in calls  # the synchronous check would run a recompute on the event loop
    assert _method_calls(cls, "handle_message") == {"self.hand_on"}
    first = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "hand_on").body[1]
    assert isinstance(first, ast.If) and ast.unparse(first.test) == "not await self.gate_ok_async()"
    assert isinstance(first.body[-1], ast.Return)  # refused: the base class's handle_message is never reached
    router = ast.parse((PLUGIN_DIR / "adapter_router.py").read_text())
    routed = {ast.unparse(n.func) for n in ast.walk(router) if isinstance(n, ast.Call)}
    assert "self._adapter.hand_on" in routed and not any("handle_message" in name for name in routed)
    assert "asyncio.to_thread" not in _method_calls(cls, "gate_ok_async")
    assert "self._gate_cache.violations" in _method_calls(cls, "gate_ok")


# --------------------------------------------------------------------------------------------
# 4. access-control keys the gateway acts on are refused, never "unknown, ignored"
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(gcfg.HERMES_AUTH_KEYS))
@pytest.mark.parametrize("value", ["*", ["*"], "open", "pairing", ["dev_1"], "", None])
def test_gateway_auth_keys_are_refused(key, value):
    result = gcfg.check({key: value})
    assert any(e.startswith(f"{key}:") and "GLASSES_ALLOWED_USERS" in e for e in result.errors), result.errors
    assert not any(w.startswith(f"{key}:") for w in result.warnings), result.warnings
    assert glasses.validate_config(pc({key: value})) is False


def test_auth_keys_cover_what_the_pinned_gateway_reads():
    assert {
        "allow_from",
        "group_allow_from",
        "allowed_users",
        "group_allowed_chats",
        "dm_policy",
        "group_policy",
        "groups",
        "allow_bots",
        "unauthorized_dm_behavior",
        "allow_admin_from",
        "user_allowed_commands",
        "group_allow_admin_from",
        "group_user_allowed_commands",
    } <= gcfg.HERMES_AUTH_KEYS


def test_open_access_keys_in_extra_are_refused():
    # Once accepted as "unknown key, ignored"; each opens the gate.
    result = gcfg.check({
        "allow_from": ["*"],
        "allowed_users": "*",
        "dm_policy": "open",
        "unauthorized_dm_behavior": "pair",
    })
    assert sorted(e.split(":", 1)[0] for e in result.errors) == [
        "allow_from",
        "allowed_users",
        "dm_policy",
        "unauthorized_dm_behavior",
    ]
    assert result.warnings == []


def test_enabled_explicit_injected_by_hermes_is_silent():
    # gateway/config_loader.py:162,375 put _enabled_explicit into extra.
    result = gcfg.check({"_enabled_explicit": True})
    assert result.errors == [] and result.warnings == []
