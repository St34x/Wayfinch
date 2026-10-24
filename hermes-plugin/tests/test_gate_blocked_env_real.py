# SPDX-License-Identifier: Apache-2.0
"""A served profile's ``.env`` that never returns from a read (a FIFO), against real Hermes.

The first check blocks inside Hermes' own ``.env`` reader and holds the verdict's lock. A second check (``connect()``
and ``handle_message()`` through the worker thread) is refused after ``LOCK_TIMEOUT`` with the fixed text while the
event loop keeps ticking; after the blocked read ends the next check is judged normally. Skipped without the Hermes
venv (see docs/test-hermes.md).
"""

from __future__ import annotations

import json
import subprocess

from conftest import PLUGIN_ROOT
from test_config_check_edges import _scratch
from test_config_check_live import hermes_python
from test_gate_served_profiles_real import SENTINEL, _home

RUNNER = f"""\
import asyncio, json, os, sys, threading, time
from pathlib import Path
sys.path.insert(0, {str(PLUGIN_ROOT)!r})
import gateway.platforms._shared  # noqa: F401 - the marker that tells the gate Hermes is loaded
from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from glasses import config as gcfg
from glasses import gate
from glasses.adapter import GlassesAdapter

platform_registry.register(PlatformEntry(name="glasses", label="Glasses", adapter_factory=lambda c: None,
    check_fn=lambda: True, validate_config=gcfg.validate_config, required_env=[], install_hint="", source="plugin"))

fifo = Path(os.environ["HERMES_HOME"]) / "profiles" / "served" / ".env"
os.mkfifo(fifo)
adapter = GlassesAdapter(PlatformConfig(enabled=True, extra={{"public_url": "wss://glasses.example.com/ws",
                                                            "lanes": ["assistant", "work"]}}))
holder = threading.Thread(target=adapter.gate_ok, daemon=True)
holder.start()
time.sleep(0.5)  # the holder is inside Hermes' read of the FIFO, holding the lock
out = {{"holder_alive": holder.is_alive()}}

async def main():
    ticks, stop = 0, asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    start = time.monotonic()
    out["connect"] = await adapter.connect()
    out["connect_waited"] = round(time.monotonic() - start, 2)
    event = type("Event", (), {{"_gateway_accepted": None, "text": "hello"}})()
    start = time.monotonic()
    await adapter.handle_message(event)
    out["handle_waited"] = round(time.monotonic() - start, 2)
    out["accepted"] = event._gateway_accepted
    stop.set()
    await task
    out["ticks"] = ticks

asyncio.run(main())
out["refusal"] = gate.LOCK_TIMEOUT_REFUSAL
with open(fifo, "wb") as writer:  # ends the blocked read: an empty .env
    writer.write(b"")
holder.join(10)
out["holder_done"] = not holder.is_alive()
fifo.unlink()  # a FIFO blocks every open that has no writer: the next recompute needs a regular file
fifo.write_text("")
out["after"] = asyncio.run(adapter.gate_ok_async())
print(json.dumps(out))
"""


def test_real_hermes_a_blocked_env_read_refuses_the_next_check_and_does_not_freeze_the_loop():
    with _scratch() as tmp:
        home, user = _home(tmp)
        script = home.parent / "blocked_runner.py"
        script.write_text(RUNNER, encoding="utf-8")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(user), "HERMES_HOME": str(home)}
        proc = subprocess.run(
            [str(hermes_python()), str(script)], cwd=home.parent, env=env, capture_output=True, text=True, timeout=60
        )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert SENTINEL not in proc.stdout + proc.stderr
    out = json.loads(proc.stdout.splitlines()[-1])
    assert out["holder_alive"] is True, out
    assert out["connect"] is False and out["accepted"] is False, out
    assert 1.8 <= out["connect_waited"] <= 4.0, out  # the first check waits out LOCK_TIMEOUT
    assert out["handle_waited"] < 0.5, out  # the recompute has outlived it: the next check is refused at once
    assert out["ticks"] >= 5, out  # a loop frozen for the ~4 s of waiting would have ticked 0 times
    assert out["holder_done"] is True and out["after"] is True, out
