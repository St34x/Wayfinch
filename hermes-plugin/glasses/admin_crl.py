# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin crl``: the device CRL, which the signer signs and publishes (the plugin holds no CA key).

    python -m glasses.admin crl [--check] [--config FILE] [--hermes-home DIR]

``crl`` asks the signer (``signer_client``, op ``crl_generate``, one 10 s deadline, no retry) to generate and publish
the CRLs and prints one line per CA: ``crl: CA <ca_id>: CRL number <n>, next update <YYYY-MM-DD>``. A refusal prints
``error: crl: the signer refused (<code>)``, any other failed signer call its fixed message; neither prints a
traceback, a path or answer text. The root-side installer (planned, built outside this repository) will put the
published CRL on the edge.
``crl --check`` checks the CA and CRL bundles the signer publishes in ``signer.public_dir`` (``crl_check``; the
rollback record is ``$state_dir/crl-seen.json``) and prints their errors and warnings, a summary line (``FAILED`` /
``WARN`` / ``OK``) and one fixed note that the edge copy is not read. Exit status: 0 = CRL published / check clean,
1 = the signer call failed or was refused / the check found errors, 3 = the check found warnings only (so a scheduler
that watches the exit status sees the 14-day and 180-day warnings), 2 = the config file is unreadable, a ``signer.*``
key the command needs is not set or a module is missing. Only ``crl --check`` imports ``crl_check`` (and so
``cryptography``), inside the command.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO, cast

from . import signer_client
from . import signer_protocol as proto
from .admin_common import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_UNREADABLE,
    ConfigFileError,
    ConfigHasErrors,
    glasses_config,
    missing_cryptography,
)
from .config import DependencyMissing, GlassesConfig, resolve_state_dir

EXIT_WARN = 3  # crl --check: warnings, no errors
CHECK_NOTE = "note: the signer's published CRL was checked; the edge copy is not read here"


def _generate(cfg: GlassesConfig, out: TextIO) -> int:
    try:  # through the module attribute: tests patch it to a client on the fake signer
        result = signer_client.client_from_config(cfg).crl_generate()
    except signer_client.NotConfigured as exc:
        print(f"error: {exc}", file=out)
        return EXIT_UNREADABLE
    except proto.SignerRefusal as exc:
        print(f"error: crl: the signer refused ({exc.code})", file=out)
        return EXIT_FAILED
    except proto.SignerError as exc:
        print(f"error: crl: {exc.message}", file=out)
        return EXIT_FAILED
    for entry in cast(list[dict[str, object]], result["crl"]):  # checked by proto.decode_answer
        when = str(entry["next_update"])[:10]
        print(f"crl: CA {entry['ca_id']}: CRL number {entry['number']}, next update {when}", file=out)
    return EXIT_OK


def _check(crl_check: Any, cfg: GlassesConfig, state_dir: Path, out: TextIO) -> int:
    try:  # through the module attribute: the test guard checks every published path there
        ca_path = signer_client.public_file(cfg, proto.PUBLIC_CA_BUNDLE)
        crl_path = signer_client.public_file(cfg, proto.PUBLIC_CRL)
    except signer_client.NotConfigured as exc:
        print(f"error: {exc}", file=out)
        return EXIT_UNREADABLE
    result = crl_check.check_published(ca_path, crl_path, state_dir)
    for line in result.errors:
        print(f"error: crl --check: {line}", file=out)
    for line in result.warnings:
        print(f"warning: crl --check: {line}", file=out)
    counts = f"{len(result.errors)} error(s), {len(result.warnings)} warning(s)"
    if result.errors:
        word, code = "FAILED", EXIT_FAILED
    elif result.warnings:
        word, code = "WARN", EXIT_WARN
    else:
        word, code = "OK", EXIT_OK
    print(f"crl check: {word} ({counts})", file=out)
    print(CHECK_NOTE, file=out)
    return code


def _crl_check_module(out: TextIO) -> Any | None:
    try:  # lazy: only crl --check needs cryptography, config check and crl must run without it
        from . import crl_check

        return crl_check
    except ImportError as exc:
        if not missing_cryptography(exc):
            raise
    print(f"error: {DependencyMissing('cryptography')}", file=out)
    return None


def crl(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    module = _crl_check_module(out) if args.check else None
    if args.check and module is None:
        return EXIT_UNREADABLE
    try:
        cfg, home = glasses_config(args, env)
    except (ConfigFileError, DependencyMissing) as exc:
        print(f"error: {exc}", file=out)
        return EXIT_UNREADABLE
    except ConfigHasErrors as exc:
        print(f"error: crl: {exc}", file=out)
        return EXIT_FAILED
    if module is None:
        return _generate(cfg, out)
    return _check(module, cfg, resolve_state_dir(cfg, home), out)


def register(commands: Any) -> None:
    """Add ``crl`` to the ``glasses-admin`` subcommands (*commands* from ``add_subparsers``)."""
    cmd = commands.add_parser("crl", help="ask the signer to generate and publish the device CRL")
    cmd.add_argument("--check", action="store_true", help="check the CA and CRL bundles the signer publishes")
    cmd.add_argument("--config", type=Path, help="Hermes config.yaml (default: $HERMES_HOME/config.yaml)")
    cmd.add_argument("--hermes-home", type=Path, help="Hermes home (default: $HERMES_HOME, else ~/.hermes)")
    cmd.set_defaults(run=crl)
