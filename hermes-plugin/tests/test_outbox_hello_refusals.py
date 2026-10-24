# SPDX-License-Identifier: Apache-2.0
"""What ``Outbox.hello`` does with a refusal (T1.248): a device the registry lists as revoked at its ``hello`` is
dropped on the loop side as well (marked revoked, its inbound dedupe ids forgotten, no live link); a tombstone sweep
the ``hello`` waits for that cannot read the registry refuses that ``hello`` (``TryAgainLater``: over the real server
the session is closed ``1013`` with no ``welcome``, and the next ``hello`` replays the kept rows); and the registry
the ``hello`` checks is a required argument of ``Outbox``."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from outbox_helpers import A, Dev, FakeLink, answer, run, seqs, store
from outbox_session_helpers import hello, rig, served, until_pong

from glasses.outbox import Outbox
from glasses.server_auth import TryAgainLater

import test_adapter
from test_outbox_revoked_hello import REVOKED, _gated_hello
from test_revoked_open_session import _until_close

adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument


def test_a_hello_the_registry_refuses_drops_the_device_on_the_loop_side_too(tmp_path):
    registry, forgotten = [Dev(A)], []
    box = Outbox(store(tmp_path), forgotten.append, lambda: registry)

    async def main() -> tuple[list[str], FakeLink]:
        await box.send(lambda: registry, "work", answer("q1"))
        registry[:] = [Dev(A, revoked_at=REVOKED)]
        link = FakeLink()
        _resume, frames = await box.hello(A, 0, link)
        await box.send(lambda: [Dev(A)], "work", answer("late"))  # a stale registry copy: nothing reaches the link
        await box.close()
        return frames, link

    frames, link = run(main())
    assert frames == [] and link.sent == [] and A not in box._links
    assert box.is_revoked(A) is True and forgotten == [A]  # the frame check now refuses it; its dedupe ids are gone


def test_a_hello_waiting_for_a_sweep_that_cannot_read_the_registry_replays_nothing(tmp_path):
    state = {"broken": False}
    registry = [Dev(A)]

    def devices() -> list[Dev]:
        if state["broken"]:
            raise OSError(5, "Input/output error")
        return registry

    box = Outbox(store(tmp_path), [].append, devices)
    hook = box.on_revoked(devices)  # the production hook of server_auth.note_revoked

    async def main() -> FakeLink:
        for text in ("q1", "q2", "q3"):
            await box.send(devices, "work", answer(text))
        release, inside = _gated_hello(box)
        link = FakeLink()
        replay = asyncio.create_task(box.hello(A, 0, link))
        await asyncio.to_thread(inside.wait, 5)  # the hello's read saw A active
        state["broken"] = True  # the sweep the refusal starts cannot read the registry
        hook(None)  # a tombstone refusal lands while the replay read is in flight
        release.set()
        with pytest.raises(TryAgainLater):  # refused, not welcomed with nothing: the server closes it 1013
            await replay
        await box.close()
        return link

    link = run(main())
    assert link.sent == [] and A not in box._links
    assert seqs(tmp_path, A) == [1, 2, 3]  # nothing was purged either: only this hello is refused


def test_a_hello_whose_sweep_cannot_read_the_registry_is_closed_1013_and_the_next_hello_replays(
    adapter_module, tmp_path
):
    r = rig(adapter_module, tmp_path)
    state = {"broken": False}

    def sweep_devices() -> list:
        if state["broken"]:
            raise OSError(5, "Input/output error")
        return r.w.registry.list()

    hook = r.box.on_revoked(sweep_devices)

    async def main() -> tuple[list[str], int | None, list[str]]:
        async with served(r, on_revoked=hook) as port, aiohttp.ClientSession() as session:
            for text in ("q1", "q2", "q3"):
                assert (await r.adapter.send("glasses:work", text)).success  # stored while the device is offline
            release, inside = _gated_hello(r.box)
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers())
            await ws.send_str(hello(0))
            await asyncio.to_thread(inside.wait, 5)
            state["broken"] = True
            hook(None)  # a tombstone refusal of another connection while this hello's read is in flight
            release.set()
            types, code = await _until_close(ws)
            state["broken"] = False
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=r.w.headers())
            await ws.send_str(hello(0))
            again = [frame["type"] for frame in await until_pong(ws, r.phone)]
            await ws.close()
            return types, code, again

    types, code, again = asyncio.run(asyncio.wait_for(main(), 20))
    assert (types, code) == ([], 1013)  # no welcome, no replay, a clean close the phone retries
    assert again == ["welcome", "msg.out", "msg.out", "msg.out", "pong"] and r.phone.texts() == ["q1", "q2", "q3"]


def test_the_registry_the_hello_checks_is_a_required_argument(tmp_path):
    with pytest.raises(TypeError):
        Outbox(store(tmp_path), [].append)  # type: ignore[call-arg]
