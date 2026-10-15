# SPDX-License-Identifier: Apache-2.0
"""Limits of the signer socket client: a full-cap answer is read to the signer's close (a byte after it is refused,
a held connection times out), timeouts are finite and in range at construction, both uids are exact ``int`` >= 0, and
a refused enrol answer leaves its token in no traceback local."""

from __future__ import annotations

import os
import socket
import time
import traceback
from pathlib import Path
from typing import Any

import fake_signer
import pytest

from glasses import signer_client
from glasses import signer_protocol as proto
from glasses.signer_client import SignerClient, Timeouts

from test_signer_client_edges import assert_clean, client, raw_peer, window_close_answer
from test_signer_client_failures import CODE, SIG, SPKI, TOKEN, enrol_answer


@pytest.mark.parametrize("pause_s", [0.0, 0.2], ids=["same-read", "late"])
def test_an_answer_of_exactly_the_cap_followed_by_one_more_byte_is_refused(pause_s):
    def exact_then_extra(conn: socket.socket, line: bytes) -> None:
        body = window_close_answer(line)
        conn.sendall(body[:-2] + b" " * (proto.ANSWER_CAP - len(body)) + b"}\n")
        time.sleep(pause_s)
        conn.sendall(b"x")

    with raw_peer(exact_then_extra) as (path, _seen), pytest.raises(proto.AnswerRejected) as info:
        client(path).window_close()
    assert_clean(info.value)


def test_an_answer_of_exactly_the_cap_on_a_held_connection_is_a_timeout():
    def exact_then_held(conn: socket.socket, line: bytes) -> None:
        body = window_close_answer(line)
        conn.sendall(body[:-2] + b" " * (proto.ANSWER_CAP - len(body)) + b"}\n")
        time.sleep(1.5)

    with raw_peer(exact_then_held) as (path, _seen), pytest.raises(signer_client.SignerTimeout) as info:
        client(path, timeouts=Timeouts(op_s=0.5)).window_close()
    assert_clean(info.value)


@pytest.mark.parametrize("value", [float("inf"), 1e300, float("nan"), 0.0, -1.0, 600.5], ids=str)
def test_a_timeout_out_of_range_is_refused_at_construction(value: float):
    for name in ("connect_s", "op_s", "enrol_s"):
        with pytest.raises(ValueError):
            Timeouts(**{name: value})


@pytest.mark.parametrize("value", [True, "1", None], ids=str)
def test_a_timeout_that_is_not_a_number_is_refused_at_construction(value: Any):
    with pytest.raises(TypeError):
        Timeouts(op_s=value)


@pytest.mark.parametrize("uid", [True, 1.0, -1, type("IntSub", (int,), {})(1)], ids=["bool", "float", "neg", "sub"])
def test_a_signer_uid_that_is_not_an_exact_int_is_refused(uid: Any, tmp_path: Path):
    with pytest.raises(TypeError):
        SignerClient(tmp_path / "s.sock", uid)


def test_a_peer_uid_that_is_not_an_exact_int_is_refused(tmp_path: Path):
    values: list[Any] = [True, False, 1.0, "1", -1, os.geteuid]
    for uid in values:
        with pytest.raises(TypeError):
            SignerClient(tmp_path / "s.sock", 1, peer_uid=uid)


def test_a_refused_enrol_answer_leaves_the_token_in_no_traceback_local():
    with fake_signer.running() as fake:
        fake.scripts["enrol"] = fake_signer.Script(raw=lambda rid: enrol_answer(rid, extra=1))
        with pytest.raises(proto.AnswerSchemaError) as info:
            client(fake.socket_path).enrol(SPKI, CODE, SIG)
    assert_clean(info.value, TOKEN)
    frames = traceback.TracebackException.from_exception(info.value, capture_locals=True)
    assert TOKEN not in "".join(frames.format())
