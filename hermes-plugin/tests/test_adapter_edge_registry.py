# SPDX-License-Identifier: Apache-2.0
"""The registry, the deny overlay and last-seen as ``adapter.connect()`` wires them (T1.184b), proved
through the real unix listener instead of by object identity: a device in the signer's published file authenticates
and is recorded in the plugin's own ``last-seen.json`` (0600), a missing / corrupt / loose-mode / symlinked file is
``401`` for everyone, the signer's directory is never written, an id in ``revoked-local.json`` gets ``403
device_revoked`` (also after a restart of the adapter) and ``disconnect()`` flushes last-seen without holding the loop.

The test process plays the signer user (it owns the files; ``signer_user_uid`` is patched to its own uid) and, with
``peer_uid`` patched, the edge user.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest
from edge_helpers import other_user, raw_unix, request_bytes, short_dir
from server_auth_helpers import creds, upgrade_headers
from signer_registry_helpers import publish

from glasses import edge_auth, edge_cert, registry_signer_file, server
from glasses.registry_signer_file import LAST_SEEN_NAME, DenyOverlay, LastSeen

import test_adapter
from test_adapter import new_adapter

REVOKED_LOCAL = "revoked-local.json"
adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument


@pytest.fixture
def as_signer(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process is the signer user and (for the peer check) the edge user."""
    monkeypatch.setattr(registry_signer_file, "signer_user_uid", lambda _cfg: os.geteuid())
    monkeypatch.setattr(edge_cert, "signer_user_uid", lambda _cfg: os.geteuid())  # the CA bundle has the same owner
    monkeypatch.setattr(edge_auth, "peer_uid", lambda _transport: other_user().pw_uid)


def _extra(sock: Path, tmp_path: Path) -> dict[str, object]:
    user = other_user().pw_name
    signer = {"user": user, "public_dir": str(tmp_path / "public"), "socket": "/run/device-signer/signer.sock"}
    return {"edge": {"socket": str(sock), "user": user}, "signer": signer, "state_dir": str(tmp_path / "state")}


def _upgrade(sock: Path, token: str) -> bytes:
    return request_bytes("GET", "/ws", {**upgrade_headers(), **creds(token)})


async def _status(sock: Path, token: str) -> bytes:
    return (await raw_unix(sock, _upgrade(sock, token))).split(b"\r\n", 1)[0]


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True, mode=0o700)
    return state


def test_a_device_in_the_signers_file_authenticates_and_only_last_seen_is_written(adapter_module, tmp_path, as_signer):
    target, device, token = publish(tmp_path)
    signed, listing = target.read_bytes(), sorted(p.name for p in target.parent.iterdir())
    state = _state(tmp_path)
    opened: list[tuple[str, int]] = []
    real_open = os.open

    def spy(path, flags, *args, **kwargs):
        opened.append((str(path), flags))
        return real_open(path, flags, *args, **kwargs)

    with short_dir() as d, pytest.MonkeyPatch.context() as mp:
        adapter = new_adapter(adapter_module, _extra(d / "edge.sock", tmp_path))
        mp.setattr(os, "open", spy)

        async def run() -> bytes:
            assert await adapter.connect() is True
            status = await _status(d / "edge.sock", token)
            await adapter.disconnect()
            return status

        assert asyncio.run(run()).startswith(b"HTTP/1.1 101")
    written = state / LAST_SEEN_NAME
    assert list(json.loads(written.read_text())["seen"]) == [device.device_id]
    assert stat.S_IMODE(written.stat().st_mode) == 0o600 and not (state / "devices.json").exists()
    assert target.read_bytes() == signed and sorted(p.name for p in target.parent.iterdir()) == listing
    writing = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
    wrote = [p for p, flags in opened if flags & writing and (str(target.parent) in p or p.endswith("devices.json"))]
    assert wrote == []


@pytest.mark.parametrize("damage", ["missing", "corrupt", "loose_mode", "symlink"])
def test_a_missing_corrupt_loose_or_symlinked_signer_file_is_401_for_every_device(
    adapter_module,
    tmp_path,
    as_signer,
    damage,
):
    target, _device, token = publish(tmp_path)
    if damage == "missing":
        target.unlink()
    elif damage == "corrupt":
        target.write_bytes(b"garbage")
    elif damage == "loose_mode":
        target.chmod(0o664)
    else:
        real = target.with_name("elsewhere.json")
        target.rename(real)
        target.symlink_to(real)
    _state(tmp_path)
    with short_dir() as d:
        adapter = new_adapter(adapter_module, _extra(d / "edge.sock", tmp_path))

        async def run() -> bytes:
            assert await adapter.connect() is True
            status = await _status(d / "edge.sock", token)
            await adapter.disconnect()
            return status

        assert asyncio.run(run()).startswith(b"HTTP/1.1 401")


def test_an_id_in_the_local_deny_file_is_403_through_the_real_app_and_after_a_restart(
    adapter_module,
    tmp_path,
    as_signer,
):
    _target, device, token = publish(tmp_path)  # the signer's file lists the device as active
    DenyOverlay(_state(tmp_path)).deny(device.device_id)
    assert (tmp_path / "state" / REVOKED_LOCAL).exists()
    with short_dir() as d:

        async def one_run() -> bytes:
            adapter = new_adapter(adapter_module, _extra(d / "edge.sock", tmp_path))
            assert await adapter.connect() is True
            reply = await raw_unix(d / "edge.sock", _upgrade(d / "edge.sock", token))
            await adapter.disconnect()
            return reply

        for _restart in range(2):  # the second run is a new connect() over the same state dir
            reply = asyncio.run(one_run())
            assert reply.startswith(b"HTTP/1.1 403") and reply.endswith(b'{"error":"device_revoked"}')


def test_a_denial_made_on_the_apps_own_overlay_cuts_the_next_upgrade(adapter_module, tmp_path, as_signer):
    _target, device, token = publish(tmp_path)
    _state(tmp_path)
    with short_dir() as d:
        adapter = new_adapter(adapter_module, _extra(d / "edge.sock", tmp_path))

        async def run() -> list[bytes]:
            assert await adapter.connect() is True
            first = await _status(d / "edge.sock", token)
            overlay = adapter._edge.runner.app[server._DEPS].registry._overlay  # what revoke_now will call deny on
            overlay.deny(device.device_id)
            second = await _status(d / "edge.sock", token)
            await adapter.disconnect()
            return [first, second]

        first, second = asyncio.run(run())
    assert first.startswith(b"HTTP/1.1 101") and second.startswith(b"HTTP/1.1 403")


def test_disconnect_returns_after_a_stalled_last_seen_write_without_holding_the_loop(
    adapter_module,
    tmp_path,
    as_signer,
    monkeypatch,
):
    _target, _device, token = publish(tmp_path)
    _state(tmp_path)
    release, entered = threading.Event(), threading.Event()
    real_write = LastSeen._write

    def stalled(self: LastSeen, records: dict[str, str]) -> None:
        entered.set()
        release.wait(5)
        real_write(self, records)

    monkeypatch.setattr(LastSeen, "_write", stalled)
    with short_dir() as d:
        adapter = new_adapter(adapter_module, _extra(d / "edge.sock", tmp_path))

        async def run() -> tuple[float, float]:
            assert await adapter.connect() is True
            assert (await _status(d / "edge.sock", token)).startswith(b"HTTP/1.1 101")
            assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)  # the write is in flight
            gaps, stop = [0.0], asyncio.Event()

            async def tick() -> None:
                last = time.monotonic()
                while not stop.is_set():
                    await asyncio.sleep(0.01)
                    now = time.monotonic()
                    gaps[0], last = max(gaps[0], now - last - 0.01), now

            ticking = asyncio.create_task(tick())
            threading.Timer(0.6, release.set).start()
            started = time.monotonic()
            await adapter.disconnect()  # joins the writer: it ends only once the write is released
            took = time.monotonic() - started
            stop.set()
            await ticking
            return took, gaps[0]

        took, overshoot = asyncio.run(run())
    assert took >= 0.5, "disconnect() did not wait for the write in flight"
    assert overshoot < 0.1, f"the loop was held for {overshoot * 1000:.0f} ms during close()"


def test_an_unset_edge_socket_with_a_valid_edge_user_is_a_fatal_error_and_binds_nothing(
    adapter_module,
    tmp_path,
    as_signer,
):
    _target, _device, _token = publish(tmp_path)
    extra = _extra(tmp_path / "unused.sock", tmp_path)
    extra["edge"] = {"socket": "", "user": other_user().pw_name}
    adapter = new_adapter(adapter_module, extra)
    abstract_before = Path("/proc/net/unix").read_text().count("@")
    assert asyncio.run(adapter.connect()) is False
    ((code, message, retryable),) = adapter.fatal_errors
    assert (code, retryable) == ("edge_listener", True) and "edge.socket: not set" in message
    assert Path("/proc/net/unix").read_text().count("@") <= abstract_before  # no autobound abstract socket either
