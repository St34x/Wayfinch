# SPDX-License-Identifier: Apache-2.0
"""Tests of the import-boundary gate (T1.100): the `.importlinter` contract and tools/quality/check_core_purity.py.

Planted violations run the real lint-imports of this venv / the real checker on a scratch copy of
`hermes-plugin/glasses` plus the real `.importlinter`, so they prove the committed contract, not a test double. Every
test plants its own state (its own core module, helpers and `# T9.99` ratchet entries; see imports_scratch.py), so
changes of the real core list or ratchet entries by later tasks do not break them. Findings recorded by T1.100:
import-linter sees stdlib modules (as external packages, with `include_external_packages = True`), also through
other modules and inside functions; a listed core module that does not exist fails the run; an ignored import
that no longer exists fails the run; subpackages of external packages (`urllib.request`) are not accepted.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imports_scratch as sc

SCRIPT = Path(__file__).resolve().parents[1] / "check_core_purity.py"
TOP = sc.TOP
CORE = sc.CORE
CORE_FILE = sc.CORE_FILE


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_core_purity_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cp = _load()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A scratch copy of the plugin package and the real contract, plus the planted core module."""
    return sc.make_tree(tmp_path)


def _cli(root: Path) -> int:
    return int(cp.main(["--config", str(root / ".importlinter"), "--src", str(root / "hermes-plugin")]))


# --- the real tree ------------------------------------------------------------------------------------------------


def test_real_tree_contract_is_kept() -> None:
    done = sc.lint(TOP)
    assert done.returncode == 0, done.stdout + done.stderr
    assert " kept, 0 broken." in done.stdout


def test_real_tree_purity_is_clean() -> None:
    assert cp.check(TOP / ".importlinter", TOP / "hermes-plugin") == []
    assert cp.main([]) == 0


def test_real_core_list_exists() -> None:
    modules = cp.core_modules((TOP / ".importlinter").read_text(encoding="utf-8"))
    assert modules
    for name in modules:
        assert cp.module_files(TOP / "hermes-plugin", name)


def test_unplanted_scratch_core_passes_both_checks(tree: Path) -> None:
    done = sc.lint(tree)
    assert done.returncode == 0, done.stdout
    assert _cli(tree) == 0


# --- planted violations: lint-imports (transitive contract) must fail ---------------------------------------------

PLANTED = [
    ("import aiohttp\n", "aiohttp"),
    ("import socket\n", "socket"),
    ("import subprocess\n", "subprocess"),
    ("from os import path\n", "os"),
    ("import os.path\n", "os"),
    ("from pathlib import Path\n", "pathlib"),
    ("import ssl\n", "ssl"),
    ("import asyncio\n", "asyncio"),
    ("from http import client\n", "http"),
    ("import shutil\n", "shutil"),
    ("import sqlite3\n", "sqlite3"),
    ("import threading\n", "threading"),
    ("import importlib\n", "importlib"),
    ("import io\n", "io"),
    ("import codecs\n", "codecs"),
    ("import sys\n", "sys"),
    ("def _lazy() -> object:\n    import time\n\n    return time\n", "time"),
    ("from tools.clarify_gateway import resolve_clarify_timeout\n", "tools"),
    ("from gateway.config import Platform\n", "gateway"),
    ("import hermes_cli\n", "hermes_cli"),
    ("from . import adapter\n", "glasses.adapter"),
    ("from . import gate\n", "glasses.gate"),
    ("from .registry import Registry\n", "glasses.registry"),
]


@pytest.mark.parametrize(("source", "target"), PLANTED, ids=[target for _, target in PLANTED])
def test_planted_import_in_core_fails(tree: Path, source: str, target: str) -> None:
    sc.plant(tree, source)
    done = sc.lint(tree)
    assert done.returncode == 1, done.stdout
    assert f"{CORE} is not allowed to import {target}:" in done.stdout
    assert _cli(tree) == 1  # the allowlist rejects it as well


def test_hermes_tools_package_is_forbidden_despite_our_tools_dir(tree: Path) -> None:
    # Our repo has a top-level tools/ directory (not a package); the contract still names Hermes' `tools`.
    (tree / "tools/quality").mkdir(parents=True)
    sc.plant(tree, "import tools.quality\n")
    done = sc.lint(tree)
    assert done.returncode == 1
    assert f"{CORE} is not allowed to import tools:" in done.stdout


def test_io_through_a_non_core_module_fails(tree: Path) -> None:
    sc.write_module(tree, "_planted_helper", "import socket\n")
    sc.plant(tree, "from . import _planted_helper\n")
    done = sc.lint(tree)
    assert done.returncode == 1
    assert f"{CORE} is not allowed to import socket:" in done.stdout
    assert "glasses._planted_helper -> socket" in done.stdout
    assert _cli(tree) == 1  # a non-core module is not on the allowlist


# --- ratchet entries (own state: helper module + `# T9.99` entry) ------------------------------------------------


def test_ratchet_entry_admits_one_import_in_both_checks(tree: Path) -> None:
    sc.write_module(tree, "_planted_helper", "import os\n")
    sc.plant(tree, "from ._planted_helper import os\n")
    sc.add_ignore(tree / ".importlinter", f"{CORE} -> glasses._planted_helper")
    done = sc.lint(tree)
    assert done.returncode == 0, done.stdout
    assert _cli(tree) == 0


def test_without_the_ratchet_entry_helper_io_is_reported(tree: Path) -> None:
    sc.write_module(tree, "_planted_helper", "import os\n")
    sc.plant(tree, "from ._planted_helper import os\n")
    done = sc.lint(tree)
    assert done.returncode == 1
    assert f"{CORE} is not allowed to import os:" in done.stdout
    assert "glasses._planted_helper -> os" in done.stdout
    assert _cli(tree) == 1


def test_stale_ratchet_entry_fails(tree: Path) -> None:
    sc.write_module(tree, "_planted_helper", "import os\n")
    sc.add_ignore(tree / ".importlinter", f"{CORE} -> glasses._planted_helper")
    done = sc.lint(tree)
    assert done.returncode == 1
    assert f"No matches for ignored import {CORE} -> glasses._planted_helper." in done.stdout


def test_ratchet_entry_of_another_module_admits_nothing(tree: Path) -> None:
    sc.write_module(tree, "_planted_helper", "X = 1\n")
    sc.write_module(tree, "_planted_other", "from ._planted_helper import X\n")
    sc.add_ignore(tree / ".importlinter", "glasses._planted_other -> glasses._planted_helper")
    sc.plant(tree, "from ._planted_helper import X as Y\n")
    assert _cli(tree) == 1  # the entry names another importer


def test_listed_core_module_that_does_not_exist_fails(tree: Path) -> None:
    sc.add_values(tree / ".importlinter", "source_modules", ["glasses._no_such_module"])
    done = sc.lint(tree)
    assert done.returncode == 1
    assert "Module 'glasses._no_such_module' does not exist." in done.stdout
    assert _cli(tree) == 2


def test_forbidden_adapter_that_does_not_exist_yet_is_accepted(tree: Path) -> None:
    sc.add_values(tree / ".importlinter", "forbidden_modules", ["glasses._future_adapter"])
    assert sc.lint(tree).returncode == 0


def test_adapter_created_later_is_covered(tree: Path) -> None:
    sc.add_values(tree / ".importlinter", "forbidden_modules", ["glasses._future_adapter"])
    sc.write_module(tree, "_future_adapter", "X = 1\n")
    sc.plant(tree, "from ._future_adapter import X as Y\n")
    done = sc.lint(tree)
    assert done.returncode == 1
    assert f"{CORE} is not allowed to import glasses._future_adapter:" in done.stdout


def test_real_forbidden_adapters_are_listed() -> None:
    text = (TOP / ".importlinter").read_text(encoding="utf-8")
    for adapter in ("glasses.adapter", "glasses.gate", "glasses.registry", "glasses.server", "glasses.telegram"):
        assert f"    {adapter}\n" in text


def test_urllib_parse_in_core_passes_both_checks(tree: Path) -> None:
    sc.plant(tree, "from urllib.parse import urlsplit\n")
    assert sc.lint(tree).returncode == 0
    assert _cli(tree) == 0


# --- planted I/O without a graph edge and ratchet hygiene through the CLI --------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import io\n_F = io.open\n",
        "import codecs\n",
        "import sys\n",
        "def _w() -> None:\n    import sys\n    sys.stdout.write('x')\n",
        "from datetime import datetime\n_T = datetime.now\n",
        "import requests\n",
        "import uuid\n",
    ],
)
def test_planted_io_in_core_fails_cli(tree: Path, source: str) -> None:
    sc.plant(tree, source)
    assert _cli(tree) == 1


def test_planted_builtin_io_in_core_fails_cli(tree: Path) -> None:
    sc.plant(tree, "def _read() -> str:\n    return open('x').read()\n")
    assert _cli(tree) == 1
    assert sc.lint(tree).returncode == 0  # the import graph alone does not see it: the AST part is needed


def test_urllib_request_behind_urllib_parse_fails_cli(tree: Path) -> None:
    # `import urllib.parse` binds `urllib`; `urllib.request` works at runtime once anything else imported it
    sc.plant(tree, "import urllib.parse\n\n\ndef _leak() -> object:\n    return urllib.request.urlopen('http://x')\n")
    assert sc.lint(tree).returncode == 0  # the import graph sees only urllib.parse
    assert _cli(tree) == 1


def test_ignore_entry_without_id_fails_cli(tree: Path) -> None:
    sc.write_module(tree, "_planted_helper", "X = 1\n")
    sc.plant(tree, "from ._planted_helper import X as Y\n")
    sc.add_ignore(tree / ".importlinter", f"{CORE} -> glasses._planted_helper", comment="# no task id")
    assert sc.lint(tree).returncode == 0
    assert _cli(tree) == 1
