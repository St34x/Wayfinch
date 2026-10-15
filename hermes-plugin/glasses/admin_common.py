# SPDX-License-Identifier: Apache-2.0
"""Shared pieces of the ``glasses-admin`` subcommands (``admin.py`` and every ``admin_*.py`` import them at the
top): the exit codes, the Hermes home lookup, reading and validating the glasses platform of a Hermes config file,
and paths safe to print."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import PLATFORM_NAME, DependencyMissing, GlassesConfig, check, may_hold_credentials
from .hermes_merge import LOCATIONS, PlatformView, platform_view

SECTION = ("gateway", "platforms", PLATFORM_NAME)
EXIT_OK, EXIT_FAILED, EXIT_UNREADABLE = 0, 1, 2


class ConfigFileError(Exception):
    """The Hermes config file cannot be read or has no glasses section (message names no value)."""


class ConfigHasErrors(Exception):
    """The glasses config does not validate, so no state dir or signer setting can be resolved."""


def missing_cryptography(exc: ImportError) -> bool:
    """True when *exc* is the failed import of ``cryptography`` itself or one of its modules."""
    return (exc.name or "").partition(".")[0] == "cryptography"


def hermes_home(args: argparse.Namespace, env: Mapping[str, str]) -> Path:
    """``--hermes-home``, else ``$HERMES_HOME``, else ``~/.hermes``."""
    return Path(args.hermes_home or env.get("HERMES_HOME") or Path.home() / ".hermes")


def glasses_config(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[GlassesConfig, Path]:
    """The validated glasses config of ``--config`` (else ``<home>/config.yaml``) and the Hermes home."""
    home = hermes_home(args, env)
    cfg = check(read_platform_section(args.config or home / "config.yaml").extra).config
    if cfg is None:
        raise ConfigHasErrors("the glasses config has errors - run glasses-admin config check")
    return cfg, home


def shown_path(path: Any) -> str:
    """*path* for a message, unless it contains ``@`` or ``://`` (it could carry credentials)."""
    return "<path not shown: it contains '@' or '://'>" if may_hold_credentials(str(path)) else str(path)


def read_platform_section(path: Path) -> PlatformView:
    """The glasses platform of a Hermes config file as the gateway merges it (``hermes_merge``);
    ``ConfigFileError`` otherwise.

    YAML errors are reported by position only: the offending line could hold a secret.
    """
    try:
        import yaml  # in the Hermes venv; only the CLI needs it
    except ImportError:
        raise DependencyMissing("PyYAML") from None

    where = shown_path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigFileError(f"{where}: cannot read ({type(exc).__name__})") from None
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as exc:  # a deeply nested file overflows the parser's stack
        mark = getattr(exc, "problem_mark", None)
        at = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ConfigFileError(f"{where}: not valid YAML{at}") from None
    view = platform_view(data)
    if view is None:
        raise ConfigFileError(
            f"{where}: no {'.'.join(SECTION)} mapping (nor any other glasses block: {', '.join(LOCATIONS[1:])})"
        )
    return view
