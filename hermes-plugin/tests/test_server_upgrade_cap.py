# SPDX-License-Identifier: Apache-2.0
"""WS server per-certificate cap on concurrent upgrades and the ``on_authenticated`` hook (T1.07c)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
import pytest
from server_auth_helpers import FP, OTHER_FP, SpyRegistry, cert_header, creds, upgrade_headers
from server_helpers import HELLO, app_for, serving, wiring

from glasses.connections import UPGRADES_PER_CERT_MAX, TooManyUpgrades, UpgradeCap
from glasses.registry import Device

TOKEN = "S3ntinelTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
SLOW = 0.6  # seconds one registry read takes


class Counting(SpyRegistry):
    """A slow registry stand-in that also records how many reads ran at the same time."""

    running = 0
    peak = 0

    def lookup(self, token: Any, fingerprint: Any) -> tuple[Device | None, bool]:
        type(self).running += 1
        type(self).peak = max(type(self).peak, type(self).running)
        try:
            return super().lookup(token, fingerprint)
        finally:
            type(self).running -= 1


def test_the_cap_counts_per_key_and_frees_a_slot_on_every_exit():
    cap = UpgradeCap(per_key=2)
    with cap.slot("a"), cap.slot("a"):
        with pytest.raises(TooManyUpgrades), cap.slot("a"):
            pytest.fail("a third slot of one key")
        with cap.slot("b"):  # another key is not counted against "a"
            pass
    with pytest.raises(RuntimeError), cap.slot("a"):
        raise RuntimeError  # leaving with an exception gives the slot back too
    with cap.slot("a"), cap.slot("a"):
        pass
    assert cap._open == {}  # nothing is kept for a key that has no upgrade in flight


def test_50_concurrent_upgrades_with_one_certificate_cost_at_most_the_cap_in_registry_reads_and_stall_nothing(tmp_path):
    w = wiring(tmp_path)
    Counting.running = Counting.peak = 0
    registry, gate_calls = Counting(delay=SLOW), []

    async def gate() -> bool:
        gate_calls.append(1)
        return True

    app = app_for(w, registry=registry, gate_ok_async=gate)

    async def main() -> tuple[list[tuple[int, bytes]], float, tuple[int, int], tuple[int, bytes]]:
        async with serving(app) as port, aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0)) as session:

            async def upgrade() -> tuple[int, bytes]:
                headers = {**upgrade_headers(), **creds(TOKEN)}
                async with session.get(f"http://127.0.0.1:{port}/ws", headers=headers) as response:
                    return response.status, await response.read()

            flood = [asyncio.create_task(upgrade()) for _ in range(50)]
            await asyncio.sleep(0.2)  # all 50 are at the server, the first reads are running
            started = time.monotonic()
            await asyncio.to_thread(lambda: None)  # an unrelated job of the loop's default executor
            stall = time.monotonic() - started
            replies = await asyncio.gather(*flood)
            reads, gates = len(registry.calls), len(gate_calls)
            return replies, stall, (reads, gates), await upgrade()  # and one more once the flood is over

    replies, stall, (reads, gates), later = asyncio.run(asyncio.wait_for(main(), 30))
    statuses = sorted(status for status, _ in replies)
    assert statuses == [401] * UPGRADES_PER_CERT_MAX + [503] * (50 - UPGRADES_PER_CERT_MAX)
    assert all(body == b'{"error":"unavailable"}' for status, body in replies if status == 503)
    assert reads == UPGRADES_PER_CERT_MAX  # the other 42 never reached the registry hop
    assert gates == UPGRADES_PER_CERT_MAX  # nor the gate
    assert Counting.peak <= UPGRADES_PER_CERT_MAX and stall < 0.5, (Counting.peak, stall)
    assert later[0] == 401  # every slot was given back: the next upgrade is judged, not refused


def test_the_key_is_the_certificate_not_the_peer_address(tmp_path):
    """While one certificate is at the cap, the same certificate from another address is capped too and another
    certificate is judged."""
    w = wiring(tmp_path)
    registry = SpyRegistry(delay=SLOW)
    app = app_for(w, registry=registry)

    async def main() -> tuple[list[int], int, int]:
        async with serving(app) as port:
            url = f"http://127.0.0.1:{port}/ws"
            async with (
                aiohttp.ClientSession() as first,
                aiohttp.ClientSession(connector=aiohttp.TCPConnector(local_addr=("127.0.0.2", 0))) as second,
            ):

                async def upgrade(session: aiohttp.ClientSession, fingerprint: str = FP) -> int:
                    headers = {**upgrade_headers(), **creds(TOKEN, fingerprint)}
                    async with session.get(url, headers=headers) as response:
                        return response.status

                crowd = [asyncio.create_task(upgrade(first)) for _ in range(UPGRADES_PER_CERT_MAX + 1)]
                await asyncio.sleep(0.1)
                elsewhere, other = await upgrade(second), await upgrade(second, OTHER_FP)
                return sorted(await asyncio.gather(*crowd)), elsewhere, other

    crowd, elsewhere, other = asyncio.run(asyncio.wait_for(main(), 30))
    assert crowd == [401] * UPGRADES_PER_CERT_MAX + [503] and elsewhere == 503
    assert other == 401  # judged, not capped


def test_the_slot_is_held_only_while_the_upgrade_is_in_flight_not_for_the_websocket(tmp_path):
    w = wiring(tmp_path)

    async def main() -> list[aiohttp.ClientWebSocketResponse]:
        sockets = []
        async with serving(app_for(w)) as port, aiohttp.ClientSession() as session:
            for _ in range(UPGRADES_PER_CERT_MAX + 3):  # more connections than the cap, one after the other
                ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
                await ws.send_str(json.dumps(HELLO))
                assert (await ws.receive_json())["type"] == "welcome"
                sockets.append(ws)
            return sockets

    assert len(asyncio.run(asyncio.wait_for(main(), 30))) == UPGRADES_PER_CERT_MAX + 3


def test_on_authenticated_gets_the_device_once_after_the_upgrade_and_not_for_a_refused_one(tmp_path):
    w = wiring(tmp_path)
    seen: list[str] = []

    async def main() -> None:
        app = app_for(w, on_authenticated=lambda device: seen.append(device.device_id))
        async with serving(app) as port, aiohttp.ClientSession() as session:
            bad = {**w.headers(), **cert_header(OTHER_FP)}  # another device's certificate
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=bad)
            assert seen == []
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await ws.send_str(json.dumps(HELLO))
            await ws.receive_json()
            await ws.close()

    asyncio.run(asyncio.wait_for(main(), 30))
    assert seen == [w.device.device_id]


def test_a_failing_on_authenticated_does_not_end_the_session_and_logs_the_type_only(tmp_path, caplog):
    w = wiring(tmp_path)
    caplog.set_level(logging.DEBUG)

    def broken(device: Device) -> None:
        raise OSError(f"disk full at {w.token}")

    async def main() -> str:
        async with serving(app_for(w, on_authenticated=broken)) as port, aiohttp.ClientSession() as session:
            ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=w.headers())
            await ws.send_str(json.dumps(HELLO))
            reply: str = (await ws.receive_json())["type"]
            await ws.close()
            return reply

    assert asyncio.run(asyncio.wait_for(main(), 30)) == "welcome"
    assert "OSError" in caplog.text and w.token not in caplog.text and FP not in caplog.text
    assert "Traceback" not in caplog.text
