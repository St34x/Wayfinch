# SPDX-License-Identifier: Apache-2.0
"""A missing ``jsonschema`` is one clear error naming the module (gateway: one log line;
``config check``: message + exit 2), never a traceback. No Hermes import (conftest.py)."""

from __future__ import annotations

import io
import logging
import subprocess
import sys

import pytest

from glasses import admin
from glasses import config as gcfg

from conftest import PLUGIN_ROOT

EXAMPLE = PLUGIN_ROOT.parent / "deploy" / "examples" / "config.example.yaml"
NEEDLE = "the Python module 'jsonschema' is not installed"


@pytest.fixture
def no_jsonschema(monkeypatch):
    gcfg._validator.cache_clear()
    monkeypatch.setitem(sys.modules, "jsonschema", None)  # import jsonschema -> ImportError
    yield
    gcfg._validator.cache_clear()


def test_check_raises_dependency_missing_naming_the_module(no_jsonschema):
    with pytest.raises(gcfg.DependencyMissing) as exc:
        gcfg.check({})
    assert NEEDLE in str(exc.value) and "Hermes venv" in str(exc.value)


def test_validate_config_logs_one_line_and_refuses(no_jsonschema, caplog):
    caplog.set_level(logging.DEBUG, logger="glasses")
    assert gcfg.validate_config({"extra": {}}) is False
    records = [r for r in caplog.records if r.name.startswith("glasses")]
    assert len(records) == 1 and records[0].levelno == logging.ERROR, caplog.text
    assert NEEDLE in records[0].getMessage() and records[0].exc_info is None


def test_config_check_exit_2_with_one_message(no_jsonschema):
    out = io.StringIO()
    assert admin.main(["config", "check", "--config", str(EXAMPLE)], {}, out) == admin.EXIT_UNREADABLE
    assert out.getvalue().splitlines() == [f"error: {gcfg.DependencyMissing('jsonschema')}"]


def test_real_cli_without_jsonschema_has_no_traceback(tmp_path):
    runner = tmp_path / "run_without_jsonschema.py"
    runner.write_text(
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(PLUGIN_ROOT)!r})\n"
        "sys.modules['jsonschema'] = None\n"
        "sys.argv = ['glasses.admin', 'config', 'check', '--config', sys.argv[1]]\n"
        "runpy.run_module('glasses.admin', run_name='__main__', alter_sys=True)\n",
        encoding="utf-8",
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, str(runner), str(EXAMPLE)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == admin.EXIT_UNREADABLE, proc.stdout + proc.stderr
    assert proc.stdout.splitlines() == [f"error: {gcfg.DependencyMissing('jsonschema')}"]
    assert proc.stderr == ""
