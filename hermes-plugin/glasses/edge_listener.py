# SPDX-License-Identifier: Apache-2.0
"""The edge listener (T1.184b): ``/ws`` and ``/enroll`` are served on the unix socket ``edge.socket`` only, never on
TCP (there is no TCP fallback).

Before ``bind()`` the path is ``lstat``-ed (a symlink is never followed): a stale socket owned by the plugin's own uid
(a connect to it is refused) is unlinked; a socket that accepts the connect belongs to a live instance and, like
anything else at the path (a regular file, a symlink, a directory, a socket of another uid), is left untouched and is
an ``EdgeSocketError``. After ``bind()`` the socket is made mode ``0660``; a failure after the bind closes it and
removes the file. A directory that is missing or not writable (also when ``lstat`` or ``unlink`` says so) is an
``EdgeSocketError`` that names the drop-in the gateway service needs; the plugin never creates
the directory. Each accepted connection is checked first (``edge_auth.transport_allowed``, ``SO_PEERCRED``): a peer of
another uid, or one whose credentials cannot be read (one WARNING per new error type, the type name only), is closed
before a byte of it is read; the app never sees it. ``stop()`` closes the listener and unlinks the socket only while
the path is still the socket this listener bound (same device and inode).
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import socket
import stat
from collections.abc import Callable
from typing import Any, cast

from .edge_auth import transport_allowed

_LOG = logging.getLogger(__name__)

MODE = 0o660
DROP_IN = "ReadWritePaths=-/run/hermes-glasses"
_DIR_ERRORS = (errno.EROFS, errno.ENOENT, errno.EACCES, errno.EPERM, errno.ENOTDIR)


class EdgeSocketError(RuntimeError):
    """``edge.socket`` cannot be served; the path was left as it was found."""


def _os_error(exc: OSError, doing: str) -> EdgeSocketError:
    """The operator-facing error of an ``OSError`` on the socket path: the drop-in hint for a directory problem."""
    if exc.errno in _DIR_ERRORS:
        return EdgeSocketError(
            f"edge.socket: cannot create the socket ({errno.errorcode.get(exc.errno or 0, 'OSError')}); the "
            f"directory must exist and be writable for the gateway service (systemd drop-in {DROP_IN})"
        )
    return EdgeSocketError(f"edge.socket: cannot {doing} ({type(exc).__name__})")


def _unlink_ours(path: str, ident: tuple[int, int] | None) -> None:
    """Unlink *path* only while it is still the socket with device/inode *ident* (never somebody else's file)."""
    try:
        found = os.lstat(path)
        if ident is not None and stat.S_ISSOCK(found.st_mode) and (found.st_dev, found.st_ino) == ident:
            os.unlink(path)
    except OSError:
        return


def _probe(path: str) -> None:
    """Return when nothing listens at *path* (``ECONNREFUSED``: stale). A successful connect is a live socket of
    another instance, and any other outcome cannot be told apart from one: both are an ``EdgeSocketError`` and the
    path is left alone. The connect is non-blocking, because this runs on the event loop: a live listener whose
    backlog is full answers ``EAGAIN`` at once (``BlockingIOError``, not a timeout), and a blocking connect would
    wait for room in the backlog for as long as that listener does not accept."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.setblocking(False)
        try:
            probe.connect(path)
        except (ConnectionRefusedError, FileNotFoundError):
            return
        except OSError:
            raise EdgeSocketError("edge.socket: cannot tell whether another process uses it; left untouched") from None
    raise EdgeSocketError("edge.socket: in use by another process; left untouched")


def _clear_stale(path: str) -> None:
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _os_error(exc, "check the path") from None
    if not stat.S_ISSOCK(found.st_mode) or found.st_uid != os.geteuid():
        raise EdgeSocketError("edge.socket: the path exists and is not a stale socket of this user; left untouched")
    _probe(path)  # a second instance's live socket is not stale: it is never unlinked
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _os_error(exc, "check the path") from None


def bind_socket(path: str) -> tuple[socket.socket, tuple[int, int] | None]:
    """A listening ``AF_UNIX`` socket at *path*, mode ``0660``, with the (device, inode) of the file it created;
    ``EdgeSocketError`` when that is not possible (a socket that was bound is closed and its file removed then)."""
    if not path:  # bind("") would autobind an abstract socket that no edge can reach, with no file to check
        raise EdgeSocketError("edge.socket: the path is empty")
    _clear_stale(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ident: tuple[int, int] | None = None
    try:
        sock.bind(path)
        found = os.lstat(path)
        ident = (found.st_dev, found.st_ino)
        os.chmod(path, MODE)
        sock.listen(128)
    except OSError as exc:
        sock.close()
        _unlink_ours(path, ident)
        raise _os_error(exc, "listen") from None
    sock.setblocking(False)
    return sock, ident


class _Guard(asyncio.Protocol):
    """Checks the peer of a new connection, then hands the connection to the app's protocol (or closes it)."""

    def __init__(self, listener: EdgeListener) -> None:
        self._listener = listener
        self._inner: Any = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not self._listener.admits(transport):
            cast(asyncio.Transport, transport).abort()  # nothing has been read from this connection
            return
        self._inner = self._listener.factory()
        self._inner.connection_made(transport)

    def data_received(self, data: bytes) -> None:
        if self._inner is not None:
            self._inner.data_received(data)

    def eof_received(self) -> bool | None:
        return self._inner.eof_received() if self._inner is not None else None

    def connection_lost(self, exc: Exception | None) -> None:
        if self._inner is not None:
            self._inner.connection_lost(exc)

    def pause_writing(self) -> None:
        if self._inner is not None:
            self._inner.pause_writing()

    def resume_writing(self) -> None:
        if self._inner is not None:
            self._inner.resume_writing()


class EdgeListener:
    """The unix-socket listener at *path* admitting only peers of *uid*; *factory* builds the app's protocol."""

    def __init__(self, path: str, uid: int) -> None:
        self.path, self.uid = path, uid
        self.factory: Callable[[], asyncio.Protocol] = _no_app
        self._server: asyncio.Server | None = None
        self._bound: tuple[int, int] | None = None
        self._warned: set[str] = set()

    def admits(self, transport: asyncio.BaseTransport) -> bool:
        try:
            return transport_allowed(transport, self.uid)
        except Exception as exc:  # noqa: BLE001 - fail closed on unreadable credentials, log the type only
            kind = type(exc).__name__
            level = logging.DEBUG if kind in self._warned else logging.WARNING
            self._warned.add(kind)
            _LOG.log(level, "glasses edge: peer credentials unreadable (%s); closing", kind)
            return False

    async def start(self, factory: Callable[[], asyncio.Protocol]) -> None:
        sock, self._bound = bind_socket(self.path)
        self.factory = factory
        try:
            self._server = await asyncio.get_running_loop().create_unix_server(lambda: _Guard(self), sock=sock)
        except BaseException:
            sock.close()
            self._unlink()
            raise

    async def stop(self) -> None:
        """Stop accepting and unlink the socket this listener created (open connections are the runner's)."""
        if self._server is not None:
            self._server.close()
            self._server = None
        self._unlink()

    def _unlink(self) -> None:
        bound, self._bound = self._bound, None
        _unlink_ours(self.path, bound)


def _no_app() -> asyncio.Protocol:
    raise EdgeSocketError("edge listener: not started")
