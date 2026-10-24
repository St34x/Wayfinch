# SPDX-License-Identifier: Apache-2.0
"""``GlassesAdapter.connect()`` / ``disconnect()`` with the edge listener (T1.184b): the socket exists (0660) while
connected and is gone after ``disconnect()``; a client of the edge uid reaches ``/ws`` (the signer's registry file is
missing here: ``401``); the wiring pins (the adapter's own ``gate_ok_async``, the deny overlay as ``is_revoked`` and
inside the registry, ``LastSeen.touch`` as ``on_authenticated`` and its ``close()`` off the loop; the frame check's
poll and worker thread end at ``disconnect()`` and on a start that fails after the poll began); an aiohttp drift and
a bad path end in the fatal error with nothing left bound."""

from __future__ import annotations

import asyncio
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from edge_helpers import other_user, raw_unix, request_bytes, short_dir
from server_auth_helpers import cert_header, upgrade_headers

from glasses import edge_auth, server, server_runner
from glasses.registry_signer_file import DenyOverlay, LastSeen
from glasses.revoked_ids import RevokedIds

from test_adapter import adapter_module, new_adapter  # noqa: F401 - the fixture is used by name


def _extra(sock: Path, tmp_path: Path) -> dict[str, object]:
    user = other_user().pw_name
    signer = {"user": user, "public_dir": str(tmp_path / "pub"), "socket": "/run/device-signer/signer.sock"}
    return {"edge": {"socket": str(sock), "user": user}, "signer": signer, "state_dir": str(tmp_path / "state")}


def test_connect_serves_the_socket_and_disconnect_removes_it(adapter_module, tmp_path, monkeypatch):  # noqa: F811 - the imported fixture, used by name
    monkeypatch.setattr(edge_auth, "peer_uid", lambda _transport: other_user().pw_uid)  # this process plays the edge
    with short_dir() as d:
        sock = d / "edge.sock"
        adapter = new_adapter(adapter_module, _extra(sock, tmp_path))

        async def run() -> bytes:
            assert await adapter.connect() is True
            found = os.lstat(sock)
            assert stat.S_ISSOCK(found.st_mode) and stat.S_IMODE(found.st_mode) == 0o660
            headers = {**upgrade_headers(), "Authorization": "Bearer t0ken-x", **cert_header()}
            reply = await raw_unix(sock, request_bytes("GET", "/ws", headers))
            await adapter.disconnect()
            return reply

        assert asyncio.run(run()).startswith(b"HTTP/1.1 401")  # past the peer check; no devices.json: 401
        assert not sock.exists() and adapter.fatal_errors == []


def test_the_app_is_wired_with_the_adapter_gate_the_overlay_and_last_seen(adapter_module, tmp_path, monkeypatch):  # noqa: F811 - the imported fixture, used by name
    closed_on: list[str] = []
    real_close = LastSeen.close

    def close(self: LastSeen) -> None:
        closed_on.append(threading.current_thread().name)
        real_close(self)

    monkeypatch.setattr(LastSeen, "close", close)
    with short_dir() as d:
        adapter = new_adapter(adapter_module, _extra(d / "edge.sock", tmp_path))

        async def run() -> None:
            assert await adapter.connect() is True
            app = adapter._edge.runner.app
            deps, auth = app[server._DEPS], app[server._AUTH]
            assert auth.gate_ok_async == adapter.gate_ok_async and auth.gate_ok_async.__self__ is adapter
            overlay = deps.registry._overlay  # is_revoked asks it (and purges the outbox of a denied device)
            assert isinstance(overlay, DenyOverlay) and deps.on_hello.__self__ is adapter._edge.outbox
            assert deps.on_closed == adapter._edge.outbox.closed  # a session's end drops its live link
            check, box = deps.is_revoked.__self__, adapter._edge.outbox  # T1.248: overlay, outbox, signer's set
            assert check._also == (overlay.is_denied, box.is_revoked) and check._read == deps.registry.list
            assert box._devices == deps.registry.list and check._purge == box.purge_soon  # hello checks the registry
            assert check._close == app[server.CONNECTIONS].close_device  # the cut closes the session 4403 at once
            assert adapter._edge.revoked is check and check._poll is not None and not check._poll.done()
            assert check._path == deps.registry._file  # the stat runs on the file the reader reads
            assert isinstance(deps.on_authenticated.__self__, LastSeen)
            poll = check._poll
            assert len(check._pool._threads) == 1  # the load ran on the check's own worker
            await adapter.disconnect()
            await asyncio.sleep(0)
            assert poll.done()  # disconnect() ends the poll
            assert _workers_end(check)  # and shuts the worker down while the check is still referenced

        asyncio.run(run())
    assert closed_on and closed_on[0] != threading.main_thread().name


def _workers_end(check: RevokedIds) -> bool:
    deadline = time.monotonic() + 2
    while any(t.is_alive() for t in check._pool._threads) and time.monotonic() < deadline:
        time.sleep(0.01)
    return not any(t.is_alive() for t in check._pool._threads)


def test_a_start_that_fails_after_the_poll_began_ends_the_poll_and_the_worker(adapter_module, tmp_path, monkeypatch):  # noqa: F811 - the imported fixture, used by name
    bound: list[RevokedIds] = []
    real_bind = RevokedIds.bind

    def bind(self: RevokedIds, close: Any, purge: Any) -> None:
        bound.append(self)
        real_bind(self, close, purge)

    async def failing_serve(_app: object, _listener: object) -> None:  # stands in for server.serve
        raise RuntimeError("serve failed")

    monkeypatch.setattr(RevokedIds, "bind", bind)
    monkeypatch.setattr(adapter_module, "serve", failing_serve)
    with short_dir() as d:
        adapter = new_adapter(adapter_module, _extra(d / "edge.sock", tmp_path))

        async def run() -> None:
            assert await adapter.connect() is False
            await asyncio.sleep(0)
            (check,) = bound
            assert check._poll is not None and check._poll.done()  # the failed start closed the check
            assert len(check._pool._threads) == 1 and _workers_end(check)

        asyncio.run(run())


def test_an_aiohttp_drift_is_a_fatal_error_and_leaves_no_socket(adapter_module, tmp_path, monkeypatch):  # noqa: F811 - the imported fixture, used by name
    real = server_runner.web.AppRunner._make_server

    async def drifted(self):
        made = await real(self)
        del made._kwargs  # as if aiohttp had renamed it
        return made

    monkeypatch.setattr(server_runner.web.AppRunner, "_make_server", drifted)
    with short_dir() as d:
        adapter = new_adapter(adapter_module, _extra(d / "edge.sock", tmp_path))
        assert asyncio.run(adapter.connect()) is False
        assert not (d / "edge.sock").exists()
    ((code, message, retryable),) = adapter.fatal_errors
    assert (code, retryable) == ("edge_listener", True) and "web.Server has no _kwargs" in message


@pytest.mark.parametrize("kind", ["file", "edge_user_is_me"])
def test_a_bad_path_or_edge_user_is_a_fatal_error(adapter_module, tmp_path, kind):  # noqa: F811 - the imported fixture, used by name
    with short_dir() as d:
        sock = d / "edge.sock"
        extra = _extra(sock, tmp_path)
        if kind == "file":
            sock.write_bytes(b"x")
        else:
            extra["edge"] = {"socket": str(sock), "user": __import__("pwd").getpwuid(os.geteuid()).pw_name}
        adapter = new_adapter(adapter_module, extra)
        assert asyncio.run(adapter.connect()) is False
        assert sock.exists() is (kind == "file")
    assert [code for code, _, _ in adapter.fatal_errors] == ["edge_listener"]
