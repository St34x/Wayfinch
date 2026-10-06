# SPDX-License-Identifier: Apache-2.0
"""Glasses access gate and limits: allow-all gate, gateway auth keys, message cap, long-answer threshold,
strict strings. No Hermes import (conftest.py enforces it); the Hermes readers are faked.
"""

from __future__ import annotations

import ast
import json
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
    # Not consulted for glasses once an allowlist exists (gateway/authz_mixin.py:666-675).
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
            installed_profiles=installed,
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
    assert gate.violations() == [
        "profile default: GLASSES_ALLOWED_USERS contains a wildcard '*'; list device ids explicitly"
    ]
    assert glasses.validate_config(pc({})) is False


def test_active_scope_is_read_with_hermes_reader(monkeypatch):
    use(monkeypatch, FakeHermes({"default": {}}, active={"GLASSES_ALLOWED_USERS": "*"}))
    assert any(v.startswith("active scope: GLASSES_ALLOWED_USERS") for v in gate.violations())


def test_clean_profiles_pass_and_every_profile_is_read(monkeypatch):
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
    # (adapter.connect() and adapter.handle_message() call gate.violations() each time).
    scopes = {"default": {}, "dev": {}}
    use(monkeypatch, FakeHermes(scopes))
    assert glasses.validate_config(pc({})) is True
    scopes["dev"]["GLASSES_ALLOW_ALL_USERS"] = "1"
    assert gate.violations() == [
        "profile dev: GLASSES_ALLOW_ALL_USERS is set; it must never be set "
        "(list device ids in GLASSES_ALLOWED_USERS instead)"
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
        assert calls == [(True, True, True)]  # every installed profile, served or not
        assert gate.violations()[0].startswith("profile dev: GLASSES_ALLOW_ALL_USERS is set")


def _adapter_class() -> ast.ClassDef:
    tree = ast.parse((PLUGIN_DIR / "adapter.py").read_text())
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GlassesAdapter")


def _method_calls(cls: ast.ClassDef, method: str) -> set[str]:
    fn = next(n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method)
    return {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}


def test_adapter_rechecks_gate_on_connect_and_every_inbound_event():
    cls = _adapter_class()
    assert "self.gate_ok" in _method_calls(cls, "connect")
    assert "self.gate_ok" in _method_calls(cls, "handle_message")
    assert "gate.violations" in _method_calls(cls, "gate_ok")


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
    # gateway/config_loader.py:161,373 put _enabled_explicit into extra.
    result = gcfg.check({"_enabled_explicit": True})
    assert result.errors == [] and result.warnings == []


# --------------------------------------------------------------------------------------------
# 5. one message cap; the gateway does not split what the long-answer router must see
# --------------------------------------------------------------------------------------------


def test_message_cap_is_one_constant_and_byte_safe():
    assert gcfg.MAX_MESSAGE_LENGTH == (65_536 - 1_024) // 6 == 10_752
    assert gcfg.MAX_MESSAGE_LENGTH >= 8192  # T1.04 review: at least 8192
    assert gcfg.MAX_MESSAGE_LENGTH > 4096  # above the gateway default it replaces
    assert not hasattr(glasses, "MAX_MESSAGE_LENGTH") or glasses.MAX_MESSAGE_LENGTH == gcfg.MAX_MESSAGE_LENGTH


@pytest.mark.parametrize("ch", ["a", "ж", "€", "😀", "\x01", '"', "\\", "\u2028"])
def test_reply_at_the_cap_fits_one_protocol_frame(ch):
    text = ch * gcfg.MAX_MESSAGE_LENGTH
    frame = {
        "v": 1,
        "type": "msg.out",
        "id": "0" * 36,
        "ts": 1_790_000_000_000,
        "body": {
            "kind": "answer",
            "chatId": "glasses:" + "a" * 32,
            "final": True,
            "longRef": {
                "id": "0" * 36,
                "channel": "telegram",
                "profile": "p" * 32,
                "status": "pending",
                "fullText": text,
            },
        },
    }
    size = len(json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert size <= gcfg.FRAME_MAX_BYTES, size


def test_adapter_class_sets_the_gateway_caps():
    cls = _adapter_class()
    assigns = {
        t.id: ast.unparse(n.value)
        for n in cls.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    assert assigns["MAX_MESSAGE_LENGTH"] == "MAX_MESSAGE_LENGTH"  # imported from glasses.config
    assert assigns["splits_long_messages"] == "True"
    tree = ast.parse((PLUGIN_DIR / "adapter.py").read_text())
    imports = {(n.module, a.name) for n in tree.body if isinstance(n, ast.ImportFrom) for a in n.names}
    assert ("config", "MAX_MESSAGE_LENGTH") in imports


def test_no_misleading_chunking_comment():
    text = (PLUGIN_DIR / "__init__.py").read_text()
    assert "Smart-chunking cap of the gateway" not in text
    assert "MAX_MESSAGE_LENGTH = 2000" not in text


# --------------------------------------------------------------------------------------------
# 6/7. long_threshold default and the long-answer loop
# --------------------------------------------------------------------------------------------


def test_long_threshold_default_is_280():
    assert gcfg.DEFAULTS["long_threshold"] == 280
    assert gcfg.load({}).long_threshold == 280


@pytest.mark.parametrize("value", ["glasses", "glasses:assistant", "Glasses"])
def test_long_answers_platform_never_glasses(value):
    assert any(e.startswith("long_answers.platform:") for e in gcfg.check({"long_answers": {"platform": value}}).errors)


# --------------------------------------------------------------------------------------------
# 1/2. full-match strings, strict public_url
# --------------------------------------------------------------------------------------------


def test_sources_use_fullmatch_only():
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        text = path.read_text()
        assert ".match(" not in text, path.name
        assert "re.search(" not in text, path.name


@pytest.mark.parametrize(
    "extra,key",
    [
        ({"state_dir": "/srv/glasses\n"}, "state_dir"),
        ({"state_dir": " /srv/glasses"}, "state_dir"),
        ({"state_dir": "/srv/gl\x00asses"}, "state_dir"),
        ({"edge": {"tls_dir": "/etc/tls\n"}}, "edge.tls_dir"),
        ({"edge": {"tls_dir": "/etc/tls "}}, "edge.tls_dir"),
        ({"edge": {"kind": "nginx\n"}}, "edge.kind"),
        ({"client_cert_encoding": "der-base64\n"}, "client_cert_encoding"),
        ({"client_cert_header": " X-Client-Cert"}, "client_cert_header"),
        ({"lanes": ["work "], "home_lane": ""}, "lanes"),
        ({"lanes": [], "home_lane": " work"}, "home_lane"),
        ({"long_answers": {"platform": "telegram "}}, "long_answers.platform"),
        ({"host": "127.0.0.1\n"}, "host"),
    ],
)
def test_whitespace_and_control_chars_rejected_in_every_string_key(extra, key):
    errors = gcfg.check(extra).errors
    assert any(e.split(":", 1)[0].split("[")[0] == key for e in errors), errors


@pytest.mark.parametrize(
    "url",
    [
        "wss://0.0.0.0/ws",
        "wss://[::]/ws",
        "wss://glasses.example.com:/ws",
        "WSS://glasses.example.com/ws",
        "wss://glässes.example.com/ws",
        "wss://glasses.example.com./ws",
        "wss://glasses..example.com/ws",
        "wss://-glasses.example.com/ws",
        "wss://glasses_x.example.com/ws",
        "wss://127.1/ws",
        "wss://glasses.example.com/w s",
        "wss://glasses.example.com/ws\x7f",
        "wss://[fe80::1%25lo]/ws",
        "wss://" + "a" * 64 + ".example.com/ws",
        "https://glasses.example.com/ws",
        "wss://",
        "wss:///ws",
    ],
)
def test_public_url_strict_rejects(url):
    assert any(e.startswith("public_url:") for e in gcfg.check({"public_url": url}).errors), url


def test_public_url_error_never_echoes_the_value():
    (msg,) = gcfg.check({"public_url": "wss://user:s3cr3t@glasses.example.com/ws\n"}).errors
    assert "s3cr3t" not in msg and "glasses.example.com" not in msg
