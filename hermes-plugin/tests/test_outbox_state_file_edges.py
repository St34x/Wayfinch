# SPDX-License-Identifier: Apache-2.0
"""Edges of the outbox file and counter (T1.09b): a corrupt, zero-byte, unwritable or symlinked ``outbox.sqlite`` at
``connect()``, mode 0600/0700, ``seq`` monotonic across a restart (also after everything was acked), ``resumeFrom``
beyond the last ``seq``, the 2^53-1 limit over a real session, the TTL boundary, and 600 answers with one device
offline."""

from __future__ import annotations

import os
import sqlite3
import stat

import aiohttp
import pytest
from edge_helpers import short_dir
from outbox_helpers import A, B, Clock, Dev, FakeLink, answer, outbox, rows, run, seqs
from outbox_session_helpers import hello, rig, served, until_pong
from signer_registry_helpers import publish

from glasses.outbox import CAP, FILE_NAME, MAX_SEQ, TTL_S, Outbox

import test_adapter
from test_adapter_edge_registry import _extra, _state, as_signer  # noqa: F401 - the fixture is used by name

adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument


def _connect(adapter_module, tmp_path, prepare) -> tuple[bool, list, int]:
    publish(tmp_path)
    prepare(_state(tmp_path) / FILE_NAME)
    with short_dir() as d:
        adapter = test_adapter.new_adapter(adapter_module, _extra(d / "edge.sock", tmp_path))

        async def main() -> bool:
            connected = await adapter.connect()
            await adapter.disconnect()
            return connected

        connected = run(main())
    path = _state(tmp_path) / FILE_NAME
    return connected, adapter.fatal_errors, stat.S_IMODE(path.stat().st_mode) if path.exists() else -1


def _garbage(path) -> None:
    path.write_bytes(b"this is not a database " * 40)
    path.chmod(0o600)


def _empty(path) -> None:
    path.touch(mode=0o600)
    path.chmod(0o600)


def _read_only(path) -> None:
    _empty(path)
    path.chmod(0o400)


def _symlink(path) -> None:
    target = path.parent / "elsewhere.sqlite"
    target.touch(mode=0o600)
    path.symlink_to(target)


@pytest.mark.parametrize("damage", [_garbage, _read_only, _symlink], ids=["garbage", "read-only", "symlink"])
def test_an_unusable_outbox_file_fails_connect_with_one_retryable_error_and_no_traceback(
    adapter_module,
    tmp_path,
    as_signer,  # noqa: F811 - the imported fixture
    damage,
    caplog,
):
    connected, fatal, _mode = _connect(adapter_module, tmp_path, damage)
    assert connected is False and len(fatal) == 1 and fatal[0][2] is True
    assert "Traceback" not in caplog.text


def test_a_zero_byte_outbox_file_of_mode_0600_is_a_fresh_outbox_and_connect_succeeds(
    adapter_module,
    tmp_path,
    as_signer,  # noqa: F811 - the imported fixture
):
    connected, fatal, mode = _connect(adapter_module, tmp_path, _empty)
    assert connected is True and fatal == [] and mode == 0o600


def test_a_fresh_state_dir_holds_the_outbox_at_0600_in_a_0700_dir_and_a_restart_keeps_the_modes(tmp_path):
    for _ in range(2):

        async def main(box: Outbox) -> None:
            await box.open()
            await box.send(lambda: [Dev(A)], "work", answer())
            await box.close()

        run(main(outbox(tmp_path / "state")))
        assert stat.S_IMODE(os.stat(tmp_path / "state" / FILE_NAME).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(tmp_path / "state").st_mode) == 0o700


def test_seq_continues_after_a_restart_also_when_everything_was_acked(tmp_path):
    first = outbox(tmp_path)

    async def one() -> None:
        for text in ("a", "b", "c"):
            await first.send(lambda: [Dev(A)], "work", answer(text))
        await first.hello(A, 3, FakeLink())  # acks 1-3: the table is empty, the counter must stay at 3
        await first.close()

    run(one())
    assert seqs(tmp_path, A) == [] and rows(tmp_path, "counters") == [(A, 3)]
    second = outbox(tmp_path)

    async def two() -> list[int]:
        link = FakeLink()
        await second.hello(A, 0, link)
        await second.send(lambda: [Dev(A)], "work", answer("d"))
        await second.close()
        return [f["seq"] for f in link.sent]

    assert run(two()) == [4]


def test_a_resume_from_beyond_the_last_seq_gives_no_replay_and_the_counter_follows_it(tmp_path):
    box = outbox(tmp_path)

    async def main() -> tuple[int, list[str], list[int]]:
        for text in ("a", "b", "c"):
            await box.send(lambda: [Dev(A)], "work", answer(text))
        link = FakeLink()
        resume, frames = await box.hello(A, 10, link)  # the phone claims seq 10, the counter is at 3
        await box.send(lambda: [Dev(A)], "work", answer("d"))
        await box.close()
        return resume, frames, [f["seq"] for f in link.sent]

    resume, frames, live = run(main())
    assert (resume, frames, live) == (11, [], [11]) and seqs(tmp_path, A) == [11]


def test_a_hello_at_2_pow_53_minus_1_keeps_the_session_up_and_later_answers_store_nothing(adapter_module, tmp_path):
    r = rig(adapter_module, tmp_path)

    async def main() -> tuple[list[dict], bool, list[str]]:
        async with served(r) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers())
            await ws.send_str(hello(MAX_SEQ))
            frames = await until_pong(ws, r.phone)
            welcome = [f for f in r.phone.wire if f["type"] == "welcome"]
            delivered = (await r.adapter.send("glasses:work", "late")).success
            frames += await until_pong(ws, r.phone)  # the session is still answering
            await ws.close()
            return welcome, delivered, [f["type"] for f in r.phone.wire]

    welcome, delivered, types = run(main())
    assert len(welcome) == 1 and "seq" not in welcome[0] and welcome[0]["body"]["resumeFrom"] == MAX_SEQ
    assert delivered is False and seqs(r.state, r.w.device.device_id) == [] and "msg.out" not in types


def test_a_row_exactly_24_hours_old_stays_and_one_a_millisecond_older_goes(tmp_path):
    clock = Clock()
    box = outbox(tmp_path, clock)

    async def main() -> tuple[list[int], list[int]]:
        await box.send(lambda: [Dev(A)], "work", answer("old"))
        clock.now += TTL_S
        await box.hello(B, 0, FakeLink())  # any hello expires every device's rows
        at_boundary = seqs(tmp_path, A)
        clock.now += 0.001
        await box.hello(B, 0, FakeLink())
        await box.close()
        return at_boundary, seqs(tmp_path, A)

    assert run(main()) == ([1], [])


def test_600_answers_with_one_device_offline_cap_it_at_500_with_one_overflow_row_and_cut_the_online_one_nowhere(
    tmp_path,
):
    box = outbox(tmp_path)

    async def main() -> list[dict]:
        online = FakeLink()
        await box.hello(A, 0, online)
        for number in range(600):
            await box.send(lambda: [Dev(A), Dev(B)], "work", answer(f"answer-{number}"))
        await box.close()
        return online.sent

    wire = run(main())
    seqs_on_wire = [f["seq"] for f in wire]
    assert seqs_on_wire == sorted(set(seqs_on_wire)) and sum(f["type"] == "msg.out" for f in wire) == 600
    kept = seqs(tmp_path, B)
    assert len(kept) <= CAP and len(rows(tmp_path)) <= 2 * CAP
    with sqlite3.connect(tmp_path / FILE_NAME) as db:
        kinds = [k for (k,) in db.execute("SELECT kind FROM rows WHERE device_id=? ORDER BY seq", (B,))]
    assert kinds.count("overflow") == 1 and kinds[-1] == "overflow" and kinds.count("answer") == len(kinds) - 1


def test_without_state_dir_the_outbox_is_created_under_hermes_home_glasses_at_0600(
    adapter_module,
    tmp_path,
    as_signer,  # noqa: F811 - the imported fixture
    monkeypatch,
):
    publish(tmp_path)
    home = tmp_path / "hermes-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    with short_dir() as d:
        extra = {k: v for k, v in _extra(d / "edge.sock", tmp_path).items() if k != "state_dir"}
        adapter = test_adapter.new_adapter(adapter_module, extra)

        async def main() -> bool:
            connected = await adapter.connect()
            await adapter.disconnect()
            return connected

        connected = run(main())
    created = home / "glasses" / FILE_NAME
    assert connected is True and created.is_file() and stat.S_IMODE(created.stat().st_mode) == 0o600
