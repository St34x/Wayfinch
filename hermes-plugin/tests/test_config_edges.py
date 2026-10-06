# SPDX-License-Identifier: Apache-2.0
"""Edge cases and hostile inputs for the glasses plugin config, on top of test_config.py.

Hosts, ports, defaults, types, strict strings, the allow-all gate and the Hermes import boundary.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

import glasses
from glasses import config as gcfg

from conftest import PLUGIN_ROOT


def pc(extra=None, enabled=True):
    return SimpleNamespace(enabled=enabled, extra=extra)


def errs(extra, key):
    return [e for e in gcfg.check(extra).errors if e.split(":", 1)[0] == key or e.startswith(key + "[")]


# --------------------------------------------------------------------------------------------
# host: loopback forms
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.2",
        "127.0.0.0",
        "127.255.255.255",
        "127.1.2.3",
        "::1",
        "0:0:0:0:0:0:0:1",
        "0000:0000:0000:0000:0000:0000:0000:0001",
        "::0001",
        "0::1",
    ],
)
def test_loopback_literals_accepted(host, no_allow_all):
    assert gcfg.is_loopback_host(host) is True
    assert gcfg.load({"host": host}).host == host
    assert glasses.validate_config(pc({"host": host})) is True


@pytest.mark.parametrize(
    "host",
    [
        # shorthand / legacy inet_aton forms: resolve to 127.0.0.1 in C but are not canonical literals
        "127.1",
        "127.0.1",
        "0177.0.0.1",
        "0x7f.0.0.1",
        "0x7f000001",
        "2130706433",
        "017700000001",
        # whitespace and control characters
        " 127.0.0.1 ",
        " 127.0.0.1",
        "127.0.0.1 ",
        "\t127.0.0.1",
        "127.0.0.1\n",
        "127.0.0.1\r\n",
        "127.0.0.1\x00",
        "127.0.0.1\u200b",
        "\u00a0127.0.0.1",
        # look-alikes and suffixes
        "127.0.0.1.",
        "１２７.0.0.1",
        "127.0.0.1/8",
        "127.0.0.1:8765",
        "127.0.0.256",
        # IPv6 forms that are not plain ::1
        "[::1]",
        "::1%lo",
        "::1%1",
        "fe80::1%lo",
        "::ffff:127.0.0.1",
        "::ffff:7f00:1",
        "::127.0.0.1",
        "::1/128",
        "::",
        "0::0",
        "::2",
        # names
        "localhost",
        "LOCALHOST",
        "ip6-localhost",
        "localhost.",
        "127.0.0.1.nip.io",
        # wrong types
        b"127.0.0.1",
        2130706433,
        1,
        ["127.0.0.1"],
        {"host": "127.0.0.1"},
        True,
        0.0,
    ],
)
def test_non_canonical_or_non_loopback_host_rejected(host, no_allow_all):
    assert gcfg.is_loopback_host(host) is False
    assert errs({"host": host}, "host"), gcfg.check({"host": host}).errors
    assert glasses.validate_config(pc({"host": host})) is False


def test_host_error_message_is_actionable():
    (msg,) = errs({"host": "0.0.0.0"}, "host")
    assert "loopback" in msg and "127.0.0.1" in msg


# --------------------------------------------------------------------------------------------
# ports
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["port", "bridge_port"])
@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        65536,
        70000,
        2**31,
        2**70,
        -(2**70),
        "8765",
        " 8765",
        "0x2000",
        "",
        b"8765",
        8765.0,
        8765.5,
        float("nan"),
        float("inf"),
        True,
        False,
        None,
        [8765],
        {"p": 1},
    ],
)
def test_port_values_rejected(key, value):
    extra = {"port": 9001, "bridge_port": 9002}
    extra[key] = value
    assert errs(extra, key), gcfg.check(extra).errors


@pytest.mark.parametrize("key", ["port", "bridge_port"])
@pytest.mark.parametrize("value", [1, 1024, 65535])
def test_port_boundaries_accepted(key, value):
    extra = {"port": 9001, "bridge_port": 9002}
    extra[key] = value
    if extra["port"] == extra["bridge_port"]:
        pytest.skip("collides")
    assert gcfg.check(extra).errors == []


@pytest.mark.parametrize(
    "extra",
    [
        {"port": 8766},  # equals the default bridge_port
        {"bridge_port": 8765},  # equals the default port
        {"port": 1, "bridge_port": 1},
        {"port": 65535, "bridge_port": 65535},
    ],
)
def test_bridge_port_equal_to_port_rejected_including_defaults(extra, no_allow_all):
    assert any("differ" in e for e in errs(extra, "bridge_port")), gcfg.check(extra).errors
    assert glasses.validate_config(pc(extra)) is False


def test_port_swap_with_defaults_is_valid():
    cfg = gcfg.load({"port": 8766, "bridge_port": 8765})
    assert (cfg.port, cfg.bridge_port) == (8766, 8765)


# --------------------------------------------------------------------------------------------
# missing keys / partial sections / wrong types
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(gcfg.DEFAULTS))
def test_each_key_missing_falls_back_to_default(key):
    full = {k: (dict(v) if isinstance(v, dict) else v) for k, v in gcfg.DEFAULTS.items()}
    del full[key]
    assert gcfg.load(full) == gcfg.load({})


def test_partial_nested_sections_merge_defaults():
    cfg = gcfg.load({"edge": {"tls_dir": "/srv/tls"}, "long_answers": {}})
    assert (cfg.edge_kind, cfg.edge_tls_dir, cfg.long_answers_platform) == ("nginx", "/srv/tls", "telegram")
    cfg = gcfg.load({"edge": None, "long_answers": None})
    assert (cfg.edge_kind, cfg.long_answers_platform) == ("nginx", "telegram")


@pytest.mark.parametrize(
    "key,value",
    [
        ("host", None),
        ("state_dir", 5),
        ("state_dir", ["/x"]),
        ("state_dir", True),
        ("public_url", None),
        ("public_url", 5),
        ("public_url", ["wss://x/ws"]),
        ("lanes", None),
        ("lanes", "assistant"),
        ("lanes", ("assistant",)),
        ("lanes", {"assistant": 1}),
        ("lanes", [None]),
        ("lanes", [["assistant"]]),
        ("home_lane", None),
        ("home_lane", 1),
        ("home_lane", ["assistant"]),
        ("client_cert_header", None),
        ("client_cert_header", 1),
        ("client_cert_encoding", None),
        ("client_cert_encoding", 1),
        ("client_cert_encoding", ["der-base64"]),
        ("long_threshold", None),
        ("long_threshold", "500"),
        ("long_threshold", 500.0),
        ("long_threshold", False),
        ("long_threshold", 100_001),
        ("long_threshold", 0),
        ("long_threshold", 513),
        ("long_answers", "telegram"),
        ("long_answers", ["telegram"]),
        ("long_answers", 1),
        ("edge", "nginx"),
        ("edge", ["nginx"]),
        ("edge", 1),
    ],
)
def test_wrong_types_rejected_with_key_named(key, value, no_allow_all):
    result = gcfg.check({key: value})
    assert any(e.split(":", 1)[0].split("[")[0].split(".")[0] == key for e in result.errors), result.errors
    assert glasses.validate_config(pc({key: value})) is False


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("edge", "kind", None),
        ("edge", "kind", ["nginx"]),
        ("edge", "tls_dir", 1),
        ("edge", "tls_dir", "rel/dir"),
        ("long_answers", "platform", None),
        ("long_answers", "platform", ["telegram"]),
        ("long_answers", "platform", "Telegram"),
    ],
)
def test_nested_wrong_types_rejected(section, key, value):
    assert any(e.startswith(f"{section}.{key}:") for e in gcfg.check({section: {key: value}}).errors)


def test_non_string_unknown_keys_only_warn():
    result = gcfg.check({1: 2, None: 3, ("t",): 4})
    assert result.errors == [] and len(result.warnings) == 3


@pytest.mark.parametrize("extra", ["", "host: 127.0.0.1", 0, 1, True, ["host"], ("host",), 3.5])
def test_extra_not_a_mapping(extra, no_allow_all):
    (msg,) = gcfg.check(extra).errors
    assert msg.startswith("extra: must be a mapping (got ") and " — " in msg
    assert glasses.validate_config(pc(extra)) is False


def test_validate_config_with_odd_platform_configs(no_allow_all):
    assert glasses.validate_config(None) is True  # no extra -> defaults
    assert glasses.validate_config({}) is True
    assert glasses.validate_config({"extra": None}) is True
    assert glasses.validate_config({"extra": "x"}) is False

    class Exploding:
        @property
        def extra(self):
            raise RuntimeError("boom")

    assert glasses.validate_config(Exploding()) is False  # never raises into the gateway


def test_validate_config_does_not_mutate_extra(no_allow_all):
    extra = {"port": 18700, "edge": {"kind": "caddy"}, "lanes": ["work"], "home_lane": "work"}
    snapshot = repr(extra)
    assert glasses.validate_config(pc(extra)) is True
    assert repr(extra) == snapshot


def test_defaults_are_not_shared_mutable_state():
    cfg = gcfg.load({})
    assert isinstance(cfg.lanes, tuple)
    with pytest.raises(Exception):
        cfg.port = 1  # frozen dataclass
    gcfg.check({"edge": {"kind": "caddy"}})
    assert gcfg.DEFAULTS["edge"] == {"kind": "nginx", "tls_dir": ""}


# --------------------------------------------------------------------------------------------
# trailing newline / whitespace in pattern-validated strings (Python `$` matches before "\n")
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra,key",
    [
        ({"lanes": ["work\n"], "home_lane": ""}, "lanes"),
        ({"lanes": [], "home_lane": "work\n"}, "home_lane"),
        ({"lanes": [], "home_lane": "glasses:work\n"}, "home_lane"),
        ({"client_cert_header": "X-Client-Cert\n"}, "client_cert_header"),
        ({"long_answers": {"platform": "telegram\n"}}, "long_answers.platform"),
    ],
)
def test_trailing_newline_rejected(extra, key):
    # The envelope schema rejects "work\n" as a lane (protocol/README.md: `$` guard); the config must agree,
    # otherwise a configured lane / home channel can never appear in a valid frame.
    assert errs(extra, key), f"{extra!r} accepted: {gcfg.check(extra)}"


@pytest.mark.parametrize(
    "url",
    [
        "wss://glasses.example.com/ws\n",
        "wss://gla\nsses.example.com/ws",
        "wss://glasses.example.com/ws\t",
        " wss://glasses.example.com/ws",
        "wss://glasses.example.com/ws ",
        "wss://glasses.example.com:0/ws",
    ],
)
def test_public_url_whitespace_and_port_zero_rejected(url):
    # urlsplit() silently strips \t \r \n and surrounding spaces while parsing, but the raw string is what
    # ends up in the enrolment QR.
    assert errs({"public_url": url}, "public_url"), f"{url!r} accepted"


@pytest.mark.parametrize(
    "url",
    [
        "wss://glasses.example.com:65536/ws",
        "wss://glasses.example.com:abc/ws",
        "wss://@glasses.example.com/ws",
        "wss://:pw@glasses.example.com/ws",
        "wss://user@glasses.example.com/ws",
        "wss://[::1/ws",
        "ws+unix://x",
        "wss:glasses.example.com/ws",
    ],
)
def test_public_url_more_rejects(url):
    assert errs({"public_url": url}, "public_url"), f"{url!r} accepted"


def test_contract_empty_query_or_fragment_marker_accepted():
    # Nit, pinned: urlsplit() yields "" for a bare "?" / "#", so these pass. Harmless for the QR.
    assert gcfg.check({"public_url": "wss://glasses.example.com/ws?"}).errors == []
    assert gcfg.check({"public_url": "wss://glasses.example.com/ws#"}).errors == []


def test_long_answers_platform_must_not_be_glasses_itself():
    # Long answers are forwarded to ANOTHER platform; glasses -> glasses would loop.
    assert errs({"long_answers": {"platform": "glasses"}}, "long_answers.platform")


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


# --------------------------------------------------------------------------------------------
# importing the package never imports Hermes (fresh interpreter, import blocker)
# --------------------------------------------------------------------------------------------

# Top-level modules/packages of the pinned Hermes tree (hermes-agent @ a4648c5f58): anything named here or
# starting with "hermes" is Hermes.
HERMES_TOP_LEVEL = (
    "acp_adapter",
    "agent",
    "batch_runner",
    "cli",
    "cron",
    "gateway",
    "mcp_serve",
    "mini_swe_runner",
    "model_tools",
    "pm",
    "providers",
    "registration_lifecycle",
    "run_agent",
    "tools",
    "toolsets",
    "toolset_distributions",
    "trajectory_compressor",
    "tui_gateway",
)

_PROBE = textwrap.dedent("""
    import importlib.abc, sys
    HERMES = set({names!r})
    attempts = []

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            top = name.split(".", 1)[0]
            if top in HERMES or top.startswith("hermes"):
                attempts.append(name)
                raise ImportError("blocked Hermes import: " + name)
            return None

    sys.meta_path.insert(0, Block())
    sys.path.insert(0, {root!r})
    import glasses, glasses.config
    from types import SimpleNamespace

    class Ctx:
        def register_platform(self, **kw):
            self.kw = kw

    ctx = Ctx()
    glasses.register(ctx)
    assert ctx.kw["name"] == "glasses"
    glasses.validate_config(SimpleNamespace(enabled=True, extra={{"host": "0.0.0.0"}}))
    glasses.is_connected(SimpleNamespace(enabled=True, extra={{}}))
    glasses.check_requirements()
    glasses.config.load({{}})
    loaded = sorted(m for m in sys.modules if m.split(".", 1)[0] in HERMES or m.startswith("hermes"))
    assert "glasses.adapter" not in sys.modules, "adapter imported eagerly"
    print("ATTEMPTS", attempts)
    print("LOADED", loaded)
""")


def test_import_register_validate_never_import_hermes_in_fresh_interpreter():
    code = _PROBE.format(names=HERMES_TOP_LEVEL, root=str(PLUGIN_ROOT))
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "ATTEMPTS []" in proc.stdout, proc.stdout
    assert "LOADED []" in proc.stdout, proc.stdout


def test_adapter_module_is_the_only_hermes_boundary():
    # adapter.py needs Hermes; in a Hermes-free interpreter importing it fails, while the package works.
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import glasses\n"
        "try:\n"
        "    import glasses.adapter\n"
        "except ImportError as e:\n"
        "    print('IMPORT_ERROR', type(e).__name__)\n"
        "else:\n"
        "    print('IMPORTED')\n" % str(PLUGIN_ROOT)
    )
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "IMPORT_ERROR" in proc.stdout, proc.stdout


def test_adapter_factory_imports_lazily():
    call = {}

    class Ctx:
        def register_platform(self, **kw):
            call.update(kw)

    sys.modules.pop("glasses.adapter", None)
    glasses.register(Ctx())
    factory = call["adapter_factory"]
    assert "glasses.adapter" not in sys.modules
    # Calling the factory is what pulls in Hermes; in the dev venv that must fail loudly, not silently.
    with pytest.raises(ImportError):
        factory(pc({}))
    sys.modules.pop("glasses.adapter", None)
