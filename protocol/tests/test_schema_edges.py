# SPDX-License-Identifier: Apache-2.0
"""Edge cases of protocol/envelope.schema.json: timestamp ranges, line terminators,
seq on keepalives, longRef channel/profile, hud.state, meetings, bidi controls. Shared helpers: test_schema.py.
"""

import re

import pytest

from test_schema import ULID, VALID_BODIES, assert_invalid, assert_valid, frame

# timestamps: real ranges


@pytest.mark.parametrize(
    "ts",
    [
        "2026-01-31T00:00:00Z",
        "2026-02-29T23:59:59Z",
        "2026-04-30T12:00:00Z",
        "2026-12-31T23:59:59.999999999Z",
        "2026-10-03T18:00:00+14:00",
        "2026-10-03T18:00:00-14:00",
        "2026-10-03T18:00:00+13:59",
        "2026-10-03T18:00:00+00:00",
    ],
)
def test_timestamp_ranges_accept(validator, ts):
    assert_valid(validator, frame("ping", ts=ts))


@pytest.mark.parametrize(
    "ts",
    [
        "2026-13-01T00:00:00Z",
        "2026-00-10T00:00:00Z",
        "2026-10-00T00:00:00Z",
        "2026-10-32T00:00:00Z",
        "2026-04-31T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-10-03T24:00:00Z",
        "2026-10-03T18:60:00Z",
        "2026-10-03T18:00:60Z",
        "2026-10-03T18:00:61Z",
        "2026-10-03T18:00:00+25:00",
        "2026-10-03T18:00:00+14:01",
        "2026-10-03T18:00:00-15:00",
        "2026-10-03T18:00:00+02:60",
        "2026-10-03T18:00:00Z\r",
        "2026-10-03T18:00:00Z\u2028",
    ],
)
def test_timestamp_ranges_reject(validator, ts):
    assert_invalid(validator, frame("ping", ts=ts))
    assert_invalid(validator, frame("approval.request", body=dict(VALID_BODIES["approval.request"], expiresAt=ts)))
    assert_invalid(validator, frame("welcome", body=dict(VALID_BODIES["welcome"], serverTime=ts)))


# Java-style line terminators after an anchored pattern


@pytest.mark.parametrize("suffix", ["\n", "\r", "\r\n", "\u0085", "\u2028", "\u2029"])
def test_line_terminator_after_anchored_value_rejected(validator, suffix):
    assert_invalid(validator, frame("msg.in", lane="work" + suffix))
    assert_invalid(validator, frame("ping", id=ULID[:-1] + suffix))
    assert_invalid(validator, frame("skill.cancel", body={"callId": "c1" + suffix}))


# seq on keepalives / error


@pytest.mark.parametrize("type_", ["ping", "pong"])
def test_keepalive_never_carries_seq(validator, type_):
    assert_valid(validator, frame(type_))
    assert_invalid(validator, frame(type_, seq=1))


def test_error_seq_optional(validator):
    assert_valid(validator, frame("error"))
    assert_valid(validator, frame("error", body={"code": "outbox_overflow", "message": "dropped 3"}, seq=9))


# longRef channel / profile


@pytest.mark.parametrize("channel", ["telegram", "matrix", "slack", "a", "x" * 32, "my_chat-2"])
def test_long_ref_channel_is_platform_pattern(validator, channel):
    lr = {"id": ULID, "channel": channel, "profile": "work", "status": "pending"}
    assert_valid(validator, frame("msg.out", body=dict(VALID_BODIES["msg.out"], longRef=lr)))


@pytest.mark.parametrize("channel", ["", "Telegram", "2tg", "x" * 33, "tele gram", "telegram\n", "telegram\r", "tg:1"])
def test_long_ref_channel_rejects(validator, channel):
    lr = {"id": ULID, "channel": channel, "profile": "work", "status": "pending"}
    assert_invalid(validator, frame("msg.out", body=dict(VALID_BODIES["msg.out"], longRef=lr)))


@pytest.mark.parametrize("profile", ["default", "work", "2nd", "0", "a" * 64, "my_profile-1", "9-a"])
def test_long_ref_profile_matches_hermes_rule(validator, profile):
    # Hermes PROFILE_ID_RE = ^[a-z0-9][a-z0-9_-]{0,63}$ (hermes_constants.py at the pinned clone)
    lr = {"id": ULID, "channel": "telegram", "profile": profile, "status": "sent"}
    assert_valid(validator, frame("msg.out", body=dict(VALID_BODIES["msg.out"], longRef=lr)))


@pytest.mark.parametrize("profile", ["", "_x", "-x", "Work", "a" * 65, "a.b", "a b", "work\n", "wörk"])
def test_long_ref_profile_rejects(validator, profile):
    lr = {"id": ULID, "channel": "telegram", "profile": profile, "status": "sent"}
    assert_invalid(validator, frame("msg.out", body=dict(VALID_BODIES["msg.out"], longRef=lr)))


# hud.state longChannel: the long-answer channel (docs/architecture.md section 2.2)


@pytest.mark.parametrize("value", ["telegram", "matrix", "none"])
def test_hud_state_long_channel(validator, value):
    assert_valid(validator, frame("hud.state", body=dict(VALID_BODIES["hud.state"], longChannel=value)))


@pytest.mark.parametrize("value", ["", "None", None, 0, "x" * 33])
def test_hud_state_long_channel_rejects(validator, value):
    assert_invalid(validator, frame("hud.state", body=dict(VALID_BODIES["hud.state"], longChannel=value)))


# meeting.start langHints optional / empty


def test_meeting_lang_hints_optional_and_may_be_empty(validator):
    start = dict(VALID_BODIES["meeting.start"])
    assert_valid(validator, frame("meeting.start", body=dict(start, langHints=[])))
    del start["langHints"]
    assert_valid(validator, frame("meeting.start", body=start))
    assert_invalid(validator, frame("meeting.start", body=dict(start, langHints=["en"] * 2)))
    assert_invalid(validator, frame("meeting.start", body=dict(start, langHints=[f"l{c}" for c in "abcdefghi"])))


def test_meeting_audio_is_opus_only(validator):
    body = dict(VALID_BODIES["meeting.audio"], mime="audio/L16;rate=16000")
    assert_invalid(validator, frame("meeting.audio", body=body))


def test_welcome_max_audio_sec_matches_clip_cap(validator):
    limits = {"maxFrame": 65536, "maxAudioSec": 31}
    assert_invalid(validator, frame("welcome", body=dict(VALID_BODIES["welcome"], limits=limits)))


# bidi controls in approval / clarify display fields

BIDI_CONTROLS = ["\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
                 "\u2066", "\u2067", "\u2068", "\u2069"]  # fmt: skip


def _display_frames(s):
    yield frame("approval.request", body=dict(VALID_BODIES["approval.request"], summary=s))
    yield frame("approval.request", body=dict(VALID_BODIES["approval.request"], detail=s))
    yield frame("clarify.request", body=dict(VALID_BODIES["clarify.request"], question=s))
    yield frame("clarify.request", body=dict(VALID_BODIES["clarify.request"], choices=["A", s]))


@pytest.mark.parametrize("ch", BIDI_CONTROLS, ids=[f"U+{ord(c):04X}" for c in BIDI_CONTROLS])
def test_bidi_controls_rejected_in_card_display_fields(validator, ch):
    for f in _display_frames("Run: " + ch + "rm -rf ~"):
        assert_invalid(validator, f)


def test_card_display_fields_accept_rtl_scripts_and_emoji(validator):
    for s in ("\u05e9\u05dc\u05d5\u05dd", "\u0645\u0631\u062d\u0628\u0627", "\U0001f469\u200d\U0001f4bb", "Привет"):
        for f in _display_frames(s):
            assert_valid(validator, f)


def test_bidi_guard_covers_exactly_the_bidi_control_set(schema):
    # Unicode Bidi_Control property: U+061C, U+200E, U+200F, U+202A..U+202E, U+2066..U+2069
    import unicodedata

    pat = re.compile(schema["$defs"]["body.approval.request"]["properties"]["summary"]["not"]["pattern"])
    hits = {cp for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF and pat.search(chr(cp))}
    assert hits == {ord(c) for c in BIDI_CONTROLS}
    assert all(unicodedata.category(chr(cp)) == "Cf" for cp in hits)
