# SPDX-License-Identifier: Apache-2.0
"""Core purity gate: bypass attempts on the allowlist + I/O scan of check_core_purity.py.

Every case scans one planted core module in a scratch package.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_core_purity.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_core_purity_allowlist_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cp = _load()


def _scan(tmp_path: Path, source: str) -> list[str]:
    """Findings for `source` as the core module `glasses.core` (siblings: `glasses.adapter`, `glasses.config`)."""
    package = tmp_path / "glasses"
    package.mkdir()
    for name in ("__init__.py", "adapter.py", "config.py"):
        (package / name).write_text("", encoding="utf-8")
    policy = cp.Policy(frozenset({"glasses.core"}), frozenset(), tmp_path)
    findings: list[str] = cp.scan_source(source, package / "core.py", policy)
    return findings


@pytest.mark.parametrize(
    "source",
    [
        "import datetime as d\n\n\ndef f() -> object:\n    return d.datetime.now()\n",
        "from datetime import datetime as D\n\nX = D.utcnow\n",
        "from . import adapter\n",
        "from . import config\n",
        "from os import *\n",
        "from io import *\n",
        "import builtins\n\nX = getattr(builtins, 'open')\n",
        "import importlib\n",
        "X = __import__('socket')\n",
        "X = \uff4f\uff50\uff45\uff4e\n",  # full-width letters: NFKC makes them the builtin name
        "from urllib import request\n",
        "import urllib.request\n",
        "import json.tool\n",
    ],
    ids=[
        "aliased-now",
        "aliased-utcnow",
        "rel-adapter",
        "rel-config-without-ratchet",
        "star-os",
        "star-io",
        "builtins-getattr",
        "importlib",
        "dunder-import",
        "fullwidth-open",
        "urllib-request-from",
        "urllib-request",
        "json-submodule",
    ],
)
def test_planted_io_is_flagged(tmp_path: Path, source: str) -> None:
    assert _scan(tmp_path, source) != []


@pytest.mark.parametrize(
    "source",
    [
        "def f() -> object:\n    return globals()['__builtins__']['open']('/etc/passwd')\n",
        "import datetime\n\n\ndef f() -> object:\n    return getattr(datetime.datetime, 'now')()\n",
    ],
    ids=["builtins-via-globals-string", "clock-via-getattr-string"],
)
def test_string_keyed_io_is_flagged(tmp_path: Path, source: str) -> None:
    assert _scan(tmp_path, source) != []


def test_the_datetime_time_class_is_not_a_clock_read(tmp_path: Path) -> None:
    assert _scan(tmp_path, "from datetime import time\n\nNOON = time(12, 0)\nLAST = time.max\n") == []


def test_pure_stdlib_use_passes(tmp_path: Path) -> None:
    source = "import datetime\nimport json\nfrom urllib.parse import quote\n\n"
    source += "X = datetime.date(2024, 2, 29), json.dumps(1)\n"
    assert _scan(tmp_path, source + "Y = quote('a b')\n") == []
