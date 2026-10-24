# SPDX-License-Identifier: Apache-2.0
"""Purge on revocation over the production wiring (T1.09b): ``adapter.connect()`` over the signer's
``devices.json`` (``signer_file_registry``) and the plugin's ``DenyOverlay``. A device revoked while the plugin runs, by
the signer (its entry becomes a fingerprint tombstone) or by the overlay, has 3 queued answers; its next upgrade is
``403`` and its rows are gone. Also: an unusable ``outbox.sqlite`` fails ``connect()`` (retryable), a store failure in
``send()`` is a retryable failed ``SendResult``, and an answer's ``corr`` goes only to the device that asked."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from edge_helpers import short_dir
from outbox_helpers import run, seqs
from outbox_session_helpers import EXTRA, rig
from server_helpers import MSG_IN, SCHEMA
from signer_registry_helpers import publish

from glasses import server
from glasses.outbox import FILE_NAME
from glasses.registry import Registry
from glasses.registry_parse import FILE_NAME as REGISTRY_FILE

import test_adapter
from test_adapter_edge_registry import _extra, _state, _status, as_signer  # noqa: F401 - the fixture is used by name

adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument


def _signer_revokes(tmp_path: Path, device_id: str) -> None:
    """The signer revokes the device and copies its file to the public dir again (as it does on revoke)."""
    Registry(tmp_path / "source").revoke(device_id)
    target = tmp_path / "public" / REGISTRY_FILE
    target.unlink()
    shutil.copyfile(tmp_path / "source" / REGISTRY_FILE, target)
    target.chmod(0o640)


@pytest.mark.parametrize("by", ["signer", "overlay"])
def test_a_device_revoked_while_the_plugin_runs_is_refused_403_and_its_3_queued_rows_are_gone(
    adapter_module,
    tmp_path,
    as_signer,  # noqa: F811 - the imported fixture
    by,
):
    _target, dev, token = publish(tmp_path)
    state = _state(tmp_path)
    with short_dir() as d:
        extra = {**_extra(d / "edge.sock", tmp_path), "lanes": ["main"], "home_lane": "main"}
        adapter = test_adapter.new_adapter(adapter_module, extra)

        async def main() -> tuple[bytes, list[int]]:
            assert await adapter.connect() is True
            for text in ("q1", "q2", "q3"):
                assert (await adapter.send("glasses:main", text)).success is True
            queued = seqs(state, dev.device_id)
            if by == "signer":
                _signer_revokes(tmp_path, dev.device_id)
            else:  # the app's own overlay, the one revoke_now will call deny on
                adapter._edge.runner.app[server._DEPS].registry._overlay.deny(dev.device_id)
            status = await _status(d / "edge.sock", token)
            await asyncio.sleep(0.05)  # a tombstone's sweep is a task: let it queue on the outbox worker
            await asyncio.wrap_future(adapter._edge.outbox.ack_soon("ffffffffffffffff", 0))  # behind the purge
            await adapter.disconnect()
            return status, queued

        status, queued = run(main())
    assert status.startswith(b"HTTP/1.1 403") and queued == [1, 2, 3]
    assert seqs(state, dev.device_id) == []


def test_an_unusable_outbox_file_fails_connect_retryably_with_the_reason(adapter_module, tmp_path, as_signer, caplog):  # noqa: F811 - the imported fixture
    publish(tmp_path)
    loose = _state(tmp_path) / FILE_NAME
    loose.touch(mode=0o644)
    loose.chmod(0o644)
    with short_dir() as d:
        adapter = test_adapter.new_adapter(adapter_module, _extra(d / "edge.sock", tmp_path))

        async def main() -> bool:
            connected = await adapter.connect()
            await adapter.disconnect()
            return connected

        assert run(main()) is False
    ((_code, message, retryable),) = adapter.fatal_errors
    assert "outbox.sqlite: must be a regular file" in caplog.text and FILE_NAME in message and retryable is True


def test_a_store_failure_in_send_is_a_retryable_failed_result_not_a_raise(adapter_module, tmp_path, caplog):
    r = rig(adapter_module, tmp_path)
    r.adapter.router.bind(SimpleNamespace(send=lambda *_a: True), SCHEMA, r.box)

    async def broken(*_args: object) -> bool:
        raise sqlite3.OperationalError("disk I/O error")

    r.box.send = broken  # type: ignore[method-assign]
    result = run(r.adapter.send("glasses:work", "hi"))
    assert (result.success, result.retryable) == (False, True) and "OperationalError" in caplog.text
    assert "disk I/O error" not in (result.error or "")


def test_an_answers_corr_goes_only_to_the_device_that_sent_the_msg_in(adapter_module):
    adapter = test_adapter.new_adapter(adapter_module, EXTRA)
    adapter.router.bind(SimpleNamespace(send=lambda *_a: True), SCHEMA)
    asker, other = "0123456789abcdef", "fedcba9876543210"
    assert adapter.router.text.dedupe.first(asker, MSG_IN["id"]) is True
    build = adapter.router.answer("glasses:work", "t", MSG_IN["id"])
    assert json.loads(build(asker, 1))["corr"] == MSG_IN["id"] and "corr" not in json.loads(build(other, 1))
