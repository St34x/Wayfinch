# SPDX-License-Identifier: Apache-2.0
"""Outbound answers (T1.09b): what a gateway ``send()`` becomes. Pure core: no I/O, no clock, no Hermes import.

* ``lane_of_chat``: ``chat_id`` must be ``glasses:<lane>`` with ``lane`` in the config ``lanes`` (``lanes: []``: any
  lane name); anything else is refused (``BAD_CHAT``).
* ``recipients``: every registry device that is not revoked and has the lane in its registry ``lanes`` (the device's
  outbound grant; ``GLASSES_ALLOWED_USERS`` stays the gateway's inbound check), and apart from them every revoked
  device, whatever its lanes (the caller purges their outbox rows).
* ``cut``: a ``text`` over ``TEXT_BUDGET`` (2048) UTF-8 bytes is cut on a code-point boundary to at most 2045 bytes
  plus ``…`` (U+2026). The long-reply hand-off (``longRef``, T1.41) replaces the cut later.
* ``msg_out`` / ``overflow_error``: the ``msg.out`` (``kind`` ``answer``, ``final`` true) and the ``error``
  ``outbox_overflow`` frames, both with ``seq``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .config_core import PLATFORM_NAME, GlassesConfig
from .envelope_text import cut_utf8
from .generated.protocol_types import ErrorBody, ErrorFrame, MsgOutFrame

TEXT_BUDGET = 2048  # bytes of msg.out text (protocol/README.md; envelope.schema.json maxBytes)
ELLIPSIS = "\u2026"
BAD_CHAT = "glasses: chat_id must be glasses:<lane> with a configured lane"
NO_RECIPIENT = "glasses: no enrolled device holds this lane"
OVERFLOW_MESSAGE = "outbox full: the oldest frame was dropped"


class DeviceView(Protocol):
    """The part of a registry ``Device`` this module reads."""

    @property
    def device_id(self) -> str: ...

    @property
    def lanes(self) -> tuple[str, ...]: ...

    @property
    def revoked_at(self) -> str | None: ...


def lane_of_chat(chat_id: object, cfg: GlassesConfig) -> str | None:
    """The lane of a ``glasses:<lane>`` chat id, or None when the id is malformed or the lane is not configured."""
    prefix = f"{PLATFORM_NAME}:"
    if not isinstance(chat_id, str) or not chat_id.startswith(prefix):
        return None
    lane = chat_id[len(prefix) :]
    if not lane or (cfg.lanes and lane not in cfg.lanes):
        return None
    return lane


def recipients(devices: Iterable[DeviceView], lane: str) -> tuple[list[str], list[str]]:
    """``(to, revoked)``: the active devices holding *lane*, and every revoked device, whatever its lanes (to purge)."""
    to: list[str] = []
    revoked: list[str] = []
    for device in devices:
        if device.revoked_at is not None:
            revoked.append(device.device_id)
        elif lane in device.lanes:
            to.append(device.device_id)
    return to, revoked


def cut(text: str) -> str:
    """*text* unchanged when it fits ``TEXT_BUDGET`` bytes, else cut on a code-point boundary and ended by ``…``. A
    lone surrogate (which ``encode`` refuses) becomes ``?`` first."""
    text = text.encode("utf-8", "replace").decode("utf-8")
    if len(text.encode()) <= TEXT_BUDGET:
        return text
    return cut_utf8(text, TEXT_BUDGET - len(ELLIPSIS.encode())) + ELLIPSIS


def msg_out(text: str, chat_id: str, corr: str | None, header: tuple[int, str, str]) -> MsgOutFrame:
    """The ``msg.out`` answer frame; *header* is ``(seq, id, ts)``."""
    seq, frame_id, ts = header
    frame: MsgOutFrame = {
        "v": 1,
        "id": frame_id,
        "type": "msg.out",
        "ts": ts,
        "seq": seq,
        "body": {"kind": "answer", "text": text, "chatId": chat_id, "final": True},
    }
    if corr is not None:
        frame["corr"] = corr
    return frame


def overflow_error(header: tuple[int, str, str]) -> ErrorFrame:
    """The outboxed ``error`` ``outbox_overflow`` (with ``seq``, README Delivery); *header* is ``(seq, id, ts)``."""
    seq, frame_id, ts = header
    body: ErrorBody = {"code": "outbox_overflow", "message": OVERFLOW_MESSAGE}
    return {"v": 1, "id": frame_id, "type": "error", "ts": ts, "seq": seq, "body": body}
