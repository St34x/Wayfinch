# SPDX-License-Identifier: Apache-2.0
"""The outbox store (T1.09b): seq per device, ack, ``hello`` (``resumeFrom``, the counter floor), TTL, cap and
overflow, pinned rows, restart over the same file, the file mode, and an event loop that stays free while SQLite
stalls. Every check of the file goes through a separate ``sqlite3`` connection."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import stat
import threading
import time

from outbox_helpers import TEXT, A, B, Clock, Dev, FakeLink, answer, outbox, rows, run, seqs, store

from glasses import outbox as outbox_module

MAX = 2**53 - 1


def test_the_file_is_created_0600_and_each_device_counts_its_own_seq(tmp_path):
    s = store(tmp_path / "state")
    s.add(A, lambda seq: answer()(A, seq))
    s.add(B, lambda seq: answer()(B, seq))
    s.add(A, lambda seq: answer()(A, seq))
    mode = stat.S_IMODE((tmp_path / "state" / outbox_module.FILE_NAME).stat().st_mode)
    assert mode == 0o600 and stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
    assert seqs(tmp_path / "state", A) == [1, 2] and seqs(tmp_path / "state", B) == [1]


def test_hello_gives_the_first_queued_seq_as_resume_from_and_drops_what_it_acks(tmp_path):
    s = store(tmp_path)
    for _ in range(6):
        s.add(A, lambda seq: answer()(A, seq))
    resume, queued, last, refused = s.hello(A, 2)
    assert (resume, [seq for seq, _ in queued], last, refused) == (3, [3, 4, 5, 6], 6, False)
    assert seqs(tmp_path, A) == [3, 4, 5, 6]


def test_resume_from_skips_rows_hello_itself_expires_and_an_empty_outbox_gives_the_next_seq(tmp_path):
    s = store(tmp_path, Clock())
    for _ in range(6):
        s.add(A, lambda seq: answer()(A, seq))
    with sqlite3.connect(tmp_path / outbox_module.FILE_NAME) as db:  # rows 3 and 4 aged 25 h after the last add
        db.execute("UPDATE rows SET created_at=created_at-90000 WHERE seq IN (3, 4)")
    assert seqs(tmp_path, A) == [1, 2, 3, 4, 5, 6]  # nothing expired before the hello
    assert s.hello(A, 2)[0] == 5 and seqs(tmp_path, A) == [5, 6]
    assert s.hello(A, 6)[:2] == (7, [])  # empty: counter + 1


def test_the_counter_never_goes_below_last_acked_seq(tmp_path):
    s = store(tmp_path)
    for _ in range(10):
        s.add(A, lambda seq: answer()(A, seq))
    assert s.hello(A, 50)[0] == 51 and s.add(A, lambda seq: answer()(A, seq))[0][0] == 51


def test_last_acked_seq_at_the_schema_maximum_gives_a_valid_resume_from_and_no_more_rows(tmp_path):
    s = store(tmp_path)
    assert s.hello(A, MAX)[:2] == (MAX, []) and s.add(A, lambda seq: answer()(A, seq)) == []


def test_an_ack_deletes_the_rows_up_to_it_only(tmp_path):
    s = store(tmp_path)
    for _ in range(4):
        s.add(A, lambda seq: answer()(A, seq))
    s.add(B, lambda seq: answer()(B, seq))
    s.ack(A, 2)
    assert seqs(tmp_path, A) == [3, 4] and seqs(tmp_path, B) == [1]


def test_over_500_rows_the_oldest_unpinned_go_and_an_overflow_error_with_seq_is_stored(tmp_path):
    s = store(tmp_path)
    for _ in range(500):
        s.add(A, lambda seq: answer()(A, seq))
    with sqlite3.connect(tmp_path / outbox_module.FILE_NAME) as db:
        db.execute("UPDATE rows SET pinned=1 WHERE seq=1")  # a planted pinned card (no producer yet)
    stored = s.add(A, lambda seq: answer()(A, seq))
    assert [seq for seq, _ in stored] == [501, 502]
    error = json.loads(stored[1][1])
    assert error["type"] == "error" and error["seq"] == 502 and error["body"]["code"] == "outbox_overflow"
    kept = seqs(tmp_path, A)
    assert len(kept) == 500 and kept[0] == 1 and 2 not in kept and 3 not in kept and kept[-2:] == [501, 502]


def test_a_25_hour_old_row_goes_on_the_next_store_and_a_pinned_one_stays(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    s.add(A, lambda seq: answer()(A, seq))
    s.add(A, lambda seq: answer()(A, seq))
    with sqlite3.connect(tmp_path / outbox_module.FILE_NAME) as db:
        db.execute("UPDATE rows SET pinned=1 WHERE seq=2")
    clock.now += 25 * 3600
    s.add(A, lambda seq: answer()(A, seq))
    assert seqs(tmp_path, A) == [2, 3]


def test_after_a_restart_the_stored_rows_come_back_in_order_and_the_counter_continues(tmp_path):
    first = store(tmp_path)
    for _ in range(3):
        first.add(A, lambda seq: answer()(A, seq))
    first.close()
    second = store(tmp_path)  # a new Store over the same file: the plugin restarted
    resume, queued, _, _ = second.hello(A, 0)
    assert resume == 1 and [json.loads(frame)["seq"] for _, frame in queued] == [1, 2, 3]
    assert second.add(A, lambda seq: answer()(A, seq))[0][0] == 4


def test_an_existing_file_with_a_wider_mode_is_refused(tmp_path):
    path = tmp_path / outbox_module.FILE_NAME
    path.write_bytes(b"")
    path.chmod(0o644)
    try:
        store(tmp_path).add(A, lambda seq: answer()(A, seq))
    except outbox_module.OutboxError:
        return
    raise AssertionError("a 0644 outbox file was used")


def test_a_stalled_sqlite_call_does_not_stall_the_loop_and_runs_on_the_outbox_worker(tmp_path, monkeypatch):
    box = outbox(tmp_path)
    threads: list[str] = []
    real_add = outbox_module.Store._add

    def stalled(self, db, device_id, build):
        threads.append(threading.current_thread().name)
        time.sleep(2)
        return real_add(self, db, device_id, build)

    monkeypatch.setattr(outbox_module.Store, "_add", stalled)

    async def main() -> float:
        sending = asyncio.ensure_future(box.send(lambda: [Dev(A)], "work", answer()))
        await asyncio.sleep(0.05)
        start = time.monotonic()
        await asyncio.sleep(0.01)  # the loop keeps running timers and other work while the worker is stuck
        took = time.monotonic() - start
        assert await sending is True
        await box.close()
        return took

    assert run(main()) < 0.1
    assert threads == [threads[0]] and threads[0].startswith("glasses-outbox")


def test_no_message_text_is_logged_on_any_store_path(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    box = outbox(tmp_path)
    link = FakeLink()

    async def main() -> None:
        await box.hello(A, 0, link)
        for _ in range(502):
            await box.send(lambda: [Dev(A), Dev(B, revoked_at="2026-10-21T00:00:00Z")], "work", answer())
        await asyncio.wrap_future(box.ack_soon(A, 3))
        await box.purge(A)
        await box.close()

    run(main())
    assert TEXT not in caplog.text and all(TEXT not in repr((r.msg, r.args)) for r in caplog.records)
    assert rows(tmp_path) == []
