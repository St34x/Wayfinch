# SPDX-License-Identifier: Apache-2.0
"""Unit tests of tools/quality/check_core_purity.py (T1.100): the import allowlist, I/O without an import statement
(builtins, clock reads, console streams), ratchet hygiene and configuration errors. Scratch-tree runs of the whole
gate are in test_imports.py and test_imports_bypass.py.
"""

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_core_purity.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_core_purity_ast_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cp = _load()
Scan = Callable[..., list[str]]


# --- check_core_purity: allowlist and I/O without imports ----------------------------------------------------------


@pytest.fixture
def scan(tmp_path: Path) -> Scan:
    """Scan source as `glasses.core` with core siblings `glasses.open_lane` and package `glasses.pkg`."""
    pkg = tmp_path / "glasses"
    (pkg / "pkg").mkdir(parents=True)
    (pkg / "generated").mkdir()
    for name in ("__init__.py", "adapter.py", "open_lane.py", "config.py", "pkg/__init__.py", "pkg/inner.py"):
        (pkg / name).write_text("", encoding="utf-8")
    (pkg / "generated/__init__.py").write_text("", encoding="utf-8")
    (pkg / "generated/limits.py").write_text("", encoding="utf-8")
    core = frozenset({"glasses.core", "glasses.open_lane", "glasses.pkg"})
    policy = cp.Policy(core, frozenset({("glasses.core", "glasses.config")}), tmp_path)

    def run(source: str, module: str = "core.py") -> list[str]:
        findings: list[str] = cp.scan_source(source, pkg / module, policy)
        return findings

    return run


AST_BAD = [
    ("with open('x') as f:\n    pass\n", ["builtin open()"]),
    ("_o = open\n", ["builtin open()"]),
    ("m = __import__('socket')\n", ["builtin __import__()"]),
    ("print('x')\n", ["builtin print()"]),
    ("exec('1')\n", ["builtin exec()"]),
    ("eval('1')\n", ["builtin eval()"]),
    ("x = input()\n", ["builtin input()"]),
    ("def f() -> None:\n    breakpoint()\n", ["builtin breakpoint()"]),
    ("import io\nio.open('x')\n", ["import of io "]),
    ("from io import open as _open\n", ["import of io "]),
    ("import codecs\n", ["import of codecs "]),
    ("import sys\nsys.stdout.write('x')\n", ["import of sys ", "console stream .stdout"]),
    ("def f(s: object) -> None:\n    s.stderr.write('x')\n", ["console stream .stderr"]),
    ("from datetime import datetime\nT = datetime.now()\n", ["clock read .now"]),
    ("import datetime\nT = datetime.datetime.utcnow()\n", ["clock read .utcnow"]),
    ("from datetime import date\nT = date.today()\n", ["clock read .today"]),
    ("from datetime import datetime as D\nC = D.now\n", ["clock read .now"]),
    ("def f(time: object) -> object:\n    return time.monotonic()\n", ["clock read .monotonic"]),
    ("import time\n", ["import of time "]),
    ("import requests\n", ["import of requests "]),
    ("from requests import get\n", ["import of requests "]),
    ("import logging\n", ["import of logging "]),
    ("import uuid\n", ["import of uuid "]),
    ("import random\n", ["import of random "]),
    ("from logging import handlers\n", ["import of logging.handlers "]),
    ("from json import tool\n", ["import of json.tool "]),
    ("def f() -> None:\n    import xml.etree\n", ["import of xml.etree "]),
    ("import urllib.request\n", ["import of urllib.request "]),
    ("from urllib import request\n", ["import of urllib.request "]),
    ("from urllib.request import urlopen\n", ["import of urllib.request "]),
    ("import urllib\n", ["import of urllib "]),
    ("from urllib import parse, error\n", ["import of urllib.error "]),
    ("from . import adapter\n", ["import of glasses.adapter "]),
    ("from . import not_a_module\n", ["import of glasses "]),
    ("from .adapter import X\n", ["import of glasses.adapter "]),
    ("import glasses.adapter\n", ["import of glasses.adapter "]),
    ("from ... import x\n", ["import of ... "]),
    # a submodule reached as an attribute of a bound package must pass the allowlist too
    ("import urllib.parse\nX = urllib.request.urlopen\n", ["module attribute urllib.request "]),
    ("import json\nX = json.tool\n", ["module attribute json.tool "]),
    ("import glasses.open_lane\nX = glasses.adapter\n", ["module attribute glasses.adapter "]),
    # string-keyed access to builtins and the clock
    ("X = globals()['__builtins__']['open']\n", ["builtin globals()"]),
    ("X = vars()\n", ["builtin vars()"]),
    ("from datetime import datetime\nX = getattr(datetime, 'now')()\n", ["clock read .now"]),
    ("def f(b: object) -> object:\n    return getattr(b, 'open')\n", ["builtin open() via getattr"]),
    # `time` is the clock module unless `from datetime import time` binds it
    ("from datetime import time as t\nX = time.time()\n", ["clock read .time"]),
]


@pytest.mark.parametrize(("source", "messages"), AST_BAD)
def test_scan_source_flags(scan: Scan, source: str, messages: list[str]) -> None:
    findings = scan(source)
    assert len(findings) == len(messages), findings
    for message in messages:
        assert any(message in finding for finding in findings), (message, findings)
    assert all(":" in finding and "core.py:" in finding for finding in findings)


AST_OK = [
    "from __future__ import annotations\n",
    "from collections.abc import Mapping\nfrom dataclasses import dataclass\nfrom typing import Any\n",
    "from urllib.parse import urlsplit\n",
    "import urllib.parse\n",
    "from urllib import parse\n",
    "import re\nP = re.compile('x')\n",
    "import json\nD = json.loads('{}')\n",
    "from datetime import date, datetime, timedelta\nD = date(2024, 2, 29) + timedelta(days=1)\n",
    "from datetime import datetime\ndef f(now: datetime) -> int:\n    return now.year\n",
    "def f(data: dict[str, str]) -> str:\n    return data.get('open', '')\n",
    "from . import open_lane\n",
    "from .open_lane import X\n",
    "import glasses.open_lane\n",
    "from .pkg.inner import X\n",
    "from .generated.limits import MAX\n",
    "from glasses.generated import limits\n",
    "from .config import NAME\n",  # the ratchet entry glasses.core -> glasses.config
    "import urllib.parse\nX = urllib.parse.quote('a b')\n",
    "import collections.abc\nX = collections.abc.Mapping\n",
    "from . import pkg\nX = pkg.inner\n",
    "import urllib.parse as up\nX = up.quote\n",
    "from datetime import time\nNOON = time(12, 0)\nLAST = time.max\n",
    "from . import open_lane\nX = open_lane.thing\n",
    "def f(d: object) -> object:\n    return getattr(d, 'year')\n",
]


@pytest.mark.parametrize("source", AST_OK)
def test_scan_source_accepts(scan: Scan, source: str) -> None:
    assert scan(source) == []


def test_ratchet_entry_is_per_importer(scan: Scan) -> None:
    findings = scan("from ..config import NAME\n", "pkg/inner.py")
    assert len(findings) == 1 and "import of glasses.config " in findings[0]


def test_scan_never_imports_the_scanned_module(scan: Scan, capsys: pytest.CaptureFixture[str]) -> None:
    findings = scan("from this import s\n")  # importing `this` prints the Zen of Python
    assert len(findings) == 1 and "import of this " in findings[0]
    assert capsys.readouterr().out == ""


def test_relative_imports_resolve_in_a_core_package(scan: Scan) -> None:
    assert scan("from .inner import X\n", "pkg/__init__.py") == []
    assert scan("from . import inner\n", "pkg/inner.py") == []
    findings = scan("from .. import adapter\n", "pkg/inner.py")
    assert len(findings) == 1 and "import of glasses.adapter " in findings[0]


def test_stdlib_allowlist_is_pure_stdlib_with_reasons() -> None:
    for name, reason in cp.STDLIB_ALLOWLIST.items():
        assert reason.strip(), name
        assert name.split(".")[0] in sys.stdlib_module_names, name
        assert importlib.util.find_spec(name) is not None, name
    for io_module in ("io", "codecs", "sys", "logging", "uuid", "time", "os", "random", "secrets", "urllib"):
        assert io_module not in cp.STDLIB_ALLOWLIST


def test_core_package_scans_every_file(tmp_path: Path) -> None:
    pkg = tmp_path / "glasses/core"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "inner.py").write_text("print(1)\n", encoding="utf-8")
    assert [p.name for p in cp.module_files(tmp_path, "glasses.core")] == ["__init__.py", "inner.py"]


def test_missing_core_module_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(cp.PurityError, match=r"core module glasses\.nope not found"):
        cp.module_files(tmp_path, "glasses.nope")


# --- ratchet hygiene of ignore_imports and config errors -------------------------------------------------------------

HEAD = "[importlinter:contract:core-purity]\nsource_modules =\n    a.b\n"


def test_ignore_entry_under_id_comment_passes() -> None:
    assert cp.ignore_entries_without_id(HEAD + "ignore_imports =\n    # T1.111 why\n    a.b -> a.c\n") == []


@pytest.mark.parametrize(
    "block",
    [
        "ignore_imports =\n    a.b -> a.c\n",
        "ignore_imports =\n    # no id here\n    a.b -> a.c\n",
        "ignore_imports =\n    # T1.111\n    a.b -> a.c\n    # reason only\n    a.b -> a.d\n",
        "ignore_imports = a.b -> a.c\n",
    ],
    ids=["no-comment", "comment-without-id", "second-block-without-id", "inline-value"],
)
def test_ignore_entry_without_id_fails(block: str) -> None:
    findings = cp.ignore_entries_without_id(HEAD + block)
    assert len(findings) == 1, findings
    assert "without a '# T<id>' line above" in findings[0]


def test_ignore_block_of_every_contract_is_read() -> None:
    other = "\n[importlinter:contract:layers]\nignore_imports =\n    x.y -> x.z\n"
    findings = cp.ignore_entries_without_id(HEAD + other)
    assert len(findings) == 1 and findings[0].endswith("x.y -> x.z")
    assert cp.ignore_entries_without_id(HEAD + other.replace("    x.y", "    # T1.99 why\n    x.y")) == []
    assert cp.ignore_entries_without_id(HEAD + "\n[importlinter]\nignore_imports =\n    x.y -> x.z\n") == []


def test_ratchet_entries_parse_pairs_and_skip_comments() -> None:
    text = HEAD + "ignore_imports =\n    # T1.111 why\n    a.b -> a.c\n    a.b -> a.*\n"
    assert cp.ratchet_entries(text) == frozenset({("a.b", "a.c"), ("a.b", "a.*")})


@pytest.mark.parametrize(
    "text",
    [
        "[importlinter]\nroot_package = glasses\n",
        HEAD.replace("    a.b\n", ""),
        HEAD.replace("    a.b\n", "    # only a comment\n"),
        "not ini\n",
    ],
    ids=["no-contract", "empty-core-list", "comment-only-core-list", "unreadable"],
)
def test_config_errors_exit_2(tmp_path: Path, text: str) -> None:
    config = tmp_path / ".importlinter"
    config.write_text(text, encoding="utf-8")
    assert cp.main(["--config", str(config), "--src", str(tmp_path)]) == 2


def test_missing_config_exits_2(tmp_path: Path) -> None:
    assert cp.main(["--config", str(tmp_path / "nope"), "--src", str(tmp_path)]) == 2
