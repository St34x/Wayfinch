# SPDX-License-Identifier: Apache-2.0
"""Socket client of the device-CA signer, the only plugin code that talks to it (the wire format is
``signer_protocol``; this module adds the socket, the checks and the deadlines). Callers today: ``glasses-admin crl``
(``admin_crl``) and the ``/enroll`` relay (``enroll``; built, mounted on the HTTP app by T1.07, planned).

* ``client_from_config(cfg)`` builds a ``SignerClient`` from ``signer.socket`` and ``signer.user``. A key that is not
  set raises ``NotConfigured`` naming it; a user that is unknown, root or the plugin's own user raises
  ``SignerUserRefused``, because the peer check would then prove nothing. ``public_file(cfg, name)`` is the path of a
  file the signer publishes in ``signer.public_dir`` (contract names only).
* Before connecting, the request is read back from its own ``line`` (one strict JSON object, one trailing newline,
  at most ``REQUEST_CAP`` bytes, an op in ``OPS``, exactly that op's keys, the request's own op and rid); anything
  else raises a ``RequestRefused`` kind and nothing is sent.
* Every call, before a byte is sent: ``lstat`` of the socket path (a symlink, a non-socket or an owner other than the
  signer user raises ``SocketRefused``; only the last path component is checked), connect, then the peer uid from
  ``SO_PEERCRED`` (another uid raises ``PeerRefused``). Then one request line, and the answer read until the signer
  closes (brief section 4: one line, then close): a byte after the newline is refused (``AnswerNotJson``), and a
  signer that keeps the connection open runs into the deadline.
* One deadline per call, from connect to the end of the answer: connect at most 2 s, ``enrol`` 340 s, every other op
  10 s (``SignerTimeout``). No call is retried; the caller decides. Every failure is a ``SignerError`` kind raised
  outside any handler, so it carries no ``__cause__`` or ``__context__``.
* ``enrol`` returns an ``EnrolResult`` whose bearer ``token`` stays out of ``repr``/``str``; a refused answer is
  raised afresh, so no frame of its traceback holds the token (``enrol``'s own frame holds the ``code``).
* ``Timeouts`` are finite and in (0, ``MAX_TIMEOUT_S``]; ``signer_uid`` and ``peer_uid`` are exact ``int`` >= 0.
"""

from __future__ import annotations

import json
import os
import pwd
import secrets
import socket
import stat
import struct
import time
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path
from typing import Any

from . import signer_protocol as proto
from .config_core import GlassesConfig

PUBLIC_FILES = frozenset({proto.PUBLIC_CA_BUNDLE, proto.PUBLIC_CRL, proto.PUBLIC_REGISTRY})
_UCRED = struct.Struct("3i")  # struct ucred: pid, uid, gid
MAX_TIMEOUT_S = 600.0  # above the 340 s enrol deadline; larger values overflow settimeout


class NotConfigured(proto.SignerError):
    message = "signer key not set"

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key
        self.args = (f"{key} is not set in the glasses config",)


class SignerUserRefused(proto.SignerError):
    message = "signer.user is unknown, root or the plugin's own user"


class SocketRefused(proto.SignerError):
    message = "signer socket is a symlink, not a socket, or not owned by the signer user"


class PeerRefused(proto.SignerError):
    message = "signer socket peer is not the signer user"


class SignerUnavailable(proto.SignerError):
    message = "signer connection failed"


class SignerTimeout(proto.SignerError):
    message = "signer did not answer in time"


class UnknownPublicFile(proto.SignerError):
    message = "not a file the signer publishes"


def read_peer_uid(sock: socket.socket) -> int:
    """The uid the kernel reports for the peer of the connected unix socket *sock*."""
    return int(_UCRED.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size))[1])


@dataclass(frozen=True)
class Timeouts:
    """Seconds per call phase: each a finite number in (0, ``MAX_TIMEOUT_S``], else ``ValueError``/``TypeError``."""

    connect_s: float = proto.CONNECT_TIMEOUT_S
    op_s: float = proto.OP_TIMEOUT_S
    enrol_s: float = proto.ENROL_TIMEOUT_S

    def __post_init__(self) -> None:
        for value in (self.connect_s, self.op_s, self.enrol_s):
            if type(value) not in (int, float):
                raise TypeError("a signer timeout is a number of seconds")
            if not 0 < value <= MAX_TIMEOUT_S:  # NaN fails too
                raise ValueError("a signer timeout must be finite, above 0 and at most MAX_TIMEOUT_S")


@dataclass(frozen=True)
class EnrolResult:
    """An enrolled device. ``token`` is its bearer secret: never in ``repr``/``str``, never logged."""

    device_id: str
    cert: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class SignerClient:
    """One method per op the plugin may send; each returns the checked result fields of the answer. ``peer_uid`` only
    sets the uid the peer must have (default: ``signer_uid``); the peer check itself always runs."""

    socket_path: Path
    signer_uid: int
    _: KW_ONLY
    peer_uid: int | None = None
    timeouts: Timeouts = Timeouts()

    def __post_init__(self) -> None:
        for uid in (self.signer_uid, self.peer_uid if self.peer_uid is not None else 0):
            if type(uid) is not int or uid < 0:  # no bool, no int subclass with a lying __eq__
                raise TypeError("signer_uid and peer_uid are uids (an exact int >= 0)")

    def status(self) -> dict[str, object]:
        return self._call("status")

    def enrol(self, spki: str, code: str, sig: str) -> EnrolResult:
        result = self._call("enrol", {"spki": spki, "code": code, "sig": sig})
        return EnrolResult(str(result["device_id"]), str(result["cert"]), str(result["token"]))

    def revoke(self, device_id: str) -> dict[str, object]:
        return self._call("revoke", {"device_id": device_id})

    def crl_generate(self) -> dict[str, object]:
        return self._call("crl_generate")

    def window_status(self) -> dict[str, object]:
        return self._call("window_status")

    def window_close(self) -> dict[str, object]:
        return self._call("window_close")

    def rotate_status(self) -> dict[str, object]:
        return self._call("rotate_status")

    def _call(self, op: str, fields: Mapping[str, str] | None = None) -> dict[str, object]:
        request = _checked(proto.build_request(op, secrets.token_hex(16), fields))
        deadline = time.monotonic() + (self.timeouts.enrol_s if request.op == "enrol" else self.timeouts.op_s)
        self._check_socket_file()
        line = b""
        error: proto.SignerError | None = None
        try:
            line = self._exchange(request, deadline)
        except TimeoutError:
            error = SignerTimeout()
        except (OSError, ValueError):
            error = SignerUnavailable()
        if error is not None:
            raise error  # outside the handler: the OSError (it names the path) is not attached
        try:
            return proto.decode_answer(request, line)
        except proto.AnswerRejected as rejected:
            error = type(rejected)()
        line = b""  # a refused enrol answer may hold a live token: keep it out of the traceback's locals
        raise error

    def _exchange(self, request: proto.Request, deadline: float) -> bytes:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(min(self.timeouts.connect_s, _left(deadline)))
            sock.connect(str(self.socket_path))
            expected = self.signer_uid if self.peer_uid is None else self.peer_uid
            if read_peer_uid(sock) != expected:
                raise PeerRefused
            sock.settimeout(_left(deadline))
            sock.sendall(request.line)
            return _receive(sock, deadline)

    def _check_socket_file(self) -> None:
        try:
            info: os.stat_result | None = os.lstat(self.socket_path)
        except (OSError, ValueError):  # ValueError: a NUL in the path
            info = None
        if info is None:
            raise SignerUnavailable
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.signer_uid:
            raise SocketRefused  # lstat: a symlink is never a socket


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate key")  # caught in _read_back
    return obj


def _read_back(line: object) -> dict[str, Any] | None:
    """The one JSON object of a request line that ends in its only newline and fits ``REQUEST_CAP``, else None."""
    if type(line) is not bytes or len(line) > proto.REQUEST_CAP or line.find(b"\n") != len(line) - 1:
        return None
    try:
        message = json.loads(line[:-1].decode("ascii"), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        return None
    return message if isinstance(message, dict) else None


def _checked(request: proto.Request) -> proto.Request:
    """``request`` rebuilt from its own ``line`` (defence in depth over the self-checking ``Request``): the rebuilt
    request must encode to the same bytes and carry the request's own op and rid, else ``RequestRefused``."""
    message = _read_back(request.line)
    if message is None:
        raise proto.RequestRefused
    version, op, rid = message.pop("v", None), message.pop("op", None), message.pop("rid", None)
    rebuilt = proto.Request(op, rid, message)  # UnknownOp / InvalidField (the values are not trusted to be str)
    same = type(request.op) is str and type(request.rid) is str and (op, rid) == (request.op, request.rid)
    if type(version) is not int or not same or rebuilt.line != request.line:
        raise proto.RequestRefused
    return rebuilt


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise SignerTimeout
    return left


def _receive(sock: socket.socket, deadline: float) -> bytes:
    """Bytes until the signer closes, or one byte over the cap (``decode_answer`` judges: one line, nothing after)."""
    buffer = bytearray()
    while len(buffer) <= proto.ANSWER_CAP:
        sock.settimeout(_left(deadline))
        chunk = sock.recv(proto.ANSWER_CAP + 1 - len(buffer))
        if not chunk:
            break
        buffer += chunk
    return bytes(buffer)


def _setting(value: str, key: str) -> str:
    if not value:
        raise NotConfigured(key)
    return value


def client_from_config(cfg: GlassesConfig) -> SignerClient:
    """The client for ``signer.socket`` and ``signer.user``; typed errors when either is unusable."""
    path, user = _setting(cfg.signer_socket, "signer.socket"), _setting(cfg.signer_user, "signer.user")
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        uid = None
    if uid is None or uid in (0, os.geteuid()):
        raise SignerUserRefused
    return SignerClient(Path(path), uid)


def _public_dir(cfg: GlassesConfig) -> Path:
    """``signer.public_dir``; the one place a signer public path comes from (the test guard checks it here)."""
    return Path(_setting(cfg.signer_public_dir, "signer.public_dir"))


def public_file(cfg: GlassesConfig, name: str) -> Path:
    """``signer.public_dir``/*name* for one of ``PUBLIC_FILES``; the file is not opened here."""
    if name not in PUBLIC_FILES:
        raise UnknownPublicFile
    return _public_dir(cfg) / name
