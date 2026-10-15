# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin``: operator CLI of the glasses plugin. This module holds ``config check`` (T1.54);
``crl`` (through the signer) and ``crl --check`` live in ``glasses.admin_crl``, ``revoke`` (signer, then the
plugin's admin socket) in ``glasses.admin_devices`` (shared CLI pieces in ``glasses.admin_common``); the plugin
holds no CA key; ``list`` comes later (T1.15f).

    python -m glasses.admin config check [--config FILE] [--hermes-home DIR] [--live]

Reads the glasses platform from FILE (default ``$HERMES_HOME/config.yaml``) merged the way the pinned
gateway merges it (``gateway.platforms.glasses``, ``platforms.glasses``, ``gateway.glasses``, a top-level
``glasses:`` block; platform-level keys promoted into ``extra`` -- ``glasses.hermes_merge``), runs the
validation the gateway runs (``glasses.config.check``) plus the allow-all gate on the process
environment, and prints one line per problem (``error: glasses.<path>: ...`` / ``warning: ...``)
and a summary. Exit status: 0 = no errors (warnings allowed), 1 = errors, 2 = cannot check (FILE
unreadable, a file too deeply nested to parse, or a required module such as ``jsonschema`` is missing: one line
naming it, no traceback). A platform whose ``enabled`` the gateway does not read as true (``yes``/``on``/``1``
count as true, as for the gateway; absent, ``false``/``no``/``off`` and unrecognised strings do not) gets a warning.
Printed values are names, numbers and paths from the deployer's own config, never secrets, and never a
value containing ``@`` or ``://`` (``glasses.config.may_hold_credentials``). The names Hermes returns are
filtered harder, because a config file can put anything there (``${VAR}`` expands to a ``.env`` value): a tool
name is shown only when it is in Hermes' toolset table (any other is only counted), a profile name only when it is
a plain identifier (``glasses.config_core.is_key_like``), a gate line only in its known shape, and every value of
the ``.env`` files read of four characters or more is masked (a shorter value would blank ordinary words; it is hidden
only where it is the whole routed profile name, and a timeout is never printed as a number). A process-environment
value, or a shorter ``.env`` value, inside a longer plain profile name can still be printed. A ``.env`` value
padded with white space inside quotes (``NAME=" value "``) is not matched: Hermes strips and lower-cases a route
profile name, the mask and the hiding compare the raw value, so such a value used as a whole route profile name is
printed in its stripped, lower-cased form. A
``--config``/``--hermes-home`` that is a URL (``://`` in the raw argument) is
refused before it is converted to a path (exit 2, one line, the value not shown).

``--live`` adds what needs this host and the Hermes install (``live_problems``): the state dir exists
with mode 0700, ``port``/``bridge_port`` are free on ``host``, ``public_url`` is set and its host
resolves, every lane is routed (``gateway.profile_routes``; no route = the default profile) to a
profile this gateway serves, and for each routed profile: an ``stt.provider`` (unset = a warning: Hermes
auto-detects one), ``stt.language`` not
``en`` while ``meeting.language_hints`` has a non-English tag, the resolved ``glasses`` tool set equal
to the ``telegram`` one, clarify timeout <= 0 (never expires) and ``approvals.timeout`` > 0 (expires,
fail closed); plus the allow-all gate in every profile scope (``glasses.gate``). The Hermes facts come
from ``glasses.hermes_facts``, which imports Hermes and must run with the Hermes venv's python; it starts
nothing and writes nothing into the Hermes home or a routed profile home (no skeleton, no ``SOUL.md``, no
chmod, no ``config.yaml.good`` backup: Hermes is imported under a throwaway home and the configs are read
without Hermes' loaders), so a snapshot of the home is identical before and after. Before it reads anything,
``--live`` refuses (exit 2, one line) when the effective user does not own the Hermes home. ``--live`` checks
the same Hermes home as the static check: ``HERMES_HOME`` is set to ``--hermes-home`` (else ``$HERMES_HOME``,
else ``~/.hermes``) in this process before the first Hermes import, and a ``--config`` that is not
``<home>/config.yaml`` is refused (exit 2). The env-dependent facts (``${VAR}`` in a config.yaml,
``HERMES_LOCAL_STT_LANGUAGE``) are evaluated against the environment the gateway sees: the process environment
overlaid with ``<home>/.env`` around the pure config reads only (a regular file of at most 1 MiB, read, never
rewritten; names that load code, such as ``PATH`` or the plugin switches, are not taken from it; a ``.env`` that
is not a regular file is an error line, the allow-all gate could not look; the process environment is restored
afterwards) and the ``allow_all_users`` grant of ``config.yaml`` (top level or under ``gateway:``) reaches the
allow-all gate the way Hermes' bridge delivers it to the gateway. Plugins installed under a profile's own home are
not discovered (tool parity is computed from the configuration). Hermes' own log output is silenced. Tests inject
fakes through ``Probes``.

T1.45 contract-test list (private Hermes APIs and copied Hermes behaviour, pinned a4648c5f58): see the
docstring of ``glasses.hermes_facts``.
"""

from __future__ import annotations

import argparse
import os
import socket
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

from . import admin_crl, admin_devices
from .admin_common import SECTION, ConfigFileError, hermes_home, read_platform_section, shown_path
from .admin_names import mask_lines, names_shown, shown_name, shown_problem
from .config import (
    PLATFORM_NAME,
    DependencyMissing,
    GlassesConfig,
    check,
    environ_reader,
    gate_violations,
    may_hold_credentials,
    resolve_state_dir,
)
from .hermes_env import hermes_home_env
from .hermes_facts import (  # noqa: F401 - re-exported: tests and callers use admin.HermesFacts etc.
    DEFAULT_PROFILE,
    HermesContractError,
    HermesFacts,
    ProfileFacts,
    gateway_enabled,
    hermes_facts,
    lanes_to_route,
    stt_language,
)
from .hermes_merge import PlatformView

EXIT_OK, EXIT_ERRORS, EXIT_UNREADABLE = 0, 1, 2
STATE_DIR_MODE = 0o700


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def extend(self, other: Report) -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


def static_report(view: PlatformView, env: Mapping[str, str]) -> tuple[Report, GlassesConfig | None]:
    """The gateway's validation of the merged platform *view* plus the allow-all gate of *env*."""
    result = check(view.extra)
    report = Report([f"{PLATFORM_NAME}.{e}" for e in result.errors], [f"{PLATFORM_NAME}.{w}" for w in result.warnings])
    if not gateway_enabled(view.enabled):
        report.warnings.append(f"{'.'.join(SECTION)}.enabled: not true — the gateway will not start the platform")
    report.errors.extend(f"environment: {p}" for p in gate_violations(environ_reader(env)))
    return report, result.config


# --------------------------------------------------------------------------------------------
# --live
# --------------------------------------------------------------------------------------------


def home_is_owned(home: Path) -> bool:
    """True when the effective user owns *home* (a home that does not exist yet has nothing to protect)."""
    try:
        return home.stat().st_uid == os.geteuid()
    except FileNotFoundError:
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class Probes:
    port_free: Callable[[str, int], bool]
    resolves: Callable[[str], bool]
    hermes: Callable[[GlassesConfig], HermesFacts]
    owns_home: Callable[[Path], bool] = home_is_owned


def port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # ignore TIME_WAIT, not listeners
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def resolves(hostname: str) -> bool:
    try:
        return bool(socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM))
    except (OSError, UnicodeError):
        return False


def _state_dir_problems(path: Path, report: Report) -> None:
    shown = shown_path(path)
    where = f"{PLATFORM_NAME}.state_dir: {shown}"
    try:
        st = path.stat()
    except FileNotFoundError:
        report.warnings.append(f"{where} does not exist yet — create it with mkdir -m 700 {shown}")
        return
    except OSError as exc:
        report.errors.append(f"{where} cannot be read ({type(exc).__name__})")
        return
    if not stat.S_ISDIR(st.st_mode):
        report.errors.append(f"{where} is not a directory")
    elif stat.S_IMODE(st.st_mode) != STATE_DIR_MODE:
        report.errors.append(
            f"{where} has mode {stat.S_IMODE(st.st_mode):04o} — it holds device secrets and "
            f"must be 0700 (chmod 700 {shown})"
        )


def _host_problems(cfg: GlassesConfig, probes: Probes, report: Report) -> None:
    for key, port in (("port", cfg.port), ("bridge_port", cfg.bridge_port)):
        if not probes.port_free(cfg.host, port):
            report.errors.append(
                f"{PLATFORM_NAME}.{key}: {cfg.host}:{port} is in use — stop what holds it or "
                f"pick another port (a running glasses gateway holds it too)"
            )
    if not cfg.public_url:
        report.errors.append(
            f"{PLATFORM_NAME}.public_url: not set — the app needs the wss:// URL of your TLS "
            f"edge (it goes into the enrolment QR)"
        )
        return
    host = urlsplit(cfg.public_url).hostname or ""
    if not probes.resolves(host):
        report.errors.append(
            f"{PLATFORM_NAME}.public_url: host {host} does not resolve — check the DNS name of your TLS edge"
        )


def _routed_profiles(cfg: GlassesConfig, facts: HermesFacts, report: Report) -> list[str]:
    routed: list[str] = []
    for lane in lanes_to_route(cfg):
        profile = facts.routes.get(lane, DEFAULT_PROFILE)
        if profile not in facts.served or profile not in facts.profiles:
            report.errors.append(
                f"{PLATFORM_NAME}.lanes: lane {lane!r} is routed to profile {shown_name(profile, quote=True)}, which "
                f"this gateway does not serve — fix gateway.profile_routes or serve that profile "
                f"(the gateway would drop the lane's messages)"
            )
        elif profile not in routed:
            routed.append(profile)
    return routed


def profile_problems(
    name: str, facts: ProfileFacts, language_hints: Sequence[str], known: frozenset[str] = frozenset()
) -> Report:
    """Problems of one routed profile (names and numbers only)."""
    where = f"profile {shown_name(name)}"
    report = Report()
    out = report.errors
    if not facts.stt_provider:  # unset = Hermes auto-detects a provider; voice may still work
        report.warnings.append(
            f"{where}: stt.provider is not set — Hermes picks the first available "
            f"speech-to-text provider; set it (e.g. stt.provider: local) to be sure which one "
            f"transcribes voice commands from the glasses"
        )
    foreign = [tag for tag in language_hints if tag.split("-")[0].lower() != "en"]
    if foreign and facts.stt_language.strip().lower() == "en":
        out.append(
            f'{where}: stt.language is "en" but meeting.language_hints has {", ".join(foreign)} — set '
            f'stt.language: "" (auto-detect); "en" translates other languages into English'
        )
    if facts.glasses_tools != facts.telegram_tools:
        out.append(
            f"{where}: the glasses tool set differs from telegram (missing on glasses: "
            f"{names_shown(facts.telegram_tools - facts.glasses_tools, known)}; only on glasses: "
            f"{names_shown(facts.glasses_tools - facts.telegram_tools, known)}) — set platform_toolsets.glasses to "
            f"the profile's telegram list"
        )
    if facts.clarify_timeout > 0:
        out.append(
            f"{where}: clarify timeout resolves to a positive number of seconds — set agent.clarify_timeout: 0 "
            f"(clarifying questions on the glasses never expire)"
        )
    if facts.approvals_timeout <= 0:
        shown = "0" if facts.approvals_timeout == 0 else "a negative number"  # a ${VAR} value is never echoed
        out.append(
            f"{where}: approvals.timeout resolves to {shown} — set a positive number of "
            f"seconds (approvals expire and fail closed)"
        )
    return report


def _hermes_error(exc: Exception) -> str:
    if isinstance(exc, HermesContractError):
        return f"hermes: unexpected Hermes behaviour — {exc}; this glasses check needs an update (T1.45)"
    name = getattr(exc, "name", None) if isinstance(exc, (ImportError, AttributeError)) else None
    what = (
        f"{type(exc).__name__}: {name}"
        if isinstance(name, str) and not may_hold_credentials(name)
        else type(exc).__name__
    )
    return f"hermes: cannot read the Hermes configuration ({what}) — run with the Hermes venv's python"


def live_problems(cfg: GlassesConfig, hermes_home: Path, probes: Probes) -> Report:
    report = Report()
    _state_dir_problems(resolve_state_dir(cfg, hermes_home), report)
    _host_problems(cfg, probes, report)
    try:
        facts = probes.hermes(cfg)
    except Exception as exc:  # noqa: BLE001 - outermost CLI boundary: Hermes missing or its config unreadable
        report.errors.append(_hermes_error(exc))
        return report
    # The process environment is already checked by static_report.
    report.errors.extend(
        f"allow-all gate: {shown_problem(p)}" for p in facts.gate_problems if not p.startswith("process environment:")
    )
    for name in _routed_profiles(cfg, facts, report):
        report.extend(profile_problems(name, facts.profiles[name], cfg.language_hints, facts.known_toolsets))
    return Report(mask_lines(report.errors, facts.env_values), mask_lines(report.warnings, facts.env_values))


DEFAULT_PROBES = Probes(port_free=port_free, resolves=resolves, hermes=hermes_facts)


def print_report(report: Report, out: TextIO) -> int:
    for line in report.errors:
        print(f"error: {line}", file=out)
    for line in report.warnings:
        print(f"warning: {line}", file=out)
    counts = f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)"
    print(f"config check: {'FAILED' if report.errors else 'OK'} ({counts})", file=out)
    return EXIT_ERRORS if report.errors else EXIT_OK


def _live_home_mismatch(args: argparse.Namespace, home: Path, probes: Probes) -> str | None:
    """Why --live cannot check *home*: another user owns it, or Hermes reads ``<home>/config.yaml``, never
    another --config file."""
    if not probes.owns_home(home):
        return (
            f"--live: the Hermes home {shown_path(home)} is owned by another user — run glasses-admin as "
            f"the user that owns it (nothing was read)"
        )
    if args.config is None or Path(args.config).resolve() == (home / "config.yaml").resolve():
        return None
    return (
        f"--live reads the Hermes install of the Hermes home {shown_path(home)}, but --config "
        f"{shown_path(args.config)} is not its config.yaml — pass --hermes-home <the directory holding that "
        f"config.yaml> instead of --config"
    )


def _local_paths(args: argparse.Namespace) -> argparse.Namespace:
    """*args* with ``--config`` and ``--hermes-home`` as ``Path``s. ``Path`` collapses ``//``, so a URL is
    recognised on the raw argv string, before the conversion (``ConfigFileError``; the value is not shown)."""
    for flag, value in (("--config", args.config), ("--hermes-home", args.hermes_home)):
        if value is not None and "://" in str(value):
            raise ConfigFileError(f"{flag}: not a local path (it contains '://'; the value is not shown)")
    paths = {key: None if getattr(args, key) is None else Path(getattr(args, key)) for key in ("config", "hermes_home")}
    return argparse.Namespace(**{**vars(args), **paths})


def config_check(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, probes: Probes) -> int:
    try:
        args = _local_paths(args)
    except ConfigFileError as exc:
        print(f"error: {exc}", file=out)
        return EXIT_UNREADABLE
    home = hermes_home(args, env)
    path = args.config or home / "config.yaml"
    mismatch = _live_home_mismatch(args, home, probes) if args.live else None
    try:
        if mismatch:
            raise ConfigFileError(mismatch)
        section = read_platform_section(path)
        report, cfg = static_report(section, env)
    except (ConfigFileError, DependencyMissing) as exc:
        print(f"error: {exc}", file=out)
        return EXIT_UNREADABLE
    if args.live and cfg is None:
        report.warnings.append("live checks skipped: fix the errors above first")
    elif args.live and cfg is not None:
        with hermes_home_env(home):  # before the first Hermes import: Hermes resolves its home from it
            report.extend(live_problems(cfg, home, probes))
    return print_report(report, out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="glasses-admin", description="Admin CLI of the glasses plugin.")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="configuration commands")
    actions = config.add_subparsers(dest="action", required=True)
    chk = actions.add_parser("check", help="validate gateway.platforms.glasses of a Hermes config")
    chk.add_argument("--config", help="Hermes config.yaml (default: $HERMES_HOME/config.yaml)")
    chk.add_argument("--hermes-home", help="Hermes home (default: $HERMES_HOME, else ~/.hermes)")
    chk.add_argument(
        "--live",
        action="store_true",
        help=(
            "also check this host and the routed Hermes profiles (read-only; plugins installed under a "
            "profile's own home are not discovered, tool parity is computed from the configuration only)"
        ),
    )
    admin_crl.register(commands)
    admin_devices.register(commands)
    return parser


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    probes: Probes | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "config":  # subcommands of the admin_* modules carry their handler
        return int(args.run(args, os.environ if env is None else env, out or sys.stdout))
    return config_check(args, os.environ if env is None else env, out or sys.stdout, probes or DEFAULT_PROBES)


if __name__ == "__main__":
    sys.exit(main())
