# SPDX-License-Identifier: Apache-2.0
"""T1.150: ``glasses.config_values`` is a core module (both purity checks and the 90 % coverage gate read the
``core-purity`` list of ``.importlinter``), its absolute-path rule is a pure check with unchanged behaviour, and
two ``config.py`` riders: the ``validate_config`` catch-all honours ``log_level``, and a ``jsonschema`` without
``Draft202012Validator`` is ``DependencyMissing``. No Hermes import (conftest.py)."""

from __future__ import annotations

import ast
import configparser
import logging
import sys
import types

import pytest

from glasses import config as gcfg
from glasses import config_values

from conftest import PLUGIN_DIR, PLUGIN_ROOT

IMPORTLINTER = PLUGIN_ROOT.parent / ".importlinter"


def test_config_values_is_in_the_core_list():
    parser = configparser.ConfigParser()
    parser.read(IMPORTLINTER, encoding="utf-8")
    core = parser["importlinter:contract:core-purity"]["source_modules"].split()
    assert "glasses.config_values" in core


def test_config_values_imports_no_os():
    tree = ast.parse((PLUGIN_DIR / "config_values.py").read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not {n for n in names if n == "os" or n.startswith("os.") or n == "posixpath"}, names


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        ("", True),
        ("/", True),
        ("/abs/state", True),
        ("//double", True),
        ("/etc/nginx/glasses", True),
        ("rel/state", False),
        ("./rel", False),
        ("~/x", False),
        ("\\\\win", False),
        ("C:/x", False),
        (" /lead", False),
        ("/tail ", False),
        ("/a\tb", False),
        (b"/bytes", False),
        (0, False),
    ],
)
def test_empty_or_abs_path_behaviour_is_pinned(value, expected):
    assert config_values._empty_or_abs_path(value) is expected


def test_validate_config_catch_all_honours_log_level(monkeypatch, caplog):
    monkeypatch.setattr(gcfg, "check", lambda _extra: (_ for _ in ()).throw(RuntimeError("bug")))
    caplog.set_level(logging.DEBUG, logger="glasses")
    assert gcfg.validate_config({"extra": {}}, log_level=logging.DEBUG) is False
    records = [r for r in caplog.records if "validation error" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.DEBUG], caplog.text
    caplog.clear()
    assert gcfg.validate_config({"extra": {}}) is False
    records = [r for r in caplog.records if "validation error" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.WARNING], caplog.text


def test_jsonschema_without_draft_2020_12_is_dependency_missing(monkeypatch):
    gcfg._validator.cache_clear()
    monkeypatch.setitem(sys.modules, "jsonschema", types.ModuleType("jsonschema"))
    try:
        with pytest.raises(gcfg.DependencyMissing) as exc:
            gcfg.check({})
        assert "jsonschema" in str(exc.value)
    finally:
        gcfg._validator.cache_clear()
