# SPDX-License-Identifier: Apache-2.0
"""T1.54: ``glasses-admin config check --live`` with fakes (no Hermes import, conftest.py), plus one run
against real Hermes code in a scratch HERMES_HOME (subprocess, read-only Hermes venv; no gateway)."""

from __future__ import annotations

import io
import os
import socket
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from glasses import admin
from glasses import config as gcfg

from conftest import PLUGIN_ROOT

WORKTREE = PLUGIN_ROOT.parent
GOOD = admin.ProfileFacts(
    stt_provider="local",
    stt_language="",
    glasses_tools=frozenset({"web", "todo"}),
    telegram_tools=frozenset({"web", "todo"}),
    clarify_timeout=0,
    approvals_timeout=300,
)


def cfg(**extra):
    base = {"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant", "work"]}
    return gcfg.load({**base, **extra})


def facts(routes=None, served=("default", "work"), gate=(), **profiles):
    profiles = {"default": GOOD, "work": GOOD, **profiles}
    return admin.HermesFacts(
        served=frozenset(served), routes=routes or {"work": "work"}, profiles=profiles, gate_problems=tuple(gate)
    )


def probes(hermes=None, busy=(), resolvable=True):
    def load(_cfg):
        if isinstance(hermes, Exception):
            raise hermes
        return hermes or facts()

    return admin.Probes(port_free=lambda _h, port: port not in busy, resolves=lambda _host: resolvable, hermes=load)


@pytest.fixture
def home(tmp_path):
    (tmp_path / "glasses").mkdir(mode=0o700)
    (tmp_path / "glasses").chmod(0o700)
    return tmp_path


def live(config, home, p):
    return admin.live_problems(config, home, p)


def test_all_good(home):
    report = live(cfg(), home, probes())
    assert (report.errors, report.warnings) == ([], [])


def test_state_dir_mode(home):
    (home / "glasses").chmod(0o755)
    (msg,) = live(cfg(), home, probes()).errors
    assert msg.startswith("glasses.state_dir: ") and "mode 0755" in msg and "chmod 700" in msg


def test_state_dir_missing_is_a_warning_and_a_file_an_error(tmp_path):
    report = live(cfg(), tmp_path, probes())
    assert report.errors == [] and report.warnings[0].startswith("glasses.state_dir: ")
    (tmp_path / "glasses").write_text("x")
    assert live(cfg(), tmp_path, probes()).errors[0].endswith("is not a directory")


def test_explicit_state_dir_is_checked(home, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o750)
    state.chmod(0o750)
    (msg,) = live(cfg(state_dir=str(state)), home, probes()).errors
    assert str(state) in msg and "0750" in msg


def test_ports_in_use(home):
    errors = live(cfg(), home, probes(busy=(8765, 8766))).errors
    assert [e.split(":", 1)[0] for e in errors] == ["glasses.port", "glasses.bridge_port"]
    assert "127.0.0.1:8765 is in use" in errors[0]


def test_real_port_probe():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert admin.port_free("127.0.0.1", port) is False
    assert admin.port_free("127.0.0.1", port) is True


def test_real_resolver_on_localhost():
    assert admin.resolves("localhost") is True


def test_public_url_unset_or_unresolvable(home):
    (msg,) = live(gcfg.load({"lanes": ["assistant", "work"]}), home, probes()).errors
    assert msg.startswith("glasses.public_url: not set")
    (msg,) = live(cfg(), home, probes(resolvable=False)).errors
    assert msg == (
        "glasses.public_url: host glasses.example.com does not resolve — check the DNS name of your TLS edge"
    )


def test_lane_routed_to_an_unserved_profile(home):
    (msg,) = live(cfg(), home, probes(facts(routes={"work": "ghost"}))).errors
    assert msg.startswith("glasses.lanes: lane 'work' is routed to profile 'ghost', which this gateway does not serve")


def test_unrouted_lane_goes_to_the_default_profile(home):
    config = gcfg.load({"public_url": "wss://glasses.example.com/ws", "lanes": [], "home_lane": "solo"})
    assert live(config, home, probes(facts(routes={}, served=("default",)))).errors == []
    (msg,) = live(config, home, probes(facts(routes={}, served=("work",)))).errors
    assert msg.startswith("glasses.lanes: lane 'solo' is routed to profile 'default', which this gateway")


@pytest.mark.parametrize(
    "change,needle",
    [
        ({"stt_language": "en"}, 'stt.language is "en"'),
        ({"glasses_tools": frozenset({"web", "terminal"})}, "missing on glasses: todo; only on glasses: terminal"),
        ({"clarify_timeout": 3600}, "clarify timeout resolves to 3600"),
        ({"approvals_timeout": 0}, "approvals.timeout resolves to 0"),
    ],
)
def test_routed_profile_problems(home, change, needle):
    config = cfg(meeting={"language_hints": ["en", "fr"]})
    (msg,) = live(config, home, probes(facts(work=replace(GOOD, **change)))).errors
    assert msg.startswith("profile work: ") and needle in msg


def test_english_only_hints_allow_stt_language_en(home):
    config = cfg(meeting={"language_hints": ["en", "en-GB"]})
    assert live(config, home, probes(facts(work=replace(GOOD, stt_language="en")))).errors == []


@pytest.mark.parametrize(
    "stt,provider,expected",
    [
        ({"language": "en", "local": {"language": ""}}, "local", "en"),  # empty per-provider falls through
        ({"language": "en", "local": {"language": "fr"}}, "local", "fr"),  # per-provider wins
        ({"language": "", "local": {"language": " "}}, "local", ""),  # auto-detect
        ({"language": "en"}, "", "en"),
        ({"language": None, "local": "x"}, "local", ""),
    ],
)
def test_stt_language_resolution_mirrors_hermes(stt, provider, expected):
    assert admin.stt_language(stt, provider) == expected


def test_negative_clarify_timeout_is_unlimited_too(home):
    assert live(cfg(), home, probes(facts(work=replace(GOOD, clarify_timeout=-1)))).errors == []


def test_unrouted_profiles_are_not_checked(home):
    assert (
        live(cfg(lanes=["work"], home_lane="work"), home, probes(facts(default=replace(GOOD, stt_provider="")))).errors
        == []
    )


def test_gate_problems_and_unreadable_hermes(home):
    gate = ["process environment: dup", "profile work: GLASSES_ALLOW_ALL_USERS is set; it must never be set"]
    assert live(cfg(), home, probes(facts(gate=gate))).errors == [f"allow-all gate: {gate[1]}"]
    (msg,) = live(cfg(), home, probes(RuntimeError("boom"))).errors
    assert msg.startswith("hermes: cannot read the Hermes configuration (RuntimeError)") and "boom" not in msg


def test_cli_live_wiring(home):
    path = home / "config.yaml"
    path.write_text(
        yaml.safe_dump({
            "gateway": {
                "platforms": {
                    "glasses": {
                        "enabled": True,
                        "extra": {"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant", "work"]},
                    }
                }
            }
        })
    )
    out = io.StringIO()
    code = admin.main(["config", "check", "--live", "--hermes-home", str(home)], {}, out, probes(busy=(8765,)))
    assert code == admin.EXIT_ERRORS and "error: glasses.port: 127.0.0.1:8765 is in use" in out.getvalue()
    path.write_text(path.read_text().replace("- work", "- Work"))
    out = io.StringIO()
    assert admin.main(["config", "check", "--live", "--hermes-home", str(home)], {}, out, probes()) == 1
    assert "warning: live checks skipped: fix the errors above first" in out.getvalue()


# --------------------------------------------------------------------------------------------
# real Hermes code, scratch HERMES_HOME under <worktree>/.local/hh-t154 (never .local/hh, no gateway)
# --------------------------------------------------------------------------------------------


def hermes_python():
    venv = os.environ.get("TEST_HERMES_VENV")  # same variable as tools/test-hermes/setup_home.sh
    if venv:
        candidate = Path(venv) / "bin" / "python"
        if candidate.exists():
            return candidate
        pytest.skip(f"TEST_HERMES_VENV has no bin/python: {venv}")
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=WORKTREE,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    for root in (WORKTREE, Path(common).parent if common else WORKTREE):
        candidate = root / ".local" / "venv314" / "bin" / "python"
        if candidate.exists():
            return candidate
    pytest.skip("Hermes venv .local/venv314 not found; the live checks stay covered by the fakes above")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_yaml(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def scratch_home(root: Path) -> Path:
    home = root / "home"
    (root / "state").mkdir(mode=0o700)
    extra = {
        "port": free_port(),
        "bridge_port": free_port(),
        "public_url": "wss://localhost/ws",
        "state_dir": str(root / "state"),
        "lanes": ["assistant", "work", "ghost"],
        "meeting": {"language_hints": ["fr"]},
    }
    write_yaml(
        home / "config.yaml",
        {
            "gateway": {
                "multiplex_profiles": True,
                "platforms": {"glasses": {"enabled": True, "extra": extra}},
                "profile_routes": [
                    {"platform": "glasses", "chat_id": "glasses:work", "profile": "work"},
                    {"platform": "glasses", "chat_id": "glasses:ghost", "profile": "ghost"},
                ],
            },
            "stt": {"provider": "local", "language": ""},
            "agent": {"clarify_timeout": 0},
            "approvals": {"timeout": 60},
            "platform_toolsets": {"glasses": ["web"], "telegram": ["web"]},
        },
    )
    write_yaml(
        home / "profiles" / "work" / "config.yaml",
        {
            "stt": {"provider": "local", "language": "en"},
            "agent": {"clarify_timeout": 3600},
            "approvals": {"timeout": "${T154_APPROVALS_TIMEOUT}"},  # canary: only Hermes' env expansion makes it 0
            "platform_toolsets": {"glasses": ["web", "todo"], "telegram": ["web"]},
        },
    )
    (home / "profiles" / "work" / ".env").write_text("GLASSES_ALLOW_ALL_USERS=1\n", encoding="utf-8")
    return home


@pytest.mark.parametrize("mode", ["env", "flag"])
def test_live_against_real_hermes_in_a_scratch_home(mode):
    python = hermes_python()
    base = WORKTREE / ".local" / "hh-t154"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        home = scratch_home(Path(tmp))
        user_home = Path(tmp) / "user"  # HOME: a wrong-home read or write would land in user/.hermes
        user_home.mkdir()
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(user_home),
            "PYTHONPATH": str(PLUGIN_ROOT),
            "T154_APPROVALS_TIMEOUT": "0",
        }
        argv = ["config", "check", "--live"]
        if mode == "env":
            env["HERMES_HOME"] = str(home)
        else:
            argv += ["--hermes-home", str(home)]
        proc = subprocess.run(
            [str(python), "-m", "glasses.admin", *argv], cwd=tmp, env=env, capture_output=True, text=True, timeout=240
        )
        assert not (user_home / ".hermes").exists(), sorted(p.name for p in user_home.iterdir())
    errors = sorted(line for line in proc.stdout.splitlines() if line.startswith("error: "))
    assert proc.returncode == admin.EXIT_ERRORS, proc.stdout + proc.stderr[-2000:]
    expected = [
        "error: allow-all gate: profile work: GLASSES_ALLOW_ALL_USERS is set",
        "error: glasses.lanes: lane 'ghost' is routed to profile 'ghost', which this gateway does not serve",
        "error: profile work: approvals.timeout resolves to 0",
        "error: profile work: clarify timeout resolves to 3600 s",
        'error: profile work: stt.language is "en" but meeting.language_hints has fr',
        "error: profile work: the glasses tool set differs from telegram",
    ]
    assert len(errors) == len(expected), proc.stdout
    for line, prefix in zip(errors, expected):
        assert line.startswith(prefix), (line, proc.stdout)
    assert os.path.isdir(base)
