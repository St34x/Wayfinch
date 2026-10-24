# SPDX-License-Identifier: Apache-2.0
"""T1.179: the deny overlay when writes fail and succeed in turn (concurrent denials, a retry for another device, a
restart), and a failed read of the signer's file at the ``/ws`` upgrade (401, the error type only in the log, the next
upgrade served again)."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from server_auth_helpers import EDGE_CERT, creds, get, refusal, upgrade_headers
from server_helpers import SCHEMA, serving
from signer_registry_helpers import SIGNER_UID, publish

from glasses import registry_signer_file
from glasses.registry import RegistryPermissionError
from glasses.registry_parse import RegistryError
from glasses.registry_signer_file import OVERLAY_NAME, DenyOverlay, SignerFileRegistry
from glasses.server import ServerDeps, build_app

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
ID_A, ID_B, ID_C = "00000000000000aa", "00000000000000bb", "00000000000000cc"


def stored(tmp_path: Path) -> dict[str, str]:
    return dict(json.loads((tmp_path / "state" / OVERLAY_NAME).read_text())["revoked"])


def failing_writes(monkeypatch, count: int, error: Exception | None = None) -> list[int]:
    """The next *count* writes of the overlay raise; returns the list of write attempts (their numbers)."""
    real, attempts = registry_signer_file._write_record, []

    def flaky(*args: object) -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) <= count:
            raise error or RegistryError("overlay: cannot write it (OSError)")
        real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(registry_signer_file, "_write_record", flaky)
    return attempts


def test_a_denial_that_could_not_be_written_goes_to_disk_with_the_next_denial_of_another_device(tmp_path, monkeypatch):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    failing_writes(monkeypatch, 1)
    with pytest.raises(RegistryError):
        overlay.deny(ID_A)
    overlay.deny(ID_B)  # a different device: the whole map is written, A included
    assert set(stored(tmp_path)) == {ID_A, ID_B}


def test_every_write_that_fails_keeps_the_flag_and_the_first_success_clears_it(tmp_path, monkeypatch):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    attempts = failing_writes(monkeypatch, 3)
    for _ in range(3):
        with pytest.raises(RegistryError):
            overlay.deny(ID_A)
    overlay.deny(ID_A)  # the fourth write works
    overlay.deny(ID_A)  # and now nothing is left to write
    overlay.deny(ID_A)
    assert attempts == [1, 2, 3, 4]
    assert stored(tmp_path) == {ID_A: "2026-10-18T12:00:00Z"}


def test_a_write_that_fails_with_an_unexpected_error_still_denies_and_is_retried(tmp_path, monkeypatch):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    failing_writes(monkeypatch, 1, ValueError("boom"))
    with pytest.raises(ValueError, match="boom"):
        overlay.deny(ID_A)
    assert overlay.is_denied(ID_A)
    overlay.deny(ID_C)
    assert set(stored(tmp_path)) == {ID_A, ID_C}


def test_a_denial_whose_write_failed_and_was_never_retried_is_gone_after_a_restart(tmp_path, monkeypatch):
    """The memory is ahead of the file until the next denial writes it: the signer's own revoke is the durable one."""
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    overlay.deny(ID_A)
    failing_writes(monkeypatch, 1)
    with pytest.raises(RegistryError):
        overlay.deny(ID_B)
    assert overlay.is_denied(ID_B)
    assert not DenyOverlay(tmp_path / "state").is_denied(ID_B)
    assert DenyOverlay(tmp_path / "state").is_denied(ID_A)


def test_the_first_time_of_a_denial_survives_the_retries(tmp_path, monkeypatch):
    times = iter(datetime(2026, 10, 18, 12, 0, s, tzinfo=UTC) for s in range(10))
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: next(times))
    failing_writes(monkeypatch, 2)
    for _ in range(2):
        with pytest.raises(RegistryError):
            overlay.deny(ID_A)
    overlay.deny(ID_A)
    assert stored(tmp_path) == {ID_A: "2026-10-18T12:00:00Z"}


def test_concurrent_denials_with_failing_writes_end_with_every_device_on_disk(tmp_path, monkeypatch):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    ids = [f"{i:016x}" for i in range(16)]
    failing_writes(monkeypatch, 5)
    errors: list[BaseException] = []
    start = threading.Barrier(len(ids))

    def deny(device_id: str) -> None:
        start.wait()
        try:
            overlay.deny(device_id)
        except RegistryError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=deny, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(errors) == 5 and all(overlay.is_denied(i) for i in ids)  # exactly the failed writes raised
    assert set(stored(tmp_path)) == set(ids)  # the first success wrote every denial made so far, later ones added
    assert json.loads((tmp_path / "state" / OVERLAY_NAME).read_text())["version"] == 1


def test_a_reader_never_sees_a_denial_come_and_go_while_writes_fail_and_succeed(tmp_path, monkeypatch):
    overlay = DenyOverlay(tmp_path / "state", clock=lambda: NOW)
    failing_writes(monkeypatch, 4)
    ids = [f"{i:016x}" for i in range(12)]
    seen_gone: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        denied: set[str] = set()
        while not stop.is_set():
            now = {i for i in ids if overlay.is_denied(i)}
            seen_gone.extend(sorted(denied - now))
            denied |= now

    watcher = threading.Thread(target=reader)
    watcher.start()
    for device_id in ids:
        with contextlib.suppress(RegistryError):
            overlay.deny(device_id)
    stop.set()
    watcher.join(10)
    assert seen_gone == []


def test_a_descriptor_is_not_left_open_when_the_file_cannot_be_wrapped_for_reading(tmp_path, monkeypatch):
    _, _, token = publish(tmp_path)
    registry = SignerFileRegistry(tmp_path / "public", SIGNER_UID)

    def refuse(fd: int, mode: str = "r") -> object:
        raise OSError(errno.EMFILE, "Too many open files")

    before = len(os.listdir("/proc/self/fd"))
    monkeypatch.setattr(os, "fdopen", refuse)
    with pytest.raises(RegistryPermissionError) as raised:
        registry.lookup(token, "ab" * 32)
    monkeypatch.undo()
    assert len(os.listdir("/proc/self/fd")) == before
    assert "OSError" in str(raised.value) and "Too many" not in str(raised.value)


@pytest.mark.parametrize("failing", [stat.S_ISREG, stat.S_ISDIR], ids=["signer-file", "public-dir"])
def test_a_failed_fstat_is_the_typed_refusal_with_no_path_or_error_text_and_no_descriptor_left(
    tmp_path, monkeypatch, failing
):
    target, _, token = publish(tmp_path)
    registry = SignerFileRegistry(tmp_path / "public", SIGNER_UID)
    real_fstat = os.fstat

    def eio(fd: int) -> os.stat_result:
        if failing(real_fstat(fd).st_mode):
            raise OSError(errno.EIO, "Input/output error: " + str(target))
        return real_fstat(fd)

    before = len(os.listdir("/proc/self/fd"))
    monkeypatch.setattr(os, "fstat", eio)
    with pytest.raises(RegistryPermissionError) as raised:
        registry.lookup(token, "ab" * 32)
    monkeypatch.undo()
    text = str(raised.value)
    assert "OSError" in text and "Input/output" not in text and str(tmp_path) not in text and "Errno" not in text
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert len(os.listdir("/proc/self/fd")) == before


def app_for_registry(tmp_path: Path, registry: SignerFileRegistry) -> web.Application:
    deps = ServerDeps(
        gate_ok_async=_open_gate,
        registry=registry,
        is_revoked=lambda _id: False,
        peer_allowed=lambda _request: True,
        client_fingerprint=EDGE_CERT.fingerprint,
        on_frame=_ignore,
        schema=SCHEMA,
        max_audio_sec=30,
    )
    return build_app(deps)


async def _open_gate() -> bool:
    return True


async def _ignore(device: object, frame: object) -> None:
    return None


async def connect(app: web.Application, token: str) -> None:
    async with serving(app) as port, aiohttp.ClientSession() as session:
        ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers={**upgrade_headers(), **creds(token)})
        await ws.close()


class EioReader:
    """Stands in for ``os.fdopen(fd, "rb")``: opens the real descriptor, fails the read with a path in the OS text."""

    path = ""

    def __init__(self, fd: int, mode: str = "r") -> None:
        self.fh = _REAL_FDOPEN(fd, "rb")

    def __enter__(self) -> EioReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.fh.close()

    def read(self, size: int = -1) -> bytes:
        raise OSError(errno.EIO, "Input/output error: " + self.path)


_REAL_FDOPEN = os.fdopen


def test_the_typed_error_of_a_failed_read_names_the_file_and_the_error_type_only(tmp_path, monkeypatch):
    target, _, token = publish(tmp_path)
    EioReader.path = str(target)
    monkeypatch.setattr(os, "fdopen", EioReader)
    with pytest.raises(RegistryPermissionError) as raised:
        SignerFileRegistry(tmp_path / "public", SIGNER_UID).lookup(token, "ab" * 32)
    text = str(raised.value)
    assert "OSError" in text and "Input/output" not in text and str(tmp_path) not in text
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_a_failed_read_of_the_signers_file_is_401_with_only_the_type_logged_and_the_next_upgrade_is_served(
    tmp_path, monkeypatch, caplog
):
    target, _, token = publish(tmp_path)
    app = app_for_registry(
        tmp_path, SignerFileRegistry(tmp_path / "public", SIGNER_UID, DenyOverlay(tmp_path / "state"))
    )
    EioReader.path = str(target)
    caplog.set_level(logging.DEBUG)
    with monkeypatch.context() as patched:
        patched.setattr(os, "fdopen", EioReader)
        reply = get(app, creds(token))
    assert refusal(reply, 401, "unauthorized")
    assert b"EIO" not in reply.body and str(target).encode() not in reply.body
    assert "RegistryPermissionError" in caplog.text
    assert str(target) not in caplog.text and "Input/output" not in caplog.text and token not in caplog.text
    served = SignerFileRegistry(tmp_path / "public", SIGNER_UID)
    asyncio.run(asyncio.wait_for(connect(app_for_registry(tmp_path, served), token), 20))  # the next upgrade is served
