# SPDX-License-Identifier: Apache-2.0
"""Edge cases and hostile inputs for the glasses plugin config, on top of test_config.py.

Ports, defaults, types and strict strings.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

import glasses
from glasses import config as gcfg


def pc(extra=None, enabled=True):
    return SimpleNamespace(enabled=enabled, extra=extra)


def errs(extra, key):
    return [e for e in gcfg.check(extra).errors if e.split(":", 1)[0] == key or e.startswith(key + "[")]


# --------------------------------------------------------------------------------------------
# bridge_port
# --------------------------------------------------------------------------------------------


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
        "8766",
        " 8766",
        "0x2000",
        "",
        b"8766",
        8766.0,
        8766.5,
        float("nan"),
        float("inf"),
        True,
        False,
        None,
        [8766],
        {"p": 1},
    ],
)
def test_bridge_port_values_rejected(value):
    assert errs({"bridge_port": value}, "bridge_port"), gcfg.check({"bridge_port": value}).errors


@pytest.mark.parametrize("value", [1, 1024, 8765, 8766, 65535])
def test_bridge_port_boundaries_accepted(value, no_allow_all):
    # No other port to differ from: 8765, the old default of the retired ``port``, is an ordinary value now.
    assert gcfg.check({"bridge_port": value}).errors == []
    assert glasses.validate_config(pc({"bridge_port": value})) is True


# --------------------------------------------------------------------------------------------
# missing keys / partial sections / wrong types
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(gcfg.DEFAULTS))
def test_each_key_missing_falls_back_to_default(key):
    full = {k: (dict(v) if isinstance(v, dict) else v) for k, v in gcfg.DEFAULTS.items()}
    del full[key]
    assert gcfg.load(full) == gcfg.load({})


def test_partial_nested_sections_merge_defaults():
    cfg = gcfg.load({"signer": {"public_dir": "/srv/pub"}, "long_answers": {}})
    assert (cfg.signer_user, cfg.signer_public_dir, cfg.long_answers_platform) == ("", "/srv/pub", "telegram")
    cfg = gcfg.load({"signer": None, "long_answers": None})
    assert (cfg.signer_public_dir, cfg.long_answers_platform) == ("", "telegram")


@pytest.mark.parametrize(
    "key,value",
    [
        ("edge", "edge"),
        ("edge", ["socket"]),
        ("edge", 1),
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
        ("signer", "signer"),
        ("signer", ["signer"]),
        ("signer", 1),
    ],
)
def test_wrong_types_rejected_with_key_named(key, value, no_allow_all):
    result = gcfg.check({key: value})
    assert any(e.split(":", 1)[0].split("[")[0].split(".")[0] == key for e in result.errors), result.errors
    assert glasses.validate_config(pc({key: value})) is False


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("edge", "socket", 1),
        ("edge", "socket", "rel/edge.sock"),
        ("edge", "socket", ["/run/edge.sock"]),
        ("edge", "user", 1),
        ("edge", "user", "Root"),
        ("edge", "user", ["edge"]),
        ("signer", "user", 1),
        ("signer", "user", ["signer"]),
        ("signer", "public_dir", 1),
        ("signer", "public_dir", "rel/dir"),
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


@pytest.mark.parametrize("extra", ["", "lanes: [a]", 0, 1, True, ["lanes"], ("lanes",), 3.5])
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
    extra = {"bridge_port": 18700, "long_answers": {"platform": "matrix"}, "lanes": ["work"], "home_lane": "work"}
    snapshot = repr(extra)
    assert glasses.validate_config(pc(extra)) is True
    assert repr(extra) == snapshot


def test_defaults_are_not_shared_mutable_state():
    cfg = gcfg.load({})
    assert isinstance(cfg.lanes, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.bridge_port = 1  # frozen dataclass
    gcfg.check({"long_answers": {"platform": "matrix"}})
    assert gcfg.DEFAULTS["long_answers"] == {"platform": "telegram"}


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


@pytest.mark.parametrize(
    "key,value",
    [("user", "signer\n"), ("user", "signer\r\n"), ("user", "signer\u2028"), ("socket", "/run/signer.sock\n")],
)
def test_a_signer_string_key_with_a_line_break_is_refused(key, value):
    # Python's re "$" matches before a final newline: the pattern alone let "signer\n" through.
    assert errs({"signer": {key: value}}, f"signer.{key}"), f"{value!r} accepted"
