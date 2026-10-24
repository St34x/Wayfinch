# SPDX-License-Identifier: Apache-2.0
"""A delivery to several devices is all or nothing: ``Store.deliver`` runs the purges and every recipient's row in one
transaction. A store failure for device B after A's row was written leaves no row for A (``Outbox.send`` raises before
``_write``), so the gateway's retry gives A the answer once, numbered from 1 and written live: no duplicate with a new
``seq`` and envelope ``id``, and no gap below a live ``seq`` that a cumulative ``ack`` would delete unseen."""

from __future__ import annotations

import pytest
from outbox_helpers import TEXT, A, B, C, Dev, FakeLink, answer, outbox, rows, run, seqs


def _failing_for(device_id: str, fail: list[bool]):
    good = answer()

    def build(owner: str, seq: int) -> str:
        if owner == device_id and fail[0]:
            raise OSError("disk full")
        return good(owner, seq)

    return build


async def _welcomed(box, device_id: str) -> FakeLink:
    link = FakeLink()
    await box.hello(device_id, 0, link)
    return link


def test_a_delivery_that_fails_for_the_second_device_stores_nothing_for_the_first(tmp_path):
    box = outbox(tmp_path)
    devices = [Dev(A), Dev(B)]

    async def main() -> tuple[list[int], list[int], list[dict]]:
        link_a = await _welcomed(box, A)
        with pytest.raises(OSError):
            await box.send(lambda: devices, "work", _failing_for(B, [True]))
        await box.close()
        return seqs(tmp_path, A), seqs(tmp_path, B), link_a.sent

    stored_a, stored_b, live_a = run(main())
    assert stored_b == [] and stored_a == [] and live_a == []  # nothing stored anywhere: the retry is clean


def test_a_retry_after_a_failed_delivery_gives_the_first_device_one_answer_and_no_gap(tmp_path):
    box = outbox(tmp_path)
    devices = [Dev(A), Dev(B)]
    fail = [True]

    async def main() -> tuple[list[int], list[int]]:
        link_a = await _welcomed(box, A)
        build = _failing_for(B, fail)
        with pytest.raises(OSError):
            await box.send(lambda: devices, "work", build)
        fail[0] = False  # the gateway retries the same answer
        assert await box.send(lambda: devices, "work", build) is True
        await box.close()
        return seqs(tmp_path, A), [f["seq"] for f in link_a.sent]

    stored, live = run(main())
    assert stored == [1] and live == [1], (stored, live, TEXT)  # one copy, numbered from 1, written live


def _purged(state) -> list[str]:
    return [str(device) for (device, _at) in rows(state, "purged")]


def test_the_purge_of_a_revoked_device_is_part_of_the_delivery_and_lands_with_the_retry(tmp_path):
    box = outbox(tmp_path)
    fail = [True]

    async def main() -> tuple[list[int], list[str], list[int], list[str]]:
        await box.send(lambda: [Dev(C)], "work", answer("old"))  # C holds a row, then is revoked
        devices = [Dev(A), Dev(B), Dev(C, revoked_at="2026-10-21T00:00:00Z")]
        build = _failing_for(B, fail)
        with pytest.raises(OSError):
            await box.send(lambda: devices, "work", build)
        held, purged = seqs(tmp_path, C), _purged(tmp_path)
        fail[0] = False
        assert await box.send(lambda: devices, "work", build) is True
        await box.close()
        return held, purged, seqs(tmp_path, C), _purged(tmp_path)

    held, purged_before, held_after, purged_after = run(main())
    assert held == [1] and purged_before == []  # the failed delivery rolled its purge back with its rows
    assert held_after == [] and purged_after == [C]  # the retry purges it
