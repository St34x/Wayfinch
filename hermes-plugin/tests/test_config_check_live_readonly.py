# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin config check --live`` is strictly read-only: a snapshot (path, mtime, size, mode) of a scratch
Hermes home is identical before and after a run against real Hermes code (fresh home, initialised home, after a
``config.yaml`` edit), the effective user must own the home (the owner check is injected: no chown, no root), and the
configs the check reads equal what Hermes' own loaders return (differential, so a Hermes bump fails loudly).
Real-Hermes tests run the Hermes venv in a subprocess with a scratch HERMES_HOME under <worktree>/.local/hh-t154.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from glasses import admin
from glasses import hermes_facts as hf

from conftest import PLUGIN_ROOT
from test_config_check_edges import _hermes_run, _live_scratch, _real_live, _scratch, _snapshot, live_home

SKELETON = (
    "audio_cache",
    "backups/config",
    "cron",
    "hooks",
    "image_cache",
    "logs/curator",
    "memories",
    "pairing",
    "sessions",
    "skills",
)


def _initialise(home: Path) -> None:
    """The directory skeleton Hermes creates in a home it loads, made by hand (so the check did not make it)."""
    for root in (home, home / "profiles" / "work"):
        for sub in SKELETON:
            (root / sub).mkdir(parents=True, exist_ok=True)
        (root / "SOUL.md").write_text("seed", encoding="utf-8")
        root.chmod(0o755)  # Hermes would chmod a profile dir to 0700


# -- real Hermes: nothing is written ------------------------------------------------------------------------------


def test_real_live_leaves_an_initialised_home_untouched():
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        _initialise(home)
        before = _snapshot(home)
        proc = _real_live(home, user)
        after = _snapshot(home)
        assert not (user / ".hermes").exists(), sorted(os.listdir(user))
    assert proc.returncode == admin.EXIT_OK, proc.stdout + proc.stderr[-2000:]
    assert after == before, sorted(set(after) ^ set(before))


def test_real_live_after_a_config_edit_keeps_no_backup_of_it():
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        assert _real_live(home, user).returncode == admin.EXIT_OK
        config = home / "config.yaml"
        config.write_text(config.read_text(encoding="utf-8") + "# edited\nlogging: {level: INFO}\n", encoding="utf-8")
        before = _snapshot(home)
        proc = _real_live(home, user)
        after = _snapshot(home)
    assert proc.returncode == admin.EXIT_OK, proc.stdout + proc.stderr[-2000:]
    assert after == before, sorted(set(after) ^ set(before))
    assert not [path for path in after if path.startswith(("backups", "profiles/work/backups"))]


# -- the owner check (injected: no chown, no root) ------------------------------------------------------------------


def _probes(owns_home, hermes=None):
    def fail(_cfg):
        raise AssertionError("Hermes was asked although the home is not owned")

    return admin.Probes(
        port_free=lambda _h, _p: True, resolves=lambda _h: True, hermes=hermes or fail, owns_home=owns_home
    )


def test_live_refuses_a_home_owned_by_another_user_and_reads_nothing(tmp_path):
    out = io.StringIO()  # no config.yaml in the home: a read would say "cannot read", not "owned"
    code = admin.main(["config", "check", "--live", "--hermes-home", str(tmp_path)], {}, out, _probes(lambda _h: False))
    lines = out.getvalue().splitlines()
    assert code == admin.EXIT_UNREADABLE and len(lines) == 1, lines
    assert lines[0].startswith("error: --live: the Hermes home ") and "owned by another user" in lines[0]


def test_the_owner_check_is_asked_about_the_checked_home_and_only_for_live(tmp_path):
    asked = []
    home = live_home(tmp_path)
    probes = _probes(lambda path: asked.append(path) or False)
    assert admin.main(["config", "check", "--hermes-home", str(home)], {}, io.StringIO(), probes) != 2
    assert asked == []
    admin.main(["config", "check", "--live", "--hermes-home", str(home)], {}, io.StringIO(), probes)
    assert asked == [home]


def test_home_is_owned_compares_the_effective_user_with_the_owner(tmp_path, monkeypatch):
    assert admin.home_is_owned(tmp_path)
    assert admin.home_is_owned(tmp_path / "missing")  # nothing to protect yet
    monkeypatch.setattr(os, "geteuid", lambda: tmp_path.stat().st_uid + 1)
    assert not admin.home_is_owned(tmp_path)


def test_home_that_cannot_be_examined_is_not_owned(tmp_path, monkeypatch):
    def deny(_self, **_kw):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "stat", deny)
    assert not admin.home_is_owned(tmp_path)


# -- the throwaway home Hermes is imported under ----------------------------------------------------------------------


def test_hermes_is_imported_under_a_throwaway_home_that_is_gone_afterwards():
    calls: list[tuple[str, str]] = []
    fake = SimpleNamespace(
        set_hermes_home_override=lambda path: calls.append(("set", str(path))) or "token",
        reset_hermes_home_override=lambda token: calls.append(("reset", token)),
    )
    with pytest.MonkeyPatch.context() as patch:  # undone before conftest checks that no Hermes module leaked
        patch.setitem(sys.modules, "hermes_constants", fake)
        with hf.quarantined_import():
            ((_, scratch),) = calls
            assert Path(scratch).is_dir()
    assert calls[1:] == [("reset", "token")] and not Path(scratch).exists()


# -- the configs we read equal what the Hermes loaders return -------------------------------------------------------

LOADERS_RUNNER = f"""\
import json, os, sys
from pathlib import Path
sys.path.insert(0, {str(PLUGIN_ROOT)!r})
import yaml
from glasses import hermes_facts as hf
with hf.quarantined_import():
    import gateway.config_loader
    import hermes_cli.config
from gateway.config import load_gateway_config
from hermes_cli.config import load_config_readonly
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
out = {{"config": [], "gateway": []}}
for number, case in enumerate(json.load(open(sys.argv[1]))):
    home = Path(os.environ["HERMES_HOME"]) / f"case{{number}}"
    home.mkdir()
    if case.get("config") is not None:
        (home / "config.yaml").write_text(yaml.safe_dump(case["config"]))
    if case.get("gateway_json") is not None:
        (home / "gateway.json").write_text(json.dumps(case["gateway_json"]))
    mine_config, mine_gw = hf.read_profile_config(home), hf.read_gateway_config(home)
    token = set_hermes_home_override(home)
    theirs_config, theirs_gw = load_config_readonly(), load_gateway_config()
    reset_hermes_home_override(token)
    out["config"].append([mine_config, theirs_config])
    out["gateway"].append([[gw.multiplex_profiles, gw.to_dict()["profile_routes"]] for gw in (mine_gw, theirs_gw)])
print(json.dumps(out, default=str, sort_keys=True))
"""
ROUTE = {"platform": "glasses", "chat_id": "glasses:work", "profile": "work"}
LOADER_CASES = [
    {"config": None},
    {"config": {}},
    {"config": {"stt": {"provider": "local"}, "approvals": {"timeout": "${LOADERS_CANARY}"}}},
    {"config": {"model": "vendor/some-model", "stt": None, "agent": {"clarify_timeout": 0}}},
    {"config": {"platform_toolsets": {"glasses": ["web"], "telegram": ["web", "todo"]}}},
    {"config": {"multiplex_profiles": True, "profile_routes": [ROUTE]}},
    {"config": {"gateway": {"multiplex_profiles": True, "profile_routes": [ROUTE]}}},
    {"config": {"gateway": {"multiplex_profiles": False}, "multiplex_profiles": True}},
    {"config": {"gateway": {"profile_routes": "not a list"}}, "gateway_json": {"multiplex_profiles": True}},
    {"config": {"gateway": {"platforms": {"glasses": {"enabled": True}}}}, "gateway_json": {"profile_routes": [ROUTE]}},
]


def test_the_configs_read_equal_what_the_hermes_loaders_return():
    with _scratch() as tmp:
        (Path(tmp) / "cases.json").write_text(json.dumps(LOADER_CASES), encoding="utf-8")
        proc = _hermes_run(LOADERS_RUNNER, tmp, "cases.json", env_extra={"LOADERS_CANARY": "42"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert len(result["config"]) == len(result["gateway"]) == len(LOADER_CASES)
    for case, (mine, theirs) in zip(LOADER_CASES, result["config"], strict=True):
        assert mine == theirs, case
    for case, (mine, theirs) in zip(LOADER_CASES, result["gateway"], strict=True):
        assert mine == theirs, case
    canary = result["config"][2][0]["approvals"]["timeout"]
    multiplex, routes = result["gateway"][5][0]  # the cases do distinguish
    assert canary == "42" and multiplex is True and [route["profile"] for route in routes] == ["work"]


# -- <home>/.env is loaded read-only before the env-dependent facts ---------------------------------------------------

DOTENV_CANARY = "T124fakeSECRET5509"  # must never reach the output
DOTENV = f"HERMES_LOCAL_STT_LANGUAGE=en\nAPPROVAL_TIMEOUT=0\nHERMES_HOME=/elsewhere\nT124_CANARY={DOTENV_CANARY}\n"
ENV_RUNNER = """\
import json, os, sys
from pathlib import Path
sys.path.insert(0, __PLUGIN_ROOT__)
import yaml
from glasses import config as gcfg
from glasses import hermes_facts as hf
home = Path(os.environ["HERMES_HOME"])
(home / "profiles" / "work").mkdir(parents=True)
route = {"platform": "glasses", "chat_id": "glasses:work", "profile": "work"}
(home / "config.yaml").write_text(yaml.safe_dump({"gateway": {"multiplex_profiles": True, "profile_routes": [route]},
    "stt": {"provider": "local"}, "approvals": {"timeout": "${APPROVAL_TIMEOUT}"}}))
(home / "profiles" / "work" / "config.yaml").write_text(yaml.safe_dump({"approvals": {"timeout": "${WORK_TIMEOUT}"}}))
(home / ".env").write_text(sys.argv[1])
(home / "profiles" / "work" / ".env").write_text("WORK_TIMEOUT=7\\n")
files = [home / ".env", home / "profiles" / "work" / ".env"]
stat = lambda: [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
env_before, files_before = dict(os.environ), stat()
facts = hf.hermes_facts(gcfg.load({"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant", "work"]}))
print(json.dumps({"env_same": dict(os.environ) == env_before, "files_same": stat() == files_before,
    "served": sorted(facts.served),
    "default": [facts.profiles["default"].stt_language, facts.profiles["default"].approvals_timeout],
    "work": [facts.profiles["work"].stt_language, facts.profiles["work"].approvals_timeout]}))
""".replace("__PLUGIN_ROOT__", repr(str(PLUGIN_ROOT)))


def test_dotenv_reaches_the_env_facts_and_the_process_env_and_files_stay_as_they_were():
    with _scratch() as tmp:
        proc = _hermes_run(ENV_RUNNER, tmp, DOTENV)
    assert proc.returncode == 0, proc.stderr[-2000:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["env_same"] and result["files_same"] and result["served"] == ["default", "work"], result
    # HERMES_LOCAL_STT_LANGUAGE comes from the launch .env for every profile; ${VAR} refs see the launch .env
    # (default) and, for a routed profile, its own .env
    assert result["default"] == ["en", 0] and result["work"] == ["en", 7], result
    assert DOTENV_CANARY not in proc.stdout + proc.stderr


def test_real_live_reports_what_only_the_dotenv_makes_wrong_and_prints_no_dotenv_value():
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        config["approvals"]["timeout"] = "${APPROVAL_TIMEOUT}"
        config["gateway"]["platforms"]["glasses"]["extra"]["meeting"] = {"language_hints": ["fr"]}
        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        control = _real_live(home, user)  # without a .env: nothing to report
        (home / ".env").write_text(DOTENV, encoding="utf-8")
        before = _snapshot(home)
        proc = _real_live(home, user)
        after = _snapshot(home)
    assert control.returncode == admin.EXIT_OK, control.stdout + control.stderr[-2000:]
    assert proc.returncode == admin.EXIT_ERRORS, proc.stdout + proc.stderr[-2000:]
    assert 'error: profile default: stt.language is "en" but meeting.language_hints has fr' in proc.stdout
    assert "error: profile default: approvals.timeout resolves to 0" in proc.stdout
    assert after == before and DOTENV_CANARY not in proc.stdout + proc.stderr


def test_environ_with_restores_the_environment_exactly(monkeypatch):
    monkeypatch.setenv("T124_KEPT", "before")
    monkeypatch.delenv("T124_ADDED", raising=False)
    snapshot = dict(os.environ)
    with pytest.raises(RuntimeError), hf.environ_with({"T124_KEPT": "over", "T124_ADDED": "new"}):
        assert os.environ["T124_KEPT"] == "over" and os.environ["T124_ADDED"] == "new"
        raise RuntimeError
    assert dict(os.environ) == snapshot


def test_dotenv_names_the_environment_refuses_or_the_home_are_skipped(tmp_path):
    env = {"GOOD": "1", "": "x", "A=B": "x", "NUL": "a\x00b", hf.HOME_ENV: "/elsewhere"}
    (tmp_path / ".env").write_text("GOOD=1\n", encoding="utf-8")  # the parse itself is faked below
    with pytest.MonkeyPatch.context() as patch:  # undone before conftest checks that no Hermes module leaked
        patch.setitem(sys.modules, "agent", SimpleNamespace())
        patch.setitem(sys.modules, "agent.secret_scope", SimpleNamespace(load_env_file=lambda path: dict(env)))
        assert hf.read_env_file(tmp_path) == {"GOOD": "1"}
