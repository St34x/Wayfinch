# SPDX-License-Identifier: Apache-2.0
"""Edge and failure cases of ``glasses-admin config check --live`` against a scratch Hermes home: the home directory
itself and the state dir stay identical (not only the entries below them), a messy ``<home>/.env`` is parsed and
never rewritten, a ``HERMES_HOME=`` or ``HOME=`` line in it moves nothing, no ``.env`` value is printed, and the
owner refusal opens no file. Names, arguments and ``enabled`` are in test_config_check_live_names_exact.py.
Real-Hermes cases run the Hermes venv in a subprocess under <worktree>/.local/hh-t154."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from glasses import admin
from glasses import hermes_facts as hf

from conftest import PLUGIN_ROOT
from test_config_check_edges import _hermes_run, _live_scratch, _real_live, _scratch
from test_config_check_live import hermes_python

CANARY = "QAcanaryTOKEN8731xyz"


def _tree(root: Path) -> dict[str, tuple[int, int, int, str]]:
    """(mtime_ns, size, mode, sha256) of *root* itself and everything below it."""
    out = {}
    for p in [root, *sorted(root.rglob("*"))]:
        st = p.lstat()
        try:
            digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else ""
        except PermissionError:
            digest = "unreadable"
        out[str(p.relative_to(root)) or "."] = (st.st_mtime_ns, st.st_size, st.st_mode, digest)
    return out


def _skeleton(home: Path) -> None:
    for root in (home, home / "profiles" / "work"):
        for sub in ("cron", "hooks", "logs", "memories", "sessions", "skills", "backups/config"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        (root / "SOUL.md").write_text("seed", encoding="utf-8")
        root.chmod(0o755)


def _env_run(home: Path, user: Path, *, extra=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(user), **(extra or {})}
    return subprocess.run(
        [str(hermes_python()), "-m", "glasses.admin", "config", "check", "--live", "--hermes-home", str(home)],
        cwd=PLUGIN_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )


# -- the home directory itself, the state dir and the scratch root stay identical ---------------------------------


@pytest.mark.parametrize("initialised", [False, True], ids=["fresh", "initialised"])
def test_real_live_leaves_home_state_and_scratch_root_identical(initialised):
    with _scratch() as tmp:
        root = Path(tmp)
        home, user = _live_scratch(root), root / "user"
        user.mkdir()
        if initialised:
            _skeleton(home)
        before = {p: _tree(p) for p in (home, root / "state")}
        listing = sorted(os.listdir(root))
        proc = _real_live(home, user)
        after = {p: _tree(p) for p in (home, root / "state")}
        assert sorted(os.listdir(root)) == listing and os.listdir(user) == []
    assert proc.returncode == admin.EXIT_OK, proc.stdout + proc.stderr[-2000:]
    assert after == before
    assert not any(".good" in key for key in after[home])  # Hermes' last-known-good backup of config.yaml


# -- <home>/.env: parsed, never rewritten, nothing it names is printed ------------------------------------------------

MESSY_ENV = (
    "\ufeff# comment\r\n"
    "export HERMES_LOCAL_STT_LANGUAGE = 'en'\r\n"
    "  APPROVAL_TIMEOUT=0   \r\n"
    "APPROVAL_TIMEOUT=0\n"
    f'QA_SECRET="{CANARY}"\r\n'
    "=novalue\n"
    "A B=c\n"
    "NO_EQUALS_LINE\n"
    "LAST=1"  # no trailing newline
)


def _messy_home(root: Path) -> Path:
    home = _live_scratch(root)
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    config["approvals"]["timeout"] = "${APPROVAL_TIMEOUT}"
    config["stt"]["language"] = ""
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (home / ".env").write_bytes(MESSY_ENV.encode("utf-8"))
    (home / ".env").chmod(0o600)
    (home / "profiles" / "work" / ".env").write_bytes(b"WORK=1\r\nQA_SECRET2=" + CANARY.encode() + b"\r\n")
    return home


def test_a_messy_dotenv_is_byte_and_mtime_identical_after_a_real_run_and_not_printed():
    with _scratch() as tmp:
        root = Path(tmp)
        home, user = _messy_home(root), root / "user"
        user.mkdir()
        before = _tree(home)
        proc = _real_live(home, user)
        after = _tree(home)
    assert after == before
    out = proc.stdout + proc.stderr
    assert proc.returncode in (admin.EXIT_OK, admin.EXIT_ERRORS), out[-2000:]
    assert CANARY not in out and "Traceback" not in out


def test_dotenv_that_cannot_be_read_is_ignored_and_left_alone():
    if os.geteuid() == 0:
        pytest.skip("root reads a 0000 file")
    with _scratch() as tmp:
        root = Path(tmp)
        home, user = _messy_home(root), root / "user"
        user.mkdir()
        (home / ".env").chmod(0)
        try:
            before = _tree(home)
            proc = _real_live(home, user)
            after = _tree(home)
        finally:
            (home / ".env").chmod(0o600)
    assert after == before
    assert proc.returncode in (admin.EXIT_OK, admin.EXIT_ERRORS) and "Traceback" not in proc.stderr
    assert CANARY not in proc.stdout + proc.stderr


def test_home_and_hermes_home_lines_in_the_dotenv_move_nothing(tmp_path_factory):
    with _scratch() as tmp:
        root = Path(tmp)
        home, user = _live_scratch(root), root / "user"
        user.mkdir()
        elsewhere, other_home = root / "elsewhere", root / "other-home"
        (home / ".env").write_text(f"HERMES_HOME={elsewhere}\nHOME={other_home}\nUSERPROFILE={other_home}\n")
        proc = _real_live(home, user)
        listing = sorted(os.listdir(root))
    assert proc.returncode == admin.EXIT_OK, proc.stdout + proc.stderr[-2000:]
    assert "elsewhere" not in listing and "other-home" not in listing, listing
    assert not (user / ".hermes").exists()


# -- the environment is restored on every way out ---------------------------------------------------------------------


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_environ_with_restores_nested_overlays_on_any_exit(monkeypatch, error):
    monkeypatch.setenv("QA_A", "orig")
    monkeypatch.setenv("QA_EMPTY", "")
    monkeypatch.delenv("QA_NEW", raising=False)
    snapshot = dict(os.environ)
    with (
        pytest.raises(error),
        hf.environ_with({"QA_A": "1", "QA_EMPTY": "x", "QA_NEW": "1"}),
        hf.environ_with({"QA_A": "2", "QA_NEW": "2"}),
    ):
        raise error
    assert dict(os.environ) == snapshot


def test_environ_with_leaves_a_variable_the_block_itself_set_to_the_restored_value(monkeypatch):
    monkeypatch.setenv("QA_B", "orig")
    with hf.environ_with({"QA_B": "over"}):
        os.environ["QA_B"] = "changed inside"
    assert os.environ["QA_B"] == "orig"


FAIL_RUNNER = """\
import json, os, sys
from pathlib import Path
sys.path.insert(0, __PLUGIN_ROOT__)
from glasses import config as gcfg
from glasses import hermes_facts as hf
home = Path(os.environ["HERMES_HOME"])
(home / "profiles" / "work").mkdir(parents=True)
route = {"platform": "glasses", "chat_id": "glasses:work", "profile": "work"}
text = "gateway: {multiplex_profiles: true, profile_routes: [%s]}\\nstt: {provider: local}\\n"
(home / "config.yaml").write_text(text % json.dumps(route))
(home / "profiles" / "work" / "config.yaml").write_text("stt: [unclosed\\n")  # the routed profile does not parse
(home / ".env").write_text("QA_FROM_ENV=1\\nHERMES_LOCAL_STT_LANGUAGE=en\\n")
before = dict(os.environ)
try:
    hf.hermes_facts(gcfg.load({"public_url": "wss://glasses.example.com/ws", "lanes": ["work"]}))
    raised = False
except Exception as exc:
    raised = type(exc).__name__
print(json.dumps({"raised": raised, "same": dict(os.environ) == before, "leaked": "QA_FROM_ENV" in os.environ}))
""".replace("__PLUGIN_ROOT__", repr(str(PLUGIN_ROOT)))


def test_real_facts_restore_the_environment_when_a_routed_profile_does_not_parse():
    with _scratch() as tmp:
        proc = _hermes_run(FAIL_RUNNER, tmp)
    assert proc.returncode == 0, proc.stderr[-2000:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["raised"] and result["same"] and not result["leaked"], result


# -- the owner refusal opens nothing ------------------------------------------------------------------------------


def test_owner_refusal_opens_no_file_of_the_home(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("gateway: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"X={CANARY}\n", encoding="utf-8")
    opened: list[str] = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: opened.append(str(p)) or real_open(p, *a, **k))
    import builtins

    real_builtin = builtins.open
    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: opened.append(str(p)) or real_builtin(p, *a, **k))

    def fail(_cfg):
        raise AssertionError("Hermes was asked")

    probes = admin.Probes(port_free=lambda *_: True, resolves=lambda _h: True, hermes=fail, owns_home=lambda _h: False)
    out = io.StringIO()
    code = admin.main(["config", "check", "--live", "--hermes-home", str(tmp_path)], {}, out, probes)
    assert code == admin.EXIT_UNREADABLE and len(out.getvalue().splitlines()) == 1
    assert not [p for p in opened if str(tmp_path) in p], opened
    assert CANARY not in out.getvalue()


def test_owner_refusal_comes_before_the_config_mismatch_and_a_missing_config(tmp_path):
    probes = admin.Probes(
        port_free=lambda *_: True, resolves=lambda _h: True, hermes=lambda _c: None, owns_home=lambda _h: False
    )
    out = io.StringIO()
    other = tmp_path / "elsewhere.yaml"
    code = admin.main(
        ["config", "check", "--live", "--hermes-home", str(tmp_path), "--config", str(other)], {}, out, probes
    )
    lines = out.getvalue().splitlines()
    assert code == admin.EXIT_UNREADABLE and len(lines) == 1 and "owned by another user" in lines[0], lines


def test_home_that_is_a_dangling_symlink_is_refused_not_crashed(tmp_path):
    link = tmp_path / "home"
    link.symlink_to(tmp_path / "gone")
    out = io.StringIO()
    code = admin.main(["config", "check", "--live", "--hermes-home", str(link)], {}, out, None or admin.DEFAULT_PROBES)
    assert code == admin.EXIT_UNREADABLE and len(out.getvalue().splitlines()) == 1, out.getvalue()
