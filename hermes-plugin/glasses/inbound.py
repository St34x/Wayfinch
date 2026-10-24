# SPDX-License-Identifier: Apache-2.0
"""Inbound text (T1.08): what a ``msg.in`` of an authenticated device becomes. Pure core: no Hermes import, no I/O and
no clock of its own; the adapter passes the clock and its Hermes calls in (``Ports``).

``Inbound.msg_in(device_id, device_lanes, frame)``, in this order:

1. the lane: the frame's ``lane``, else ``home_lane``. It must be one of the device's registry lanes and one of the
   config ``lanes`` (``lanes: []`` = the device's lanes only), else ``error`` ``lane_forbidden``;
2. the source: ``Ports.source("glasses:<lane>", device_id)`` (the adapter's ``build_source``, which stamps the profile
   of ``gateway.profile_routes``). A source the gateway marks as routed to a profile it does not serve
   (``Ports.unrouted``) is ``error`` ``lane_unrouted``: the gateway would drop it with a log line only;
3. the dedupe: an envelope ``id`` the same device sent within the last ``DEDUPE_WINDOW_S`` (10 min) is dropped and
   nothing is sent. In memory, per device, at most ``DEDUPE_MAX_IDS`` (1024) ids, the oldest dropped first; a restart
   forgets it. An id counts only once its event was handed on: it is held while ``deliver`` is pending (a copy that
   arrives meanwhile is dropped) and released again when ``deliver`` raises, is cancelled or reports that the gateway
   dropped the event (allow-all gate open): the phone's re-send is then a new attempt;
4. the event: ``Ports.deliver(TextIn, source)`` (the adapter builds the gateway's text event and awaits
   ``GlassesAdapter.hand_on``, which calls ``handle_message``; that only spawns the agent turn). The event never
   controls the gateway: it is not parsed as a gateway command and never answers a pending approval or other prompt
   (``allow_gateway_control=False``).

``user_id`` is always the id of the authenticated device, never a value of the frame. The allowlist is the gateway's:
it applies ``GLASSES_ALLOWED_USERS`` to ``user_id``. An ``error`` frame (``error_for``) is connection-scoped: no
``seq``, ``corr`` = the ``msg.in`` id, a fixed ``message`` per code. No text of the message reaches it or a result.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .config_core import PLATFORM_NAME, GlassesConfig
from .generated.protocol_types import ErrorFrame, MsgInFrame, Timestamp, Ulid

DEDUPE_WINDOW_S = 600.0
DEDUPE_MAX_IDS = 1024
HANDED_ON = "handed on"
NOT_HANDED_ON = "not handed on"  # the gateway dropped the event (allow-all gate open): the id is released
DUPLICATE = "duplicate"


@dataclass(frozen=True)
class Refusal:
    code: str
    message: str  # fixed per code: never input


LANE_FORBIDDEN = Refusal("lane_forbidden", "lane not allowed for this device")
LANE_UNROUTED = Refusal("lane_unrouted", "lane not routed to a profile this server serves")


@dataclass(frozen=True)
class TextIn:
    """The fields of the gateway text event of one ``msg.in``."""

    chat_id: str
    user_id: str
    text: str
    message_id: str


@dataclass(frozen=True)
class Ports:
    """The adapter's side: ``source(chat_id, user_id)`` builds the gateway source, ``unrouted(source)`` says whether
    the gateway refused its route, ``deliver`` hands the event on, ``refuse(device_id, refusal, corr)`` sends the
    ``error`` frame on the device's connection."""

    source: Callable[[str, str], object]
    unrouted: Callable[[object], bool]
    deliver: Callable[[TextIn, object], Awaitable[bool]]  # True: handed on; False: the gateway dropped the event
    refuse: Callable[[str, Refusal, str], None]


class Dedupe:
    """Envelope ids seen per device within ``DEDUPE_WINDOW_S`` seconds of *clock*, at most ``DEDUPE_MAX_IDS`` each."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._seen: dict[str, OrderedDict[str, float]] = {}

    def first(self, device_id: str, frame_id: str) -> bool:
        """True (and remembered) unless the device sent *frame_id* within the window."""
        now = self._clock()
        seen = self._seen.setdefault(device_id, OrderedDict())
        while seen and next(iter(seen.values())) <= now - DEDUPE_WINDOW_S:
            seen.popitem(last=False)
        if frame_id in seen:
            return False
        seen[frame_id] = now
        if len(seen) > DEDUPE_MAX_IDS:
            seen.popitem(last=False)
        return True

    def discard(self, device_id: str, frame_id: str) -> None:
        """Release *frame_id* of the device (its hand-on failed, so a re-send is a new attempt)."""
        self._seen.get(device_id, OrderedDict()).pop(frame_id, None)

    def forget(self, device_id: str) -> None:
        """Drop every id of the device (its purge on revocation, T1.09b)."""
        self._seen.pop(device_id, None)

    def senders(self, frame_id: str) -> frozenset[str]:
        """The devices whose ``msg.in`` *frame_id* is in the table (an answer's ``corr`` goes to them only, T1.09b)."""
        return frozenset(device_id for device_id, seen in self._seen.items() if frame_id in seen)


def lane_of(frame: MsgInFrame, device_lanes: tuple[str, ...], cfg: GlassesConfig) -> str | None:
    """The lane the frame goes to, or None when it is not allowed (``lane_forbidden``)."""
    lane = frame.get("lane") or cfg.home_lane
    if not lane or lane not in device_lanes or (cfg.lanes and lane not in cfg.lanes):
        return None
    return lane


def error_for(refusal: Refusal, corr: Ulid, frame_id: Ulid, ts: Timestamp) -> ErrorFrame:
    """The connection-scoped ``error`` frame answering the ``msg.in`` *corr* (no ``seq``)."""
    return {
        "v": 1,
        "id": frame_id,
        "type": "error",
        "ts": ts,
        "corr": corr,
        "body": {"code": refusal.code, "message": refusal.message},
    }


class Inbound:
    """The ``msg.in`` path of one adapter (module docstring); ``dedupe`` is its table."""

    def __init__(self, cfg: GlassesConfig, clock: Callable[[], float], ports: Ports) -> None:
        self._cfg, self._ports = cfg, ports
        self.dedupe = Dedupe(clock)

    async def msg_in(self, device_id: str, device_lanes: tuple[str, ...], frame: MsgInFrame) -> str:
        """Route one ``msg.in``; the outcome for a log line (an error code, ``DUPLICATE`` or ``HANDED_ON``)."""
        lane = lane_of(frame, device_lanes, self._cfg)
        if lane is None:
            return self._refuse(device_id, LANE_FORBIDDEN, frame)
        chat_id = f"{PLATFORM_NAME}:{lane}"
        source = self._ports.source(chat_id, device_id)
        if self._ports.unrouted(source):
            return self._refuse(device_id, LANE_UNROUTED, frame)
        if not self.dedupe.first(device_id, frame["id"]):
            return DUPLICATE
        text_in = TextIn(chat_id, device_id, frame["body"]["text"], frame["id"])
        handed_on = False
        try:
            handed_on = await self._ports.deliver(text_in, source)
        finally:
            if not handed_on:  # raised, cancelled or dropped by the gate: the phone's re-send must be processed
                self.dedupe.discard(device_id, frame["id"])
        return HANDED_ON if handed_on else NOT_HANDED_ON

    def _refuse(self, device_id: str, refusal: Refusal, frame: MsgInFrame) -> str:
        self._ports.refuse(device_id, refusal, frame["id"])
        return refusal.code
