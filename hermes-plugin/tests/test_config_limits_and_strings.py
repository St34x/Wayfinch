# SPDX-License-Identifier: Apache-2.0
"""Glasses limits and strict strings: one message cap, long-answer threshold, full-match strings, strict
``public_url``. No Hermes import (conftest.py enforces it). The access gate: test_config_access_gate.py.
"""

from __future__ import annotations

import ast
import json

import pytest

import glasses
from glasses import config as gcfg

from conftest import PLUGIN_DIR
from test_config_access_gate import _adapter_class

# --------------------------------------------------------------------------------------------
# 5. one message cap; the gateway does not split what the planned long-answer router must see
# --------------------------------------------------------------------------------------------


def test_message_cap_is_one_constant_and_byte_safe():
    assert gcfg.MAX_MESSAGE_LENGTH == (65_536 - 1_024) // 6 == 10_752
    assert gcfg.MAX_MESSAGE_LENGTH >= 8192  # 8192 chars reach send() unsplit (for the planned long-answer router)
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
        ({"signer": {"public_dir": "/srv/pub\n"}}, "signer.public_dir"),
        ({"signer": {"socket": "/run/signer.sock "}}, "signer.socket"),
        ({"signer": {"user": " signer"}}, "signer.user"),
        ({"client_cert_encoding": "der-base64\n"}, "client_cert_encoding"),
        ({"client_cert_header": " X-Client-Cert"}, "client_cert_header"),
        ({"lanes": ["work "], "home_lane": ""}, "lanes"),
        ({"lanes": [], "home_lane": " work"}, "home_lane"),
        ({"long_answers": {"platform": "telegram "}}, "long_answers.platform"),
        ({"edge": {"socket": "/run/edge.sock\n"}}, "edge.socket"),
        ({"edge": {"user": "edge-proxy\n"}}, "edge.user"),
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
