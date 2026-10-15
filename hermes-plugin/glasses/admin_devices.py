# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin revoke``: revoke a device through the signer, then the running plugin's admin socket.

    python -m glasses.admin revoke DEVICE_ID [--config FILE] [--hermes-home DIR]

Order: the device id is checked (16 lower-case hex, else exit 2 and nothing is sent); the signer's ``revoke`` op
(``signer_client``, one 10 s deadline, no retry) writes the tombstone and the CRL; then ``$state_dir/admin.sock``
(wire format ``glasses.admin_protocol``) asks the plugin for the live cut. Any signer failure, ``unknown_device``
included (also an unexpected exception in the signer call), still calls the socket: the cut only narrows access and
covers a signer whose registry was lost while the plugin holds a session for that id; the signer leg then counts as
not confirmed and the exit is 1. The server half of the socket is T1.63a (planned). Every line is fixed
text; no traceback, path, answer text or exception text is printed, apart from the config error lines shared with
the other subcommands; the command writes no file. A config with errors stops revoke before both steps: fix the
config first (``glasses-admin config check``), or the open session stays until a gateway restart. Exit status:
0 = signer and plugin confirmed, 1 = a leg not confirmed (``unknown_device`` is never 0) or a config with
errors, 2 = a bad device id, an unreadable config, a missing module or a usage error.
"""

from __future__ import annotations

import argparse
import os
import socket
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from . import admin_protocol, signer_client
from . import signer_protocol as proto
from .admin_common import EXIT_FAILED, EXIT_OK, EXIT_UNREADABLE, ConfigFileError, ConfigHasErrors, glasses_config
from .config import DependencyMissing, GlassesConfig, resolve_state_dir
from .signer_client import read_peer_uid

MAX_SOCKET_PATH = 107  # bytes: sun_path holds 108 including the terminating NUL
_CONFIG_ERRORS = (
    "A config with errors stops revoke before both steps: fix the config first "
    "(`glasses-admin config check`), or the open session stays until a gateway restart."
)
_EPILOG = (
    "A signer that does not know the device still gets the plugin's live cut asked for; the exit is then 1, "
    f"never 0. {_CONFIG_ERRORS}"
)
_HELP = (
    "revoke a device: the signer's tombstone and CRL first, then the plugin's live cut "
    "(exit 0 both confirmed; 1 a leg not confirmed, unknown device or config errors; "
    "2 bad device id, unreadable config, missing module or usage error)"
)


class AdminSocketRefused(admin_protocol.AdminError):
    message = "the admin socket is not a socket of this user"


class AdminUnreachable(admin_protocol.AdminError):
    message = "the plugin is not reachable (is the gateway running?)"


class AdminTimeout(admin_protocol.AdminError):
    message = "the plugin did not answer in time"


def _check_socket_file(path: Path) -> None:
    if len(os.fsencode(path)) > MAX_SOCKET_PATH:
        raise AdminSocketRefused
    try:
        info: os.stat_result | None = os.lstat(path)
    except (OSError, ValueError):  # ValueError: a NUL in the path
        info = None
    if info is None:
        raise AdminUnreachable
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise AdminSocketRefused  # lstat: a symlink is never a socket


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise AdminTimeout
    return left


def _exchange(path: Path, request: bytes, deadline: float) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(min(admin_protocol.CONNECT_TIMEOUT_S, _left(deadline)))
        sock.connect(str(path))
        if read_peer_uid(sock) != os.geteuid():
            raise AdminSocketRefused
        sock.settimeout(_left(deadline))
        sock.sendall(request)
        buffer = bytearray()
        while len(buffer) <= admin_protocol.ANSWER_CAP:
            sock.settimeout(_left(deadline))
            chunk = sock.recv(admin_protocol.ANSWER_CAP + 1 - len(buffer))
            if not chunk:
                break
            buffer += chunk
        return bytes(buffer)


def call_admin_socket(state_dir: Path, device_id: str) -> dict[str, int]:
    """Ask the plugin at ``state_dir/admin.sock`` to revoke *device_id* now; returns its counts. No retry."""
    request = admin_protocol.revoke_request(device_id)
    path = state_dir / admin_protocol.SOCKET_NAME
    _check_socket_file(path)
    deadline = time.monotonic() + admin_protocol.CALL_TIMEOUT_S
    data = b""
    error: admin_protocol.AdminError | None = None
    try:
        data = _exchange(path, request, deadline)
    except TimeoutError:
        error = AdminTimeout()
    except (OSError, ValueError):
        error = AdminUnreachable()
    if error is not None:
        raise error  # outside the handler: the OSError (it names the path) is not attached
    return admin_protocol.decode_revoke_answer(device_id, data)


def _refusal_line(exc: proto.SignerRefusal) -> str:
    if exc.code == "crl_failed" and exc.registry_revoked is True:
        return "error: revoke: the signer wrote the tombstone but could not rebuild the CRL (crl_failed)"
    return f"error: revoke: the signer refused ({exc.code})"


def _signer_leg(cfg: GlassesConfig, device_id: str, out: TextIO) -> bool:
    """True = signer confirmed, False = not confirmed (``unknown_device`` too: the live cut still runs)."""
    try:  # through the module attribute: tests patch it to a client on the fake signer
        result = signer_client.client_from_config(cfg).revoke(device_id)
    except signer_client.NotConfigured as exc:
        print(f"error: {exc}", file=out)
        return False
    except proto.SignerRefusal as exc:
        if exc.code == "unknown_device":
            print("error: revoke: the signer does not know this device", file=out)
            return False
        print(_refusal_line(exc), file=out)
        return False
    except proto.SignerError as exc:
        print(f"error: revoke: {exc.message}", file=out)
        return False
    except Exception:  # noqa: BLE001 - a signer-client bug must not skip the live cut; no exception text
        print("error: revoke: the signer call failed (unexpected error)", file=out)
        return False
    number = result["crl_number"]
    if result["already_revoked"] is True:
        print(f"signer: {device_id} was already revoked, CRL number {number}", file=out)
    else:
        print(f"signer: revoked {device_id}, CRL number {number}", file=out)
    return True


def _plugin_leg(state_dir: Path, device_id: str, out: TextIO) -> bool:
    try:
        counts = call_admin_socket(state_dir, device_id)
    except admin_protocol.AdminRefusal as exc:
        print(f"error: revoke: the plugin refused ({exc.code})", file=out)
        return False
    except admin_protocol.AdminError as exc:
        print(f"error: revoke: {exc.message}", file=out)
        return False
    shown = ", ".join(f"{name}={counts[name]}" for name in sorted(counts))
    print(f"plugin: live cut confirmed for {device_id} ({shown})", file=out)
    return True


def _summary(device_id: str, signer_ok: bool, plugin_ok: bool) -> str:
    if signer_ok and plugin_ok:
        return f"revoke: {device_id} revoked (signer and plugin confirmed)"
    if plugin_ok:
        return "error: revoke: revoked in the plugin, not confirmed by the signer"
    if signer_ok:
        return (
            "error: revoke: revoked by the signer, new connections are refused; "
            "an open session stays until the plugin's live cut or a gateway restart"
        )
    return "error: revoke: not confirmed by the signer or the plugin"


def revoke(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    device_id = args.device_id
    if not admin_protocol.is_device_id(device_id):
        print("error: revoke: not a device id (16 lower-case hex)", file=out)
        return EXIT_UNREADABLE
    try:
        cfg, home = glasses_config(args, env)
    except (ConfigFileError, DependencyMissing) as exc:
        print(f"error: {exc}", file=out)
        return EXIT_UNREADABLE
    except ConfigHasErrors as exc:
        print(f"error: revoke: {exc}", file=out)
        print(
            "error: revoke: neither the signer nor the plugin was asked; fix the config first, "
            "or the open session stays until a gateway restart",
            file=out,
        )
        return EXIT_FAILED
    signer_ok = _signer_leg(cfg, device_id, out)
    plugin_ok = _plugin_leg(resolve_state_dir(cfg, home), device_id, out)
    print(_summary(device_id, signer_ok, plugin_ok), file=out)
    return EXIT_OK if signer_ok and plugin_ok else EXIT_FAILED


def register(commands: Any) -> None:
    """Add ``revoke`` to the ``glasses-admin`` subcommands (*commands* from ``add_subparsers``)."""
    cmd = commands.add_parser("revoke", help=_HELP, description=_HELP, epilog=_EPILOG)
    cmd.add_argument("device_id", metavar="DEVICE_ID", help="the device id (16 lower-case hex)")
    cmd.add_argument("--config", type=Path, help="Hermes config.yaml (default: $HERMES_HOME/config.yaml)")
    cmd.add_argument("--hermes-home", type=Path, help="Hermes home (default: $HERMES_HOME, else ~/.hermes)")
    cmd.set_defaults(run=revoke)
