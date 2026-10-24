# SPDX-License-Identifier: Apache-2.0
"""Shared parts of the outbox tests: a ``Store``/``Outbox`` over a temp state dir with a fake wall clock, registry
device views, a ``msg.out`` encoder with the real schema, and a fake live link that records what it was given."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server_helpers import SCHEMA

from glasses import outbound
from glasses.envelope_encode import encode
from glasses.outbox import FILE_NAME, Outbox, Store

TEXT = "planted-answer-5531"
ULID = "01JABCDEFGHJKMNPQRSTVWXYZ0"
TS = "2026-10-21T00:00:00Z"
A, B, C = "0123456789abcdef", "fedcba9876543210", "00112233445566ff"


@dataclass(frozen=True)
class Dev:
    device_id: str
    lanes: tuple[str, ...] = ("work",)
    revoked_at: str | None = None


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeLink:
    """A welcomed connection: ``accept`` False makes ``send`` refuse (a full or halted queue)."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    accept: bool = True
    refusals: int = 0

    def send(self, text: str) -> bool:
        if self.accept:
            self.sent.append(json.loads(text))
        return self.accept

    def refused(self) -> None:
        self.refusals += 1


def overflow(seq: int) -> str:
    return encode(outbound.overflow_error((seq, ULID, TS)), SCHEMA)


def answer(text: str = TEXT, chat_id: str = "glasses:work", corr: str | None = None):
    def build(_device_id: str, seq: int) -> str:
        return encode(outbound.msg_out(text, chat_id, corr, (seq, ULID, TS)), SCHEMA)

    return build


def store(state: Path, clock: Clock | None = None) -> Store:
    return Store(state / FILE_NAME, clock or Clock(), overflow)


def outbox(state: Path, clock: Clock | None = None, forgotten: list[str] | None = None) -> Outbox:
    sink = forgotten if forgotten is not None else []
    return Outbox(store(state, clock), sink.append, lambda: [])


def rows(state: Path, table: str = "rows") -> list[tuple[Any, ...]]:
    """What the file holds, read through a separate connection."""
    with sqlite3.connect(state / FILE_NAME) as db:
        return db.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - a fixed table name of the test


def seqs(state: Path, device_id: str) -> list[int]:
    with sqlite3.connect(state / FILE_NAME) as db:
        found = db.execute("SELECT seq FROM rows WHERE device_id=? ORDER BY seq", (device_id,)).fetchall()
    return [seq for (seq,) in found]


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, 20))
