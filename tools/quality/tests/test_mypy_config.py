# SPDX-License-Identifier: Apache-2.0
"""Tests of tools/quality/check_mypy_config.py (T1.99): every way to weaken mypy --strict fails the gate.

Planted bypasses: extra keys or sections in mypy.ini, new or grown baseline blocks, and inline suppressions in the
checked files. Planted suppression texts are split so that this file does not trip the scan itself.
"""

import ast
import re
import shutil
from pathlib import Path

import pytest

from test_mypy_baseline import ROOT, cmc, copy_tree, run_mypy

INI_TEXT = (ROOT / "mypy.ini").read_text(encoding="utf-8")
GLOBAL_ONLY = "[mypy]\n" + "".join(f"{key} = {value}\n" for key, value in cmc.GLOBAL.items())
BOUNDARY = f"\n[{cmc.HERMES}]\nignore_missing_imports = True\n\n[{cmc.ADAPTER}]\ndisallow_subclassing_any = False\n"
GOOD = GLOBAL_ONLY + BOUNDARY
RATCHET = {"T1.5": ("a", "b.*"), "T1.06a": ("c",)}
BLOCKS = (
    "\n# T1.5\n[mypy-a]\nignore_errors = True\n[mypy-b.*]\nignore_errors = True\n"
    "\n# T1.06a\n[mypy-c]\nignore_errors = True\n"
)
MYPY_COMMENT = "# my" + "py: ignore-errors\n"
TYPE_IGNORE = "# type: ig" + "nore\n"
NO_CHECK = "from typing import no_type_" + "check\n\n\n@no_type_" + "check\ndef planted(value):\n    return value\n"
HERMES_MSG = f"[{cmc.HERMES}] must hold exactly"
ADAPTER_MSG = f"[{cmc.ADAPTER}] must hold exactly"


def _sub(old: str, new: str) -> str:
    assert INI_TEXT.count(old) == 1, old
    return INI_TEXT.replace(old, new)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (INI_TEXT + "\n[mypy-glasses.hermes_merge]\ndisable_error_code = no-untyped-def\n", "unexpected section"),
        (_sub("strict = True\n", "strict = True\ndisable_error_code = no-untyped-def\n"), "[mypy] must hold"),
        (_sub("strict = True\n", "strict = False\n"), "[mypy] must hold"),
        (_sub("strict = True\n", "strict: False\n"), "[mypy] must hold"),
        (_sub("strict = True\n", "strict = True\nfollow_imports = skip\n"), "[mypy] must hold"),
        (_sub("strict = True\n", "strict = True\nexclude = tools/\n"), "[mypy] must hold"),
        (_sub("enable_error_code = ignore-without-code\n", ""), "[mypy] must hold"),
        (_sub("warn_unused_ignores = True\n", ""), "[mypy] must hold"),
        (_sub("files = hermes-plugin/glasses, tools\n", "files = tools\n"), "[mypy] must hold"),
        (_sub("ignore_missing_imports = True\n", "ignore_missing_imports = True\nfollow_imports = skip\n"), HERMES_MSG),
        (_sub("subclassing_any = False\n", "subclassing_any = False\nignore_errors = True\n"), ADAPTER_MSG),
        ("[DEFAULT]\nignore_errors = True\n" + INI_TEXT, "[mypy] must hold"),
        (INI_TEXT + "\n# T1.200\n[mypy-glasses.registry]\nignore_errors = True\n", "not in the baseline"),
        (INI_TEXT + "\n# T1.300\n[mypy-glasses.*]\nignore_errors = True\n", "not in the baseline"),
        (INI_TEXT + "\n[mypy-glasses.registry]\nignore_errors = True\n", "unexpected section"),
    ],
)
def test_planted_config_bypass_fails(text: str, message: str) -> None:
    errors = cmc.check_config(text)
    assert any(message in error for error in errors), errors


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# T1.5\n[mypy-a]\nignore_errors = True\ndisable_error_code = misc\n", "[mypy-a] must hold exactly"),
        ("# T1.5\n[mypy-a]\nignore_errors = False\n", "[mypy-a] must hold exactly"),
        ("# T1.5\n[mypy-a]\n", "[mypy-a] must hold exactly"),
        ("# T1.5\n[mypy-a]\nignore_errors = True\n\n# T1.5\n[mypy-b.*]\nignore_errors = True\n", "second block"),
        ("# T1.5\n# T1.06a\n[mypy-a]\nignore_errors = True\n", "needs a blank line"),
        ("# T1.5\n\n[mypy-a]\nignore_errors = True\n", "block T1.5 has no section"),
        ("# T1.5\n\n[mypy-a]\nignore_errors = True\n", "unexpected section"),
        ("# fix later\n[mypy-a]\nignore_errors = True\n", "unexpected section"),
        ("# T1.5\n[mypy-a.*.b]\nignore_errors = True\n", "not in its baseline"),
        ("# T1.5\n[mypy-a,z]\nignore_errors = True\n", "not in its baseline"),
        ("# T1.06a\n[mypy-a]\nignore_errors = True\n", "not in its baseline"),
        ("# T1.7\n[mypy-a]\nignore_errors = True\n", "not in the baseline"),
    ],
)
def test_malformed_baseline_fails(text: str, message: str) -> None:
    errors = cmc.check_config(GOOD + "\n" + text, RATCHET)
    assert any(message in error for error in errors), errors


def test_valid_layouts_pass() -> None:
    assert cmc.check_config(GOOD + BLOCKS, RATCHET) == []
    assert cmc.check_config(GOOD, RATCHET) == []  # every block deleted: the ratchet only shrinks
    assert cmc.check_config(GOOD + "\n# T1.5\n[mypy-b.*]\nignore_errors = True\n", RATCHET) == []
    assert cmc.check_config(GLOBAL_ONLY, RATCHET) == []
    assert cmc.check_config(INI_TEXT) == []


def test_missing_global_section_or_unreadable_config_fails() -> None:
    assert "missing [mypy] section" in cmc.check_config(BOUNDARY + BLOCKS, RATCHET)
    with pytest.raises(cmc.ConfigError, match="strict"):
        cmc.check_config(_sub("strict = True\n", "strict = True\nStrict = False\n"))


def test_real_tree_has_no_inline_suppressions() -> None:
    files = cmc.checked_files(ROOT)
    assert Path(__file__) in files
    assert ROOT / "tools/quality/check_mypy_config.py" in files
    assert ROOT / "hermes-plugin/glasses/registry.py" in files
    assert cmc.scan(ROOT, files) == []


@pytest.mark.parametrize(
    ("where", "prefix", "what"),
    [
        ("hermes-plugin/glasses/registry.py", MYPY_COMMENT, "mypy inline configuration comment"),
        ("hermes-plugin/glasses/registry.py", "#my" + "py: disable-error-code=misc\n", "mypy inline configuration"),
        ("tools/quality/check_size.py", TYPE_IGNORE, "type-ignore comment on a line of its own"),
        ("tools/quality/check_size.py", "  #type:ig" + "nore[misc]\n", "type-ignore comment on a line of its own"),
        ("tools/new-tool/planted_nocheck.py", NO_CHECK, "typing decorator that switches checking off"),
        ("hermes-plugin/glasses/generated/planted_stub.pyi", MYPY_COMMENT, "mypy inline configuration comment"),
    ],
)
def test_planted_inline_suppression_fails_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], where: str, prefix: str, what: str
) -> None:
    root = copy_tree(tmp_path)
    target = root / where
    target.parent.mkdir(parents=True, exist_ok=True)
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    target.write_text(prefix + original, encoding="utf-8")
    assert cmc.main(["--root", str(root)]) == 1
    assert f"{where}:1: {what}" in capsys.readouterr().err


def test_cli_ok_on_a_clean_copy_and_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = copy_tree(tmp_path / "tree")
    assert cmc.main(["--root", str(root)]) == 0
    assert "check-mypy-config: OK" in capsys.readouterr().out
    empty = tmp_path / "empty"
    empty.mkdir()
    shutil.copy(root / "mypy.ini", empty / "mypy.ini")
    assert cmc.main(["--root", str(empty)]) == 1
    assert "no checked files" in capsys.readouterr().err
    (root / "mypy.ini").unlink()
    assert cmc.main(["--root", str(root)]) == 2


TC = "from typing import TYPE_CHECKING\n\n"
TYPING_ONLY = "if TYPE_CHECKING:\n    from pathlib import Path\n    import typing\n"


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        (TC + "if not TYPE_CHECKING:\n    X = 1\n", [3]),
        ("import typing\n\nif not typing.TYPE_CHECKING:\n    X = 1\n", [3]),
        (TC + TYPING_ONLY + "else:\n    X = 1\n", [3]),
        (TC + TYPING_ONLY + "elif X:\n    Y = 1\n", [3]),
        (TC + "if TYPE_CHECKING:\n    raise SystemExit\n", [3]),
        (TC + "if TYPE_CHECKING and X:\n    import typing\n", [3]),
        (TC + "assert not TYPE_CHECKING\n", [3]),
        (TC + "X = 1 if TYPE_CHECKING else 2\n", [3]),
        (TC + "while not TYPE_CHECKING:\n    break\n", [3]),
        ("MYPY = False\nif not MYPY:\n    X = 1\n", [1, 2]),
        ("PY2 = True\nif PY2:\n    X = 1\n", [1, 2]),
        ("import six\n\nif not six.PY3:\n    X = 1\n", [3]),
    ],
)
def test_code_hidden_behind_a_mypy_constant_is_found(source: str, lines: list[int]) -> None:
    assert cmc.hidden_code(ast.parse(source)) == lines


def test_typing_only_imports_under_type_checking_pass() -> None:
    assert cmc.hidden_code(ast.parse(TC + TYPING_ONLY)) == []
    assert cmc.hidden_code(ast.parse("import typing\n\nif typing.TYPE_CHECKING:\n    import os\n")) == []
    assert cmc.hidden_code(ast.parse("from typing import TYPE_CHECKING as TC\n")) == []


@pytest.mark.parametrize(
    ("where", "what"),
    [
        ("tools/quality/planted.pyi", "stub file"),
        ("hermes-plugin/glasses/registry.pyi", "stub file"),
        ("tools/.planted/x.py", "crawl skips this path"),
        ("tools/node_modules/x.py", "crawl skips this path"),
        ("tools/quality/.planted.py", "crawl skips this path"),
        ("tools/quality/check_size/__init__.py", "a directory of the same name"),
    ],
)
def test_files_mypy_never_reads_fail_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], where: str, what: str
) -> None:
    root = copy_tree(tmp_path)
    target = root / where
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("X: int = 1\n", encoding="utf-8")
    assert cmc.main(["--root", str(root)]) == 1
    err = capsys.readouterr().err
    shown = "tools/quality/check_size.py" if what.startswith("a directory") else where
    assert f"check-mypy-config: {shown}: " in err and what in err, err


def test_hidden_code_and_invalid_python_fail_the_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = copy_tree(tmp_path)
    (root / "tools/planted_hidden.py").write_text(TC + "if not TYPE_CHECKING:\n    X = 1\n", encoding="utf-8")
    (root / "tools/planted_broken.py").write_text("X = 1\ndef (:\n", encoding="utf-8")
    assert cmc.main(["--root", str(root)]) == 1
    err = capsys.readouterr().err
    assert f"tools/planted_hidden.py:3: {cmc.FLAG_MSG}" in err, err
    assert "tools/planted_broken.py:2: not valid Python" in err, err


def test_bare_type_ignore_on_a_line_fails_mypy(tmp_path: Path) -> None:
    root = copy_tree(tmp_path / "tree")
    bare = "\n\ndef planted_bare_ignore(value):  # type: ig" + "nore\n    return value\n"
    with (root / "hermes-plugin/glasses/registry.py").open("a", encoding="utf-8") as handle:
        handle.write(bare)
    code, output = run_mypy(root, root / "mypy.ini", tmp_path / "cache")
    assert code == 1, output
    assert re.search(r"glasses/registry\.py:\d+: error: .*\[ignore-without-code\]", output), output
