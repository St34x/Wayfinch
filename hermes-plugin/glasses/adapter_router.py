# SPDX-License-Identifier: Apache-2.0
"""The adapter's frame router (T1.08, T1.09b; moved out of ``adapter.py`` unchanged, T1.249): the server's
``on_frame`` and the encoder of each reply's ``msg.out``. Its records keep ``adapter``'s logger name.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from gateway.platforms.event import MessageEvent, MessageType

from . import inbound, outbound
from .connections import ConnectionTable
from .envelope import FrameSchema
from .envelope_encode import encode
from .generated.protocol_types import Frame
from .outbox import Outbox
from .registry import Device
from .server import _now, _ulid

if TYPE_CHECKING:  # annotations only: adapter imports this module
    from .adapter import GlassesAdapter

logger = logging.getLogger(__name__.removesuffix("_router"))  # adapter's logger: the records keep their name


def _unrouted(source: object) -> bool:
    """The gateway marks a source whose ``profile_routes`` target it does not serve (``build_source``)."""
    return getattr(source, "profile_route_rejected", False) is True


class _FrameRouter:
    """The server's ``on_frame`` (T1.08): ``msg.in`` through ``inbound.Inbound``; any other type is logged by type.
    ``bind`` gives it the running app's connection table and schema for the ``error`` frames."""

    def __init__(self, adapter: GlassesAdapter) -> None:
        self._adapter = adapter
        self._send: Callable[[str, str], bool] | None = None
        self._schema: FrameSchema | None = None
        self.outbox: Outbox | None = None
        ports = inbound.Ports(self._source, _unrouted, self._deliver, self._refuse)
        self.text = inbound.Inbound(adapter.settings, time.monotonic, ports)

    def bind(self, table: ConnectionTable, schema: FrameSchema, outbox: Outbox | None = None) -> None:
        self._send, self._schema, self.outbox = table.send, schema, outbox

    async def on_frame(self, device: Device, frame: Frame) -> None:
        """Route one authenticated frame. A ``msg.in`` awaits the gate check: a stalled one holds this device's frame
        loop up to ``gate.LOCK_TIMEOUT`` + 1 = 3 s per ``msg.in`` (fails closed; a phone cannot trigger the stall)."""
        if frame["type"] == "ack" and self.outbox is not None:
            self.outbox.ack_soon(device.device_id, frame["body"]["upToSeq"])
            return
        if frame["type"] != "msg.in":
            logger.debug("glasses adapter: %s frame not dispatched (dispatch not built yet)", frame["type"])
            return
        outcome = await self.text.msg_in(device.device_id, device.lanes, frame)
        logger.debug("glasses adapter: msg.in %s", outcome)  # the outcome only, never the text

    def _source(self, chat_id: str, user_id: str) -> object:
        return self._adapter.build_source(chat_id=chat_id, chat_type="dm", user_id=user_id)

    async def _deliver(self, text: inbound.TextIn, source: object) -> bool:  # False: the open gate dropped the event
        return await self._adapter.hand_on(
            MessageEvent(
                text=text.text,
                message_type=MessageType.TEXT,
                user_id=text.user_id,
                source=source,
                message_id=text.message_id,
                # Glasses text stays conversational: never a gateway command, never an answer to a pending
                # approval, clarify or confirm prompt (approvals only through the T1.10 card: once or deny).
                allow_gateway_control=False,
            )
        )

    def _refuse(self, device_id: str, refusal: inbound.Refusal, corr: str) -> None:
        # The server's private _ulid/_now on purpose (defined in server_session.py, re-exported by server): every frame
        # on the edge takes its id and time from there.
        if self._send is not None and self._schema is not None:
            self._send(device_id, encode(inbound.error_for(refusal, corr, _ulid(), _now()), self._schema))

    def answer(self, chat_id: str, content: str, reply_to: str | None) -> Callable[[str, int], str]:
        """The encoder of one reply's ``msg.out`` per (device, seq); ``corr`` only for a device that sent *reply_to*."""
        text, schema = outbound.cut(content), self._schema
        if schema is None:
            raise RuntimeError("glasses adapter: frame router not bound")
        if text != content:  # the byte counts only, never the text
            sizes = len(content.encode(errors="replace")), len(text.encode())
            logger.info("glasses adapter: reply cut from %d to %d bytes", *sizes)
        senders = self.text.dedupe.senders(reply_to) if reply_to else frozenset()

        def build(device_id: str, seq: int) -> str:
            corr = reply_to if device_id in senders else None
            return encode(outbound.msg_out(text, chat_id, corr, (seq, _ulid(), _now())), schema)

        return build
