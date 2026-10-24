# SPDX-License-Identifier: Apache-2.0
"""Shared parts of the frame check's tests (``revoked_ids``): a registry file whose content the test sets, written by an
atomic rename as the signer writes it, and a reader that refuses a file of a loose mode as the production reader does
(mode 0640 or stricter); plus waits on the check's read."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
from pathlib import Path

from outbox_helpers import Dev

from glasses.registry_parse import RegistryCorruptError
from glasses.revoked_ids import RevokedIds

X, Y = "1111111111111111", "2222222222222222"
REVOKED = "2026-10-21T00:00:00Z"


class FakeFile:
    """A registry file; ``read`` counts calls, may raise (``error``, ``fail_once``) or wait on ``hold``."""

    def __init__(self, path: Path) -> None:
        self.path, self.devices, self.reads, self.error = path, [Dev(X, revoked_at=REVOKED), Dev(Y)], 0, False
        self.fail_once: BaseException | None = None
        self.hold: threading.Event | None = None  # set: the read waits for it (after taking the content)
        self.started = threading.Event()
        self.write()

    def write(self, mode: int = 0o640) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([d.device_id for d in self.devices]) + " " * self.reads, encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, self.path)  # a new inode, as the signer's atomic rename

    def read(self) -> list[Dev]:
        self.reads += 1
        devices = list(self.devices)  # the content of the version the read opened
        self.started.set()
        if self.hold is not None:
            self.hold.wait(10)
        if self.fail_once is not None:
            error, self.fail_once = self.fail_once, None
            raise error
        if self.error:
            raise RegistryCorruptError("devices.json: not valid JSON")
        if stat.S_IMODE(os.stat(self.path).st_mode) & 0o037:
            raise PermissionError("devices.json: mode too open")
        return devices


async def settled(ids: RevokedIds) -> None:
    while ids._pending is not None:
        await asyncio.sleep(0.005)
