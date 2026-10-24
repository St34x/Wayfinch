# SPDX-License-Identifier: Apache-2.0
"""The pure outbound core (T1.09b): the ``glasses:<lane>`` chat id, the recipients, the 2048 B ``text`` cut and the
frames, each encoded by the real codec."""

from __future__ import annotations

import json

import pytest
from outbox_helpers import TS, ULID, Dev
from server_helpers import SCHEMA

from glasses import outbound
from glasses.config import load
from glasses.envelope_encode import encode

EMOJI = "\U0001f600"  # 4 UTF-8 bytes


def cfg(lanes: list[str]):
    return load({"lanes": lanes, "home_lane": lanes[0] if lanes else ""})


def test_a_5000_byte_reply_with_an_emoji_across_the_cut_point_is_cut_to_valid_utf8_under_2048_bytes():
    text = "a" * 2043 + EMOJI + "b" * (5000 - 2047)  # the emoji covers bytes 2043-2046, across the 2045-byte point
    cut = outbound.cut(text)
    raw = cut.encode()
    assert len(raw) <= 2048 and cut.endswith("\u2026") and raw.decode() == cut
    assert cut == "a" * 2043 + "\u2026"  # the emoji did not fit whole: it is dropped, never split
    frame = outbound.msg_out(cut, "glasses:work", None, (1, ULID, TS))
    assert json.loads(encode(frame, SCHEMA))["body"]["text"] == cut  # the codec's encode accepts it


def test_a_reply_of_exactly_2048_bytes_is_sent_unchanged_and_one_more_byte_is_cut():
    exact = "x" * 2044 + EMOJI
    assert outbound.cut(exact) == exact
    over = outbound.cut(exact + "y")  # 2049 bytes: the emoji ends past byte 2045, so it goes whole
    assert over == "x" * 2044 + "\u2026" and len(over.encode()) == 2047


def test_a_lone_surrogate_becomes_a_question_mark_so_the_codec_accepts_the_frame():
    cut = outbound.cut("ok\ud800ok")
    assert cut == "ok?ok"
    encode(outbound.msg_out(cut, "glasses:work", None, (1, ULID, TS)), SCHEMA)


@pytest.mark.parametrize(
    ("chat_id", "lanes", "lane"),
    [
        ("glasses:work", ["work"], "work"),
        ("glasses:other", ["work"], None),
        ("glasses:", ["work"], None),
        ("telegram:work", ["work"], None),
        (None, ["work"], None),
        ("glasses:any", [], "any"),
    ],
)
def test_the_chat_id_must_name_a_configured_lane(chat_id, lanes, lane):
    assert outbound.lane_of_chat(chat_id, cfg(lanes)) == lane


def test_recipients_are_the_active_devices_holding_the_lane_and_every_revoked_one_is_set_apart():
    devices = [Dev("a"), Dev("b", ("home",)), Dev("c", revoked_at=TS), Dev("d", ("home",), revoked_at=TS)]
    assert outbound.recipients(devices, "work") == (["a"], ["c", "d"])


def test_the_answer_frame_carries_seq_kind_final_chat_id_and_corr_only_when_given():
    framed = outbound.msg_out("t", "glasses:work", ULID, (7, ULID, TS))
    assert framed["seq"] == 7 and framed["corr"] == ULID
    assert framed["body"] == {"kind": "answer", "text": "t", "chatId": "glasses:work", "final": True}
    assert "corr" not in outbound.msg_out("t", "glasses:work", None, (7, ULID, TS))
    error = outbound.overflow_error((8, ULID, TS))
    assert error["seq"] == 8 and error["body"]["code"] == "outbox_overflow" and SCHEMA.is_valid(error)
