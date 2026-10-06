# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin``: operator CLI of the glasses plugin. This module holds ``config check`` (T1.54);
the CA, enrolment and revoke subcommands come later (T1.15).

    python -m glasses.admin config check [--config FILE] [--hermes-home DIR] [--live]

Reads the glasses platform from FILE (default ``$HERMES_HOME/config.yaml``) merged the way the pinned
gateway merges it (``gateway.platforms.glasses``, ``platforms.glasses``, ``gateway.glasses``, a top-level
``glasses:`` block; platform-level keys promoted into ``extra`` -- ``glasses.hermes_merge``), runs the
validation the gateway runs (``glasses.config.check``) plus the allow-all gate on the process
environment, and prints one line per problem (``error: glasses.<path>: ...`` / ``warning: ...``)
and a summary. Exit status: 0 = no errors (warnings allowed), 1 = errors, 2 = cannot check (FILE
unreadable, or a required module such as ``jsonschema`` is missing: one line naming it, no traceback).
Printed values are names, numbers and paths from the deployer's own config, never secrets, and never a
value containing ``@`` or ``://`` (``glasses.config.may_hold_credentials``).

``--live`` adds what needs this host and the Hermes install (``live_problems``): the state dir exists
with mode 0700, ``port``/``bridge_port`` are free on ``host``, ``public_url`` is set and its host
resolves, every lane is routed (``gateway.profile_routes``; no route = the default profile) to a
profile this gateway serves, and for each routed profile: an ``stt.provider`` (unset = a warning: Hermes
auto-detects one), ``stt.language`` not
``en`` while ``meeting.language_hints`` has a non-English tag, the resolved ``glasses`` tool set equal
to the ``telegram`` one, clarify timeout <= 0 (never expires) and ``approvals.timeout`` > 0 (expires,
fail closed); plus the allow-all gate in every profile scope (``glasses.gate``). The Hermes facts come
from ``glasses.hermes_facts``, which imports Hermes and must run with the Hermes venv's python; it starts
nothing, but it is NOT read-only today: Hermes' config load creates the home skeleton (``hooks/``, ``logs/``,
``cron/``, ``sessions/``, ``backups/``, ``SOUL.md``, ...) in the home it checks -- point it at a copy or accept
that (known gap, strict xfail ``test_real_live_does_not_write_into_the_hermes_home``). ``--live`` checks the
same Hermes home as the static check: ``HERMES_HOME`` is set to ``--hermes-home`` (else ``$HERMES_HOME``, else
``~/.hermes``) in this process before the first Hermes import, and a ``--config`` that is not
``<home>/config.yaml`` is refused (exit 2). Tests inject fakes through ``Probes``.

T1.45 contract-test list -- private Hermes APIs and copied Hermes behaviour (pinned a4648c5f58), each to
be pinned by a contract test so a Hermes bump fails loudly instead of drifting:

* ``hermes_cli.tools_config._get_platform_tools(config, platform) -> set[str]`` (private; design D5a).
* ``tools.transcription_tools._resolve_stt_language`` -- copied in ``hermes_facts.stt_language``:
  ``stt.<provider>.language`` (+ ``language_code`` for elevenlabs, tools/transcription_cloud.py:336) >
  ``stt.language`` > env ``tools.transcription_common.LOCAL_STT_LANGUAGE_ENV`` (imported, not copied).
* ``tools.approval_context._get_approval_timeout`` -- copied in ``hermes_facts.approvals_timeout``:
  ``int(approvals.timeout)``, 300 when missing or not a number (its upper clamp does not affect ``<= 0``).
* ``gateway.platforms._shared`` -- imported for its side effect: ``glasses.gate`` then reads every profile.
* ``glasses.hermes_merge.platform_view`` -- copy of ``gateway.config_loader.merge_platform_sections``,
  ``PlatformConfig.from_dict`` (private ``PlatformConfig._TYPED_KEYS``) and the root-block bridge
  (``_bridged_keys``); differential test in tests/test_config_check_merge.py.
* Shapes asserted at run time (``HermesContractError``, a clear error, never a silent default):
  ``load_config_readonly()`` returns a mapping, its ``stt``/``approvals`` are mappings or null,
  ``_get_platform_tools`` returns a set of names.

Public Hermes APIs used instead of copies: ``hermes_cli.config.load_config_readonly`` (defaults, canonical
keys, env expansion, managed overlay; like any Hermes read it may initialise a missing profile skeleton
and keep a last-known-good backup of config.yaml), ``hermes_constants.set/reset_hermes_home_override``,
``tools.clarify_gateway.resolve_clarify_timeout``, ``gateway.config.load_gateway_config``,
``gateway.profile_routing.match_profile_route``, ``hermes_cli.profiles.profiles_to_serve``.
"""

from __future__ import annotations

import argparse
import os
import socket
import stat
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

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
from .hermes_facts import (  # noqa: F401 - re-exported: tests and callers use admin.HermesFacts etc.
    DEFAULT_PROFILE,
    HermesContractError,
    HermesFacts,
    ProfileFacts,
    hermes_facts,
    hermes_home_env,
    lanes_to_route,
    stt_language,
)
from .hermes_merge import LOCATIONS, PlatformView, platform_view

EXIT_OK, EXIT_ERRORS, EXIT_UNREADABLE = 0, 1, 2
SECTION = ("gateway", "platforms", PLATFORM_NAME)
STATE_DIR_MODE = 0o700
MAX_LISTED_TOOLS = 10


class ConfigFileError(Exception):
    """The Hermes config file cannot be read or has no glasses section (message names no value)."""


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def extend(self, other: Report) -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


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
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        at = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise ConfigFileError(f"{where}: not valid YAML{at}") from None
    view = platform_view(data)
    if view is None:
        raise ConfigFileError(
            f"{where}: no {'.'.join(SECTION)} mapping (nor any other glasses block: {', '.join(LOCATIONS[1:])})"
        )
    return view


def static_report(view: PlatformView, env: Mapping[str, str]) -> tuple[Report, GlassesConfig | None]:
    """The gateway's validation of the merged platform *view* plus the allow-all gate of *env*."""
    result = check(view.extra)
    report = Report([f"{PLATFORM_NAME}.{e}" for e in result.errors], [f"{PLATFORM_NAME}.{w}" for w in result.warnings])
    if view.enabled is not True:
        report.warnings.append(f"{'.'.join(SECTION)}.enabled: not true — the gateway will not start the platform")
    report.errors.extend(f"environment: {p}" for p in gate_violations(environ_reader(env)))
    return report, result.config


# --------------------------------------------------------------------------------------------
# --live
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Probes:
    port_free: Callable[[str, int], bool]
    resolves: Callable[[str], bool]
    hermes: Callable[[GlassesConfig], HermesFacts]


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
                f"{PLATFORM_NAME}.lanes: lane {lane!r} is routed to profile {profile!r}, which "
                f"this gateway does not serve — fix gateway.profile_routes or serve that profile "
                f"(the gateway would drop the lane's messages)"
            )
        elif profile not in routed:
            routed.append(profile)
    return routed


def _names(names: Iterable[str]) -> str:
    listed = sorted(names)
    more = f" (+{len(listed) - MAX_LISTED_TOOLS} more)" if len(listed) > MAX_LISTED_TOOLS else ""
    return (", ".join(listed[:MAX_LISTED_TOOLS]) or "none") + more


def profile_problems(name: str, facts: ProfileFacts, language_hints: Sequence[str]) -> Report:
    """Problems of one routed profile (names and numbers only)."""
    where = f"profile {name}"
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
            f"{_names(facts.telegram_tools - facts.glasses_tools)}; only on glasses: "
            f"{_names(facts.glasses_tools - facts.telegram_tools)}) — set platform_toolsets.glasses to "
            f"the profile's telegram list"
        )
    if facts.clarify_timeout > 0:
        out.append(
            f"{where}: clarify timeout resolves to {facts.clarify_timeout} s — set agent.clarify_timeout: 0 "
            f"(clarifying questions on the glasses never expire)"
        )
    if facts.approvals_timeout <= 0:
        out.append(
            f"{where}: approvals.timeout resolves to {facts.approvals_timeout} — set a positive number of "
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
    except Exception as exc:  # outermost CLI boundary: Hermes missing or its config unreadable
        report.errors.append(_hermes_error(exc))
        return report
    # The process environment is already checked by static_report.
    report.errors.extend(
        f"allow-all gate: {p}" for p in facts.gate_problems if not p.startswith("process environment:")
    )
    for name in _routed_profiles(cfg, facts, report):
        report.extend(profile_problems(name, facts.profiles[name], cfg.language_hints))
    return report


DEFAULT_PROBES = Probes(port_free=port_free, resolves=resolves, hermes=hermes_facts)


def print_report(report: Report, out: TextIO) -> int:
    for line in report.errors:
        print(f"error: {line}", file=out)
    for line in report.warnings:
        print(f"warning: {line}", file=out)
    counts = f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)"
    print(f"config check: {'FAILED' if report.errors else 'OK'} ({counts})", file=out)
    return EXIT_ERRORS if report.errors else EXIT_OK


def _live_home_mismatch(args: argparse.Namespace, home: Path) -> str | None:
    """Why --live cannot check *home*: Hermes reads ``<home>/config.yaml``, never another --config file."""
    if args.config is None or Path(args.config).resolve() == (home / "config.yaml").resolve():
        return None
    return (
        f"--live reads the Hermes install of the Hermes home {shown_path(home)}, but --config "
        f"{shown_path(args.config)} is not its config.yaml — pass --hermes-home <the directory holding that "
        f"config.yaml> instead of --config"
    )


def config_check(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, probes: Probes) -> int:
    home = Path(args.hermes_home or env.get("HERMES_HOME") or Path.home() / ".hermes")
    path = args.config or home / "config.yaml"
    mismatch = _live_home_mismatch(args, home) if args.live else None
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
    chk.add_argument("--config", type=Path, help="Hermes config.yaml (default: $HERMES_HOME/config.yaml)")
    chk.add_argument("--hermes-home", type=Path, help="Hermes home (default: $HERMES_HOME, else ~/.hermes)")
    chk.add_argument("--live", action="store_true", help="also check this host and the routed Hermes profiles")
    return parser


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    probes: Probes | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    return config_check(args, os.environ if env is None else env, out or sys.stdout, probes or DEFAULT_PROBES)


if __name__ == "__main__":
    sys.exit(main())
