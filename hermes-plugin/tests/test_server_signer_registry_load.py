# SPDX-License-Identifier: Apache-2.0
"""Upgrades against the signer's registry file while the signer rewrites it (T1.179): concurrent upgrades see only
whole files, a bad file gives 401 and nothing else, and a large registry read does not stall the event loop."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import aiohttp
from server_auth_helpers import creds, upgrade_headers
from server_helpers import serving
from signer_registry_helpers import publish

from test_server_signer_registry import app_with

N_CLIENTS = 8  # the server admits 8 upgrades in flight per peer, then 503


def rewrite_forever(target: Path, versions: list[bytes], stop: threading.Event) -> threading.Thread:
    def run() -> None:
        i = 0
        while not stop.is_set():
            tmp = target.with_name("devices.json.new")
            tmp.write_bytes(versions[i % len(versions)])
            tmp.chmod(0o640)
            os.replace(tmp, target)
            i += 1
            time.sleep(0.001)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


async def upgrade_status(port: int, token: str) -> int:
    async with aiohttp.ClientSession() as session:
        try:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers={**upgrade_headers(), **creds(token)})
        except aiohttp.WSServerHandshakeError as exc:
            return exc.status
        await ws.close()
        return 101


async def burst(app, token: str, count: int = N_CLIENTS) -> list[int]:  # type: ignore[no-untyped-def]
    async with serving(app) as port:
        return list(await asyncio.gather(*[upgrade_status(port, token) for _ in range(count)]))


def test_concurrent_upgrades_during_atomic_signer_rewrites_all_succeed(tmp_path):
    target, device, token = publish(tmp_path)
    active = target.read_bytes()
    doc = json.loads(active)
    doc["devices"].append(dict(doc["devices"][0], deviceId="1" * 16, certFingerprint="12" * 32, tokenSha256="3" * 64))
    other = json.dumps(doc, indent=2, sort_keys=True).encode() + b"\n"
    stop = threading.Event()
    writer = rewrite_forever(target, [active, other], stop)
    app, _, seen = app_with(tmp_path)
    try:
        statuses = asyncio.run(asyncio.wait_for(burst(app, token), 60))
    finally:
        stop.set()
        writer.join(10)
        seen.close()
    assert statuses == [101] * N_CLIENTS
    assert list(json.loads((tmp_path / "state" / "last-seen.json").read_text())["seen"]) == [device.device_id]


def test_upgrades_while_the_file_is_rewritten_in_place_are_101_or_401_never_a_server_error(tmp_path):
    target, _, token = publish(tmp_path)
    good = target.read_bytes()
    stop = threading.Event()

    def torn() -> None:
        while not stop.is_set():
            with target.open("r+b") as fh:
                fh.truncate(0)
                fh.write(good)

    writer = threading.Thread(target=torn)
    writer.start()
    app, _, seen = app_with(tmp_path)
    try:
        statuses = asyncio.run(asyncio.wait_for(burst(app, token), 60))
    finally:
        stop.set()
        writer.join(10)
        seen.close()
    assert set(statuses) <= {101, 401}


def test_a_large_registry_read_does_not_stall_the_event_loop(tmp_path):
    target, _, token = publish(tmp_path)
    doc = json.loads(target.read_bytes())
    template = doc["devices"][0]
    entries = [
        dict(template, deviceId=f"{i:016x}", certFingerprint=f"{i:064x}", name=f"device {i}") for i in range(8000)
    ]
    doc["devices"] = [*entries, template]
    target.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    target.chmod(0o640)
    gaps: list[float] = []

    async def run() -> list[int]:
        stop = asyncio.Event()

        async def heartbeat() -> None:
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        try:
            return await burst(app, token, 8)
        finally:
            stop.set()
            await beat

    app, _, seen = app_with(tmp_path)
    try:
        statuses = asyncio.run(asyncio.wait_for(run(), 120))
    finally:
        seen.close()
    assert statuses == [101] * 8
    assert max(gaps) < 2.0  # a gross bound (a parse of 3.7 MB takes ~0.2 s here): the read is off the loop
