# SPDX-License-Identifier: Apache-2.0
"""Gateway-facing config contract of the glasses plugin.

Auth-key errors never echo the value and match key names case-sensitively like Hermes; gateway allow-all
values that Hermes ignores are not violations. The ``test_contract_*`` tests pin what the plugin accepts
against the byte budgets of the envelope schema (protocol/README.md), so the long-answer router, the config
schema and the codec must enforce it.
"""

from __future__ import annotations

import pytest

import glasses.config as gcfg

# protocol/README.md: UTF-8 byte limits of msg.out
MSG_OUT_TEXT_MAX_B = 2048
MSG_OUT_TEXT_WITH_LONGREF_MAX_B = 512
FULLTEXT_MAX_B = 4096


def test_auth_key_error_never_echoes_the_value():
    r = gcfg.check({"allowed_users": "secret-dev-xyz", "allow_from": ["secret-dev-abc"]})
    assert r.config is None and len(r.errors) == 2
    assert not any("secret-dev" in e for e in r.errors)


def test_auth_keys_are_case_sensitive_like_hermes():
    # Hermes reads the lower-case keys only; an upper-case twin is inert and just warned about.
    r = gcfg.check({"ALLOWED_USERS": "*"})
    assert r.errors == [] and r.warnings == ["ALLOWED_USERS: unknown key, ignored"]


@pytest.mark.parametrize("value", ["on", "enabled", "y"])
def test_gateway_allow_all_values_hermes_ignores_are_not_violations(value):
    # Hermes' _TRUTHY is {true, 1, yes} (gateway/authz_mixin.py:28); other values do not open the gate.
    assert gcfg.gate_violations(lambda n: {"GATEWAY_ALLOW_ALL_USERS": value}.get(n, "")) == []


def test_contract_max_message_length_exceeds_every_msg_out_field_budget():
    # The gateway hands the adapter replies of up to MAX_MESSAGE_LENGTH characters in one send() call.
    # Even pure ASCII at that length is over msg.out.text (2048 B) and longRef.fullText (4096 B), so the
    # long-answer router MUST cut/route every reply above the text budget; send() can never put a
    # gateway reply into a msg.out frame verbatim (requirement for T1.41/T1.07, not a T1.04 defect).
    n = gcfg.MAX_MESSAGE_LENGTH
    assert n == 10752
    for ch, per_char in (("a", 1), ("ж", 2), ("€", 3), ("\U0001f600", 4)):
        size = len((ch * n).encode("utf-8"))
        assert size == n * per_char
        assert size > MSG_OUT_TEXT_MAX_B and size > FULLTEXT_MAX_B


def test_contract_long_threshold_can_exceed_msg_out_text_budget():
    # T1.65 closed this gap: long_threshold is capped at 512 characters (2048 B // 4 B per character), so a
    # head below the threshold always fits msg.out.text; above 512 (e.g. 5000) the config is refused.
    # 1100 Cyrillic chars = 2200 B would not fit msg.out.text: is_long() also flags > 2048 UTF-8 bytes.
    r = gcfg.check({"long_threshold": 5000})
    assert r.config is None and [e.split(":", 1)[0] for e in r.errors] == ["long_threshold"]
    assert gcfg.MAX_LONG_THRESHOLD * 4 <= MSG_OUT_TEXT_MAX_B
    assert len(("ж" * 1100).encode("utf-8")) > MSG_OUT_TEXT_MAX_B
    assert gcfg.is_long("ж" * 1100, 100_000) is True
    # The default 280 is safe even for 4-byte characters without a longRef...
    assert MSG_OUT_TEXT_MAX_B >= 280 * 4
    # ...but a 280-char emoji/CJK head does not fit next to a longRef (512 B): the router truncates
    # the head on a code-point boundary when it attaches a longRef.
    assert MSG_OUT_TEXT_WITH_LONGREF_MAX_B < 280 * 4
