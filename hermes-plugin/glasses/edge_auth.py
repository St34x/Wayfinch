# SPDX-License-Identifier: Apache-2.0
"""The edge-peer check (T1.184b): only the TLS edge's system user may talk to ``/ws`` and ``/enroll``.

``edge_uid(name)`` resolves ``edge.user`` once at start; an unset or unknown user, root, or the plugin's own uid is
``EdgeUserRefused`` (the plugin's own uid would admit every process of the gateway user). ``peer_uid(transport)`` is
the uid of the peer process of an accepted unix-socket connection, read with ``getsockopt(SOL_SOCKET, SO_PEERCRED)``
on the transport's socket (no socket: ``OSError``). ``transport_allowed`` is the check the listener runs when a
connection is accepted (``edge_listener``), ``edge_peer_allowed(uid)`` the same check as the request predicate the
server calls before anything else (``ServerDeps.peer_allowed``): True only for a peer whose uid is the edge uid.
Credentials that cannot be read raise; both callers refuse then (fail closed). No header is ever read here.
"""

from __future__ import annotations

import os
import pwd
import socket
import struct
from asyncio import BaseTransport
from collections.abc import Callable

from aiohttp import web

_UCRED = struct.Struct("3i")  # struct ucred: pid, uid, gid


class EdgeUserRefused(RuntimeError):
    """``edge.user`` cannot be the only admitted peer (unset, unknown, root or the plugin's own user)."""


def edge_uid(name: str) -> int:
    """The uid of *name*, which must be a known user other than root and the plugin's own user."""
    if not name:
        raise EdgeUserRefused("edge.user: not set")
    try:
        uid = pwd.getpwnam(name).pw_uid
    except KeyError:
        raise EdgeUserRefused("edge.user: no such user") from None
    if uid in (0, os.geteuid()):
        raise EdgeUserRefused("edge.user: must not be root or the user the gateway runs as")
    return uid


def peer_uid(transport: BaseTransport) -> int:
    """The ``SO_PEERCRED`` uid of the connection behind *transport*; ``OSError`` when it cannot be read."""
    sock = transport.get_extra_info("socket")
    if sock is None:
        raise OSError("no socket")
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    return int(_UCRED.unpack(raw)[1])


def transport_allowed(transport: BaseTransport, uid: int) -> bool:
    """True only when the peer of *transport* runs as *uid* (raises when the credentials cannot be read)."""
    return peer_uid(transport) == uid


def edge_peer_allowed(uid: int) -> Callable[[web.BaseRequest], bool]:
    """The request predicate of ``/ws`` and ``/enroll``: the peer of the request's connection runs as *uid*."""

    def allowed(request: web.BaseRequest) -> bool:
        transport = request.transport
        return transport is not None and transport_allowed(transport, uid)

    return allowed
