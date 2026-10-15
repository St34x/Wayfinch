# SPDX-License-Identifier: Apache-2.0
"""What a ``<home>/.env`` can make ``config check --live`` do: it makes the checker run no code (plugin switches,
``PATH``, ``LD_*`` are not overlaid, the overlay never wraps plugin discovery), nor hang it (FIFO), nor is it followed
out of the home (symlink) or read whole (huge file); the environment is put back even when the update is
interrupted. Output rules for ``.env`` values are in test_config_check_env_secrets.py."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

from glasses import admin
from glasses import hermes_env as he

from conftest import PLUGIN_ROOT
from test_config_check_edges import _hermes_run, _live_scratch, _real_live, _scratch
from test_config_check_env_secrets import UNSAFE, _dir, _edit, _run

# -- a .env cannot make the checker run code --------------------------------------------------------------------

MARKER_PLUGIN = """\
from pathlib import Path
Path({marker!r}).write_text("ran")
def register(ctx):
    pass
"""
MANIFEST = "name: boom\nversion: 1.0.0\ndescription: planted by a test\nkind: backend\n"


def _plant(root: Path) -> tuple[Path, Path]:
    """A bundled-plugins dir with a plugin that writes a marker file when its package is imported."""
    plugins = root / "bundled"
    (plugins / "boom").mkdir(parents=True)
    (plugins / "boom" / "plugin.yaml").write_text(MANIFEST, encoding="utf-8")
    marker = root / "marker"
    (plugins / "boom" / "__init__.py").write_text(MARKER_PLUGIN.format(marker=str(marker)), encoding="utf-8")
    return plugins, marker


def test_a_plugin_dir_named_by_the_dotenv_is_not_loaded_while_the_process_env_one_is():
    with _scratch() as tmp:
        plugins, marker = _plant(Path(tmp))
        home, user = _live_scratch(_dir(Path(tmp) / "scratch")), Path(tmp) / "user"
        user.mkdir()
        control = _real_live(home, user, env_extra={"HERMES_BUNDLED_PLUGINS": str(plugins)})
        assert marker.exists(), (
            control.stdout + control.stderr[-2000:]
        )  # the planted plugin does run when Hermes is told
        marker.unlink()
        proc = _run(home, user, f"HERMES_BUNDLED_PLUGINS={plugins}\n")
        assert not marker.exists(), "a .env line made the checker run a plugin"
    assert proc.returncode == admin.EXIT_OK, proc.stdout + proc.stderr[-2000:]


OVERLAY_RUNNER = f"""\
import json, os, sys
sys.path.insert(0, {str(PLUGIN_ROOT)!r})
from glasses import config as gcfg
from glasses import hermes_facts as hf
home = os.environ["HERMES_HOME"]
seen = {{}}
def spy(label, real):
    def wrapper(*args, **kwargs):
        seen[label] = {{n: os.environ.get(n) for n in sys.argv[1:]}}
        return real(*args, **kwargs)
    return wrapper
for name in ("read_gateway_config", "read_profile_config"):
    setattr(hf, name, spy(name, getattr(hf, name)))
with hf.quarantined_import():
    import hermes_cli.tools_config as tc
tc._get_platform_tools = spy("_get_platform_tools", tc._get_platform_tools)
hf.hermes_facts(gcfg.load({{"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant"]}}))
print(json.dumps(seen))
"""
DOTENV_NAMES = {
    "ONLY_IN_DOTENV": "yes",
    "PATH": "/dotenv/bin",
    "LD_PRELOAD": "/dotenv/evil.so",
    "LD_LIBRARY_PATH": "/dotenv/lib",
    "PYTHONPATH": "/dotenv/py",
    "PYTHONSTARTUP": "/dotenv/start.py",
    "HOME": "/dotenv/home",
    "TMPDIR": "/dotenv/tmp",
    "HERMES_BUNDLED_PLUGINS": "/dotenv/plugins",
    "HERMES_ENABLE_PROJECT_PLUGINS": "1",
    "HERMES_MANAGED_DIR": "/dotenv/managed",
    "HERMES_PLUGINS_DEBUG": "1",
}


def test_the_dotenv_overlay_wraps_the_pure_reads_only_and_never_a_code_loading_name():
    with _scratch() as tmp:
        (Path(tmp) / "home").mkdir()
        (Path(tmp) / "home" / ".env").write_text(
            "".join(f"{k}={v}\n" for k, v in DOTENV_NAMES.items()), encoding="utf-8"
        )
        proc = _hermes_run(OVERLAY_RUNNER, tmp, *DOTENV_NAMES)
    assert proc.returncode == 0, proc.stderr[-2000:]
    seen = json.loads(proc.stdout.strip().splitlines()[-1])
    for label in ("read_gateway_config", "read_profile_config"):
        assert seen[label]["ONLY_IN_DOTENV"] == "yes", seen  # the pure reads see the file ...
        code_loading = {
            k: v for k, v in seen[label].items() if k != "ONLY_IN_DOTENV" and v in set(DOTENV_NAMES.values())
        }
        assert code_loading == {}, seen  # ... but none of the names that load code
    assert seen["_get_platform_tools"]["ONLY_IN_DOTENV"] is None, seen  # plugin discovery sees no .env at all


@pytest.mark.parametrize(
    "name", [n for n in DOTENV_NAMES if n != "ONLY_IN_DOTENV"] + ["DYLD_INSERT_LIBRARIES", "XDG_CONFIG_HOME"]
)
def test_names_that_load_code_or_move_directories_are_not_overlaid(name):
    assert not he.overlay_name_ok(name, "x")
    assert he.overlay_name_ok("HERMES_LOCAL_STT_LANGUAGE", "en") and he.overlay_name_ok("MY_API_KEY", "x")


# -- the .env file itself: regular, capped -----------------------------------------------------------------------


def _fake_loader_module(calls: list):
    def load_env_file(path):
        calls.append(path)
        return {"LANG_HINT": "x"}

    return SimpleNamespace(load_env_file=load_env_file)


def _read_with_fake(home: Path) -> tuple[dict, list]:
    calls: list = []
    with pytest.MonkeyPatch.context() as patch:  # undone before conftest checks that no Hermes module leaked
        patch.setitem(sys.modules, "agent", SimpleNamespace())
        patch.setitem(sys.modules, "agent.secret_scope", _fake_loader_module(calls))
        return he.read_env_file(home), calls


def test_a_regular_dotenv_is_read(tmp_path):
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    assert _read_with_fake(tmp_path) == ({"LANG_HINT": "x"}, [tmp_path / ".env"])


def test_a_fifo_a_symlink_a_directory_and_a_huge_file_are_never_opened(tmp_path):
    kinds = {}
    for kind in ("fifo", "symlink", "directory", "huge", "missing"):
        home = tmp_path / kind
        home.mkdir()
        path = home / ".env"
        if kind == "fifo":
            os.mkfifo(path)
        elif kind == "symlink":
            (tmp_path / "elsewhere").write_text("A=1\n", encoding="utf-8")
            path.symlink_to(tmp_path / "elsewhere")
        elif kind == "directory":
            path.mkdir()
        elif kind == "huge":
            with path.open("wb") as handle:
                handle.truncate(he.MAX_ENV_FILE_BYTES + 1)
        kinds[kind] = _read_with_fake(home)
    assert kinds == {kind: ({}, []) for kind in kinds}


def test_a_file_of_exactly_the_cap_is_read(tmp_path):
    with (tmp_path / ".env").open("wb") as handle:
        handle.truncate(he.MAX_ENV_FILE_BYTES)
    assert _read_with_fake(tmp_path)[1] == [tmp_path / ".env"]


def _hints_fr(config):
    config["gateway"]["platforms"]["glasses"]["extra"]["meeting"] = {"language_hints": ["fr"]}


def test_real_live_a_fifo_a_symlinked_and_a_huge_dotenv_are_neither_read_nor_waited_for():
    line = "HERMES_LOCAL_STT_LANGUAGE=en\n"
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        _edit(home, _hints_fr)
        control = _run(home, user, line)
        assert 'stt.language is "en"' in control.stdout, control.stdout  # the line has an effect where it is honoured
        (home / ".env").unlink()
        os.mkfifo(home / ".env")
        fifo = _real_live(home, user, timeout=90)  # a blocking open would end in TimeoutExpired
        (home / ".env").unlink()
        (Path(tmp) / "elsewhere.env").write_text(line, encoding="utf-8")
        (home / ".env").symlink_to(Path(tmp) / "elsewhere.env")
        link = _real_live(home, user)
        (home / ".env").unlink()
        (home / ".env").write_text("#" * (he.MAX_ENV_FILE_BYTES + 1) + "\n" + line, encoding="utf-8")
        huge = _real_live(home, user)
    for proc in (fifo, link, huge):  # not read, not followed, and the allow-all gate says it could not look
        assert proc.returncode == admin.EXIT_ERRORS and proc.stderr == "", proc.stdout + proc.stderr[-2000:]
        assert proc.stdout.splitlines() == [UNSAFE, "config check: FAILED (1 error(s), 0 warning(s))"], proc.stdout


def test_real_live_a_routed_profile_sees_the_launch_dotenv_and_its_own_wins():
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        _edit(home / "profiles" / "work", lambda c: c["approvals"].__setitem__("timeout", "${LAUNCH_ONLY}"))
        only_launch = _run(home, user, "LAUNCH_ONLY=0\n")
        own = _run(home, user, "LAUNCH_ONLY=0\n", "LAUNCH_ONLY=77\n")
    assert only_launch.returncode == admin.EXIT_ERRORS, only_launch.stdout
    assert "error: profile work: approvals.timeout resolves to 0" in only_launch.stdout, only_launch.stdout
    assert own.returncode == admin.EXIT_OK, own.stdout


class _InterruptedAfterOneName(Mapping[str, str]):
    """Yields every name on the first pass (``environ_with`` saves them) and breaks off after one on the second
    (``os.environ.update`` is part way through)."""

    def __init__(self):
        self.passes = 0

    def __getitem__(self, key):
        return "over"

    def __len__(self):
        return 2

    def __iter__(self):
        self.passes += 1
        yield "T123_FIRST"
        if self.passes > 1:
            raise KeyboardInterrupt
        yield "T123_SECOND"


def test_environ_with_puts_the_environment_back_when_the_update_itself_is_interrupted(monkeypatch):
    monkeypatch.delenv("T123_FIRST", raising=False)
    monkeypatch.delenv("T123_SECOND", raising=False)
    before = dict(os.environ)
    with pytest.raises(KeyboardInterrupt), he.environ_with(_InterruptedAfterOneName()):
        pytest.fail("the block must not run")
    assert dict(os.environ) == before
