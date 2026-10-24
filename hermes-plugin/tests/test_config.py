# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the glasses plugin config: defaults, value rules, unknown keys, errors.

No Hermes import anywhere (enforced by conftest.py).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import glasses
from glasses import config as gcfg


def pc(extra=None, enabled=True):
    """A stand-in for Hermes' PlatformConfig (only the attributes the plugin reads)."""
    return SimpleNamespace(enabled=enabled, extra=extra)


# --------------------------------------------------------------------------------------------
# defaults
# --------------------------------------------------------------------------------------------


def test_defaults_are_generic():
    cfg = gcfg.load({})
    assert cfg.edge_socket == "" and cfg.edge_user == ""
    assert cfg.bridge_port == 8766
    assert cfg.state_dir is None
    assert cfg.public_url == ""
    assert cfg.lanes == ("assistant",)
    assert cfg.home_lane == "assistant"
    assert cfg.client_cert_header == "X-Client-Cert"
    assert cfg.client_cert_encoding == "pem-urlescaped"
    assert cfg.long_threshold == 280  # long-answer threshold default (T1.41)
    assert cfg.long_answers_platform == "telegram"


def test_none_extra_means_defaults():
    assert gcfg.load(None) == gcfg.load({})


def test_defaults_name_no_deployment():
    # Publishable defaults: no default names a real host, domain or profile.
    text = repr(gcfg.DEFAULTS).lower()
    for word in ("dev", "default", ".com", ".net", "/home/", "/var/"):
        assert word not in text, word


def test_home_chat_id():
    assert gcfg.load({}).home_chat_id == "glasses:assistant"
    assert gcfg.load({"home_lane": ""}).home_chat_id is None


# --------------------------------------------------------------------------------------------
# bridge_port
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, 65536, "8766", True, 8766.0, None])
def test_bad_bridge_ports_rejected(value):
    errors = gcfg.check({"bridge_port": value}).errors
    assert any(e.startswith("bridge_port:") for e in errors), errors


def test_valid_bridge_ports():
    assert gcfg.load({"bridge_port": 18700}).bridge_port == 18700
    assert gcfg.load({"bridge_port": 1}).bridge_port == 1


# --------------------------------------------------------------------------------------------
# other keys
# --------------------------------------------------------------------------------------------


def test_state_dir_must_be_absolute(tmp_path):
    assert gcfg.load({"state_dir": str(tmp_path)}).state_dir == str(tmp_path)
    assert gcfg.load({"state_dir": ""}).state_dir is None
    for bad in ("relative/dir", 5, ["x"]):
        assert any(e.startswith("state_dir:") for e in gcfg.check({"state_dir": bad}).errors)


def test_resolve_state_dir(tmp_path):
    assert gcfg.resolve_state_dir(gcfg.load({}), tmp_path) == tmp_path / "glasses"
    other = tmp_path / "elsewhere"
    assert gcfg.resolve_state_dir(gcfg.load({"state_dir": str(other)}), tmp_path) == other


@pytest.mark.parametrize("url", ["", "wss://glasses.example.com/ws", "wss://203.0.113.7:8443/ws"])
def test_public_url_ok(url):
    assert gcfg.load({"public_url": url}).public_url == url


@pytest.mark.parametrize(
    "url,normal",
    [
        ("wss://Glasses.Example.COM/ws", "wss://glasses.example.com/ws"),
        ("wss://glasses.example.com:443/ws", "wss://glasses.example.com/ws"),
        ("wss://glasses.example.com", "wss://glasses.example.com/"),
        ("wss://glasses.example.com/ws?", "wss://glasses.example.com/ws"),
        ("wss://[2001:DB8::0001]:8443/ws", "wss://[2001:db8::1]:8443/ws"),
        ("wss://glasses.example.com:65535/a/b", "wss://glasses.example.com:65535/a/b"),
    ],
)
def test_public_url_stored_normalised(url, normal):
    assert gcfg.load({"public_url": url}).public_url == normal


@pytest.mark.parametrize(
    "url",
    [
        "ws://glasses.example.com/ws",
        "https://glasses.example.com/ws",
        "wss:///ws",
        "wss://user:pw@glasses.example.com/ws",
        "wss://glasses.example.com/ws?x=1",
        "wss://glasses.example.com/ws#f",
        "glasses.example.com",
        7,
    ],
)
def test_public_url_rejected(url):
    errors = gcfg.check({"public_url": url}).errors
    assert any(e.startswith("public_url:") for e in errors), errors


def test_public_url_error_does_not_echo_credentials():
    errors = gcfg.check({"public_url": "wss://user:s3cr3t@glasses.example.com/ws"}).errors
    assert errors and not any("s3cr3t" in e for e in errors)


def test_lanes_ok_and_home_lane_prefix_normalised():
    cfg = gcfg.load({"lanes": ["assistant", "work"], "home_lane": "glasses:work"})
    assert cfg.lanes == ("assistant", "work")
    assert cfg.home_lane == "work"


def test_empty_lanes_allowed():
    # Allowlist = glasses.lanes + per-device lanes; an empty global list is valid.
    cfg = gcfg.load({"lanes": [], "home_lane": "assistant"})
    assert cfg.lanes == ()


@pytest.mark.parametrize(
    "lanes",
    [
        "assistant",
        ["Assistant"],
        ["1lane"],
        ["a" * 33],
        [""],
        ["work", "work"],
        [5],
        None,
    ],
)
def test_bad_lanes_rejected(lanes):
    errors = gcfg.check({"lanes": lanes}).errors
    assert any(e.startswith("lanes") for e in errors), errors


@pytest.mark.parametrize("home", ["Work", "glasses:", "telegram:work", 3])
def test_bad_home_lane_rejected(home):
    errors = gcfg.check({"home_lane": home}).errors
    assert any(e.startswith("home_lane:") for e in errors), errors


def test_home_lane_must_be_in_lanes():
    errors = gcfg.check({"lanes": ["work"], "home_lane": "assistant"}).errors
    assert any(e.startswith("home_lane:") and "lanes" in e for e in errors), errors


@pytest.mark.parametrize("enc", ["pem-urlescaped", "der-base64"])
def test_cert_encoding_ok(enc):
    assert gcfg.load({"client_cert_encoding": enc}).client_cert_encoding == enc


@pytest.mark.parametrize("enc", ["pem", "DER-BASE64", "", None])
def test_cert_encoding_rejected(enc):
    assert any(e.startswith("client_cert_encoding:") for e in gcfg.check({"client_cert_encoding": enc}).errors)


@pytest.mark.parametrize("hdr", ["", "X Client Cert", "X-Client-Cert:", "a" * 65, None, "X-Cert\r\nX: y"])
def test_cert_header_rejected(hdr):
    assert any(e.startswith("client_cert_header:") for e in gcfg.check({"client_cert_header": hdr}).errors)


@pytest.mark.parametrize(
    "value,ok",
    [
        (1, True),
        (280, True),
        (512, True),
        (0, False),
        (513, False),
        (100_000, False),
        (-1, False),
        (True, False),
        ("280", False),
        (1.5, False),
    ],
)
def test_long_threshold(value, ok):
    # T1.65: 1..512 (= msg.out.text budget 2048 B // 4 B per character); one precise message per mistake.
    errors = gcfg.check({"long_threshold": value}).errors
    assert (not errors) is ok, errors
    if not ok:
        (msg,) = errors
        assert msg.startswith("long_threshold: must be an integer 1-512") and "disables" not in msg


def test_long_answers_platform():
    assert gcfg.load({"long_answers": {"platform": "matrix"}}).long_answers_platform == "matrix"
    for bad in ({"platform": "Tele gram"}, {"platform": ""}, "telegram", {"platform": 1}):
        assert any(e.startswith("long_answers") for e in gcfg.check({"long_answers": bad}).errors)


@pytest.mark.parametrize("edge", ["nginx", 5, "/run/edge.sock"])
def test_edge_section_must_be_a_mapping(edge):
    # The old edge section (kind, tls_dir) is gone; the new one holds socket and user (test_config_edge.py).
    assert any(e.startswith("edge:") for e in gcfg.check({"edge": edge}).errors)


def test_unknown_nested_keys_warn():
    result = gcfg.check({"signer": {"user": "", "pubilc_dir": "red"}, "long_answers": {"x": 1}})
    assert result.errors == []
    assert "signer.pubilc_dir: unknown key, ignored — did you mean signer.public_dir?" in result.warnings
    assert "long_answers.<an unknown key of 1 character>: unknown key, ignored" in result.warnings


def test_unknown_keys_warn_but_stay_valid(no_allow_all):
    result = gcfg.check({"bridge_port": 18700, "token_file": "/x", "spike_log": "/y"})
    assert result.errors == []
    assert result.config is not None
    assert "<an unknown key of 10 characters>: unknown key, ignored" in result.warnings
    assert glasses.validate_config(pc({"bridge_port": 18700, "token_file": "/x"})) is True


def test_keys_injected_by_hermes_are_silent():
    # The gateway sets these in every PlatformConfig.extra before building the adapter
    # (gateway/run_adapters.py:_instantiate_adapter at d923a5eb48).
    result = gcfg.check({"group_sessions_per_user": True, "thread_sessions_per_user": False})
    assert result.errors == [] and result.warnings == []


def test_extra_must_be_mapping(no_allow_all):
    assert gcfg.check(["lanes"]).errors
    assert glasses.validate_config(pc(["lanes"])) is False


def test_all_errors_reported_at_once():
    errors = gcfg.check({"bridge_port": 0, "edge": {"user": "root"}, "long_answers": {"platform": "glasses"}}).errors
    keys = {e.split(":", 1)[0] for e in errors}
    assert {"bridge_port", "edge.user", "long_answers.platform"} <= keys


def test_config_error_carries_errors():
    with pytest.raises(gcfg.ConfigError) as exc:
        gcfg.load({"bridge_port": 0, "long_threshold": 0})
    assert len(exc.value.errors) == 2
    assert "bridge_port:" in str(exc.value)
