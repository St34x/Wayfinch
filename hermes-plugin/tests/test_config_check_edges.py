# SPDX-License-Identifier: Apache-2.0
"""Hostile inputs for ``config check``: secrets, missing modules, Hermes facts, block locations, the home.

Credential-looking key names and --live names (values: test_config_check_credentials.py); missing ``jsonschema`` /
``PyYAML`` on the CLI and the gateway side; unset ``stt.provider``; platform-level keys at every block
location, plus a differential against the full ``gateway.config.load_gateway_config``; the Hermes home of
``--live``: no wrong-home read, and that it writes nothing into the home it checks. Real-Hermes tests run the
Hermes venv in a subprocess with a scratch HERMES_HOME under <worktree>/.local/hh-t154.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from glasses import admin, hermes_merge
from glasses import config as gcfg

from conftest import PLUGIN_ROOT
from test_config_check_live import WORKTREE, free_port, hermes_python

SECRET = "EDGEfakeSECRET7731"
EXTRA = {"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant", "work"]}
GOOD = admin.ProfileFacts(
    stt_provider="local",
    stt_language="",
    glasses_tools=frozenset({"web"}),
    telegram_tools=frozenset({"web"}),
    clarify_timeout=0,
    approvals_timeout=300,
)


def plat(extra=None, **level):
    return {"gateway": {"platforms": {"glasses": {"enabled": True, "extra": extra or EXTRA, **level}}}}


def check_file(tmp_path, data=None, text=None, *extra_args):
    path = tmp_path / "config.yaml"
    path.write_text(text if text is not None else yaml.safe_dump(data), encoding="utf-8")
    out = io.StringIO()
    return admin.main(["config", "check", "--config", str(path), *extra_args], {}, out), out.getvalue()


def live_home(tmp_path):
    (tmp_path / "glasses").mkdir(mode=0o700, parents=True)
    (tmp_path / "glasses").chmod(0o700)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(plat()), encoding="utf-8")
    return tmp_path


def run_live(home, facts):
    out = io.StringIO()
    probes = admin.Probes(port_free=lambda _h, _p: True, resolves=lambda _h: True, hermes=lambda _c: facts)
    code = admin.main(["config", "check", "--live", "--hermes-home", str(home)], {}, out, probes)
    return code, out.getvalue()


# -- never echo a value containing '@' or '://' ----------------------------------------------------------


def test_config_path_with_a_scheme_is_not_shown(tmp_path):
    out = io.StringIO()
    admin.main(["config", "check", "--config", f"/nonexistent/x://{SECRET}/config.yaml"], {}, out)
    assert SECRET not in out.getvalue(), out.getvalue()


@pytest.mark.parametrize(
    "data",
    [
        plat(extra={**EXTRA, f"https://u:{SECRET}@h": 1}),
        plat(extra={**EXTRA, "meeting": {f"u:{SECRET}@h": 1}}),
        plat(**{f"https://u:{SECRET}@h": 1}),
    ],
    ids=["extra", "nested", "platform-level"],
)
def test_unknown_key_named_like_a_credential_url_not_shown(tmp_path, data):
    _code, out = check_file(tmp_path, data)
    assert SECRET not in out, out


def test_live_route_to_a_credential_looking_profile_not_shown(tmp_path):
    facts = admin.HermesFacts(frozenset({"default"}), {"work": f"https://u:{SECRET}@h"}, {"default": GOOD})
    code, out = run_live(live_home(tmp_path), facts)
    assert code == admin.EXIT_ERRORS and "is routed to profile" in out
    assert SECRET not in out, out


def test_live_tool_names_are_filtered_too(tmp_path):
    odd = admin.ProfileFacts("local", "", frozenset({f"u:{SECRET}@h"}), frozenset({"web"}), 0, 300)
    facts = admin.HermesFacts(frozenset({"default", "work"}), {"work": "work"}, {"default": GOOD, "work": odd})
    code, out = run_live(live_home(tmp_path), facts)
    assert code == admin.EXIT_ERRORS and SECRET not in out, out


# -- platform-level keys at every block location ----------------------------------------------------------

BAD_LEVEL = {"host": "0.0.0.0", "allow_from": ["*"], "dm_policy": "open"}


@pytest.mark.parametrize(
    "data",
    [
        {"gateway": {"platforms": {"glasses": {"enabled": True, "extra": EXTRA, **BAD_LEVEL}}}},
        {"platforms": {"glasses": {"enabled": True, "extra": EXTRA, **BAD_LEVEL}}},
        {"gateway": {"glasses": {"enabled": True, "extra": EXTRA, **BAD_LEVEL}}},
        {"glasses": {"enabled": True, "extra": EXTRA, **BAD_LEVEL}},
        {**plat(), "glasses": dict(BAD_LEVEL)},
        {**plat(), "platforms": {"glasses": dict(BAD_LEVEL)}},
    ],
    ids=["gateway.platforms", "platforms", "gateway.glasses", "root", "root over extra", "platforms over extra"],
)
def test_non_loopback_host_and_allow_all_keys_refused_at_every_location(tmp_path, data):
    code, out = check_file(tmp_path, data)
    assert code == admin.EXIT_ERRORS, out
    for needle in (
        "error: glasses.host: must be a loopback IP literal",
        "error: glasses.allow_from: not allowed",
        "error: glasses.dm_policy: not allowed",
    ):
        assert needle in out, out


def test_boolish_enabled_string_is_not_reported_as_disabled(tmp_path):
    code, out = check_file(tmp_path, {"gateway": {"platforms": {"glasses": {"enabled": "yes", "extra": EXTRA}}}})
    assert code == admin.EXIT_OK and "enabled: not true" not in out, out


def test_deeply_nested_yaml_is_exit_2(tmp_path):
    code, out = check_file(tmp_path, None, "gateway: " + "[" * 20000 + "]" * 20000)
    assert code == admin.EXIT_UNREADABLE and len(out.splitlines()) == 1


# -- missing modules on the real CLI ----------------------------------------------------------------------------


def _run_without(module, argv, tmp_path):
    runner = tmp_path / "run_without.py"
    runner.write_text(
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(PLUGIN_ROOT)!r})\n"
        f"sys.modules[{module!r}] = None\n"
        f"sys.argv = ['glasses.admin', *{argv!r}]\n"
        "runpy.run_module('glasses.admin', run_name='__main__', alter_sys=True)\n",
        encoding="utf-8",
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(runner)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.parametrize("module,name", [("jsonschema", "jsonschema"), ("yaml", "PyYAML")])
def test_real_cli_live_without_a_module_is_one_line_exit_2(tmp_path, module, name):
    home = live_home(tmp_path / "home")
    proc = _run_without(module, ["config", "check", "--live", "--hermes-home", str(home)], tmp_path)
    assert proc.returncode == admin.EXIT_UNREADABLE, proc.stdout + proc.stderr
    assert proc.stdout.splitlines() == [f"error: {gcfg.DependencyMissing(name)}"] and proc.stderr == ""
    assert not (tmp_path / ".hermes").exists()


# -- real Hermes (subprocess, scratch HERMES_HOME) ---------------------------------------------------------------


def _hermes_run(script, tmp, *args, env_extra=None):
    runner = Path(tmp) / "runner.py"
    runner.write_text(script, encoding="utf-8")
    (Path(tmp) / "home").mkdir(exist_ok=True)
    env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "HERMES_HOME": str(Path(tmp) / "home"), **(env_extra or {})}
    return subprocess.run(
        [str(hermes_python()), str(runner), *args], cwd=tmp, env=env, capture_output=True, text=True, timeout=240
    )


def _scratch():
    base = WORKTREE / ".local" / "hh-t154"
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=base)


DIFF_RUNNER = f"""\
import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(PLUGIN_ROOT)!r})
import yaml
from gateway.config import load_gateway_config
from gateway.platform_registry import PlatformEntry, platform_registry
from glasses import config as gcfg
platform_registry.register(PlatformEntry(name="glasses", label="Glasses", adapter_factory=lambda c: None,
    check_fn=lambda: True, validate_config=gcfg.validate_config, required_env=[], install_hint="", source="plugin"))
home = Path(os.environ["HERMES_HOME"])
out = []
for case in json.load(open(sys.argv[1])):
    (home / "config.yaml").write_text(yaml.safe_dump(case))
    pc = {{str(getattr(k, "value", k)): v for k, v in load_gateway_config().platforms.items()}}.get("glasses")
    out.append(None if pc is None else {{"enabled": pc.enabled,
               "extra": {{k: v for k, v in pc.extra.items() if k != "_enabled_explicit"}}}})
print(json.dumps(out))
"""
DIFF_CASES = [
    {"gateway": {"platforms": {"glasses": {"enabled": True, **BAD_LEVEL, "extra": EXTRA}}}},
    {"glasses": {"enabled": True, **BAD_LEVEL, "extra": EXTRA}},
    {**plat(), "glasses": {"host": "0.0.0.0", "allow_from": "*", "dm_policy": "open"}},
    {"gateway": {"glasses": {"enabled": True, "host": "0.0.0.0", "allow_from": ["*"]}}},
    {"platforms": {"glasses": {"enabled": True, "host": "0.0.0.0", "group_policy": "open", "allow_admin_from": ["*"]}}},
    {
        "gateway": {"platforms": {"glasses": {"enabled": True, "extra": {"host": "::1"}}}},
        "glasses": {"extra": {"host": "0.0.0.0"}},
    },
    {"gateway": {"platforms": {"glasses": {"enabled": True, "host": "0.0.0.0", "extra": {**EXTRA, "host": "::1"}}}}},
]


def test_view_equals_the_full_hermes_gateway_load():
    """Differential over load_gateway_config (legacy json + YAML layer + root-block bridge + env overrides)."""
    with _scratch() as tmp:
        (Path(tmp) / "cases.json").write_text(json.dumps(DIFF_CASES), encoding="utf-8")
        proc = _hermes_run(DIFF_RUNNER, tmp, "cases.json")
    assert proc.returncode == 0, proc.stderr[-2000:]
    hermes = json.loads(proc.stdout.strip().splitlines()[-1])
    for case, got in zip(DIFF_CASES, hermes, strict=True):
        view = hermes_merge.platform_view(case)
        assert got is not None and view is not None, case
        assert view.extra == got["extra"], (case, got)
        assert gcfg.check(view.extra).errors == gcfg.check(got["extra"]).errors


NOJS_RUNNER = f"""\
import io, logging, sys
sys.path.insert(0, {str(PLUGIN_ROOT)!r})
buf = io.StringIO()
logging.basicConfig(stream=buf, level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")
from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
import glasses
sys.modules["jsonschema"] = None
glasses.config._validator.cache_clear()
platform_registry.register(PlatformEntry(name="glasses", label="Glasses", adapter_factory=glasses._create_adapter,
    check_fn=glasses.check_requirements, validate_config=glasses.validate_config, required_env=[],
    install_hint="aiohttp, jsonschema, cryptography", source="plugin", is_connected=glasses.is_connected))
pc = PlatformConfig(enabled=True, extra={{"public_url": "wss://glasses.example.com/ws"}})
print(platform_registry.create_adapter("glasses", pc), glasses.is_connected(pc), glasses.validate_config(pc))
print("LOG", repr(buf.getvalue()))
"""


def test_gateway_side_without_jsonschema_logs_no_traceback():
    with _scratch() as tmp:
        proc = _hermes_run(NOJS_RUNNER, tmp)
    assert proc.returncode == 0 and proc.stderr == "", proc.stderr[-2000:]
    first, log_line = proc.stdout.splitlines()[-2:]
    assert first == "None False False"
    log = ast.literal_eval(log_line[4:])  # repr of a str written by the runner
    assert "Traceback" not in log and "requirements not met (aiohttp, jsonschema" in log
    mine = [line for line in log.splitlines() if line.startswith(("ERROR glasses", "WARNING glasses"))]
    assert mine and all("the Python module 'jsonschema' is not installed" in line for line in mine)


def _live_scratch(root: Path, stt_provider=True) -> Path:
    home = root / "home"
    (root / "state").mkdir(mode=0o700)
    extra = {
        "port": free_port(),
        "bridge_port": free_port(),
        "public_url": "wss://localhost/ws",
        "state_dir": str(root / "state"),
        "lanes": ["assistant", "work"],
    }
    stt = {"provider": "local", "language": ""} if stt_provider else {"language": ""}
    profile = {
        "stt": stt,
        "agent": {"clarify_timeout": 0},
        "approvals": {"timeout": 60},
        "platform_toolsets": {"glasses": ["web"], "telegram": ["web"]},
    }
    (home / "profiles" / "work").mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "gateway": {
                "multiplex_profiles": True,
                "platforms": {"glasses": {"enabled": True, "extra": extra}},
                "profile_routes": [{"platform": "glasses", "chat_id": "glasses:work", "profile": "work"}],
            },
            **profile,
        }),
        encoding="utf-8",
    )
    (home / "profiles" / "work" / "config.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    return home


def _snapshot(home: Path) -> dict[str, tuple[int, int, int]]:
    """(mtime, size, mode) of the home directory itself (``.``) and everything below it."""
    return {
        str(p.relative_to(home)): (p.lstat().st_mtime_ns, p.lstat().st_size, p.lstat().st_mode)
        for p in [home, *sorted(home.rglob("*"))]
    }


def _real_live(home: Path, user: Path, env_extra=None, timeout=240):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(user), **(env_extra or {})}  # no HERMES_HOME: only --hermes-home
    return subprocess.run(
        [str(hermes_python()), "-m", "glasses.admin", "config", "check", "--live", "--hermes-home", str(home)],
        cwd=PLUGIN_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_real_live_unset_stt_provider_is_a_warning_and_no_wrong_home():
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp), stt_provider=False), Path(tmp) / "user"
        user.mkdir()
        proc = _real_live(home, user)
        assert not (user / ".hermes").exists(), sorted(os.listdir(user))
    assert proc.returncode == admin.EXIT_OK, proc.stdout + proc.stderr[-2000:]
    warnings = [line for line in proc.stdout.splitlines() if line.startswith("warning: ")]
    assert sorted(w.split(":")[1].strip() for w in warnings) == ["profile default", "profile work"], proc.stdout
    assert all("stt.provider is not set" in w for w in warnings)


def test_real_live_second_run_on_an_initialised_home_writes_nothing():
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        assert _real_live(home, user).returncode == admin.EXIT_OK
        before = _snapshot(home)
        proc = _real_live(home, user)
        after = _snapshot(home)
    assert proc.returncode == admin.EXIT_OK and after == before


def test_real_live_does_not_write_into_the_hermes_home():
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        before = _snapshot(home)
        proc = _real_live(home, user)
        after = _snapshot(home)
    assert proc.returncode == admin.EXIT_OK, proc.stdout + proc.stderr[-2000:]
    assert after == before, sorted(set(after) ^ set(before))
