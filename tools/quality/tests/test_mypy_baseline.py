# SPDX-License-Identifier: Apache-2.0
"""Tests of the mypy --strict gate (T1.99): baseline blocks of mypy.ini against real mypy runs, planted errors.

The layout rules live in tools/quality/check_mypy_config.py (tests: test_mypy_config.py). The mypy runs here are
compared with the errors of the current tree, so a task that deletes its own baseline block leaves these tests
green; only the gate's own mypy run then shows that block's errors.
"""

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
INI = ROOT / "mypy.ini"
ERROR = re.compile(r"^(?P<path>[^:\n]+\.py):\d+: error: ", re.MULTILINE)
PLANTED = "\n\ndef planted_untyped(value):\n    return value\n"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_mypy_config", ROOT / "tools/quality/check_mypy_config.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_mypy_config"] = module
    spec.loader.exec_module(module)
    return module


cmc = _load()


def baseline() -> dict[str, list[str]]:
    """Follow-up id -> module globs of the current mypy.ini baseline blocks."""
    blocks, _ = cmc.parse_blocks(INI.read_text(encoding="utf-8"))
    return {task: [glob for section in sections for glob in cmc.globs(section)] for task, sections in blocks.items()}


def remove_block(text: str, task: str) -> str:
    """mypy.ini text without the baseline block of `task` (header line to the next blank line)."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if line.strip() == f"# {task}":
            skipping = True
        elif skipping and not line.strip():
            skipping = False
        if not skipping:
            out.append(line)
    return "".join(out)


def module_of(path: Path) -> str:
    """Module name mypy gives `path` (namespace_packages off: crawl up through __init__.py only)."""
    parts = [] if path.name == "__init__.py" else [path.stem]
    folder = path.parent
    while (folder / "__init__.py").exists():
        parts.insert(0, folder.name)
        folder = folder.parent
    return ".".join(parts)


def matches(glob: str, module: str) -> bool:
    if glob.endswith(".*"):
        base = glob.removesuffix(".*")
        return module == base or module.startswith(base + ".")
    return module == glob


def run_mypy(root: Path, config: Path, cache: Path) -> tuple[int, str]:
    argv = [sys.executable, "-m", "mypy", "--config-file", str(config), "--cache-dir", str(cache)]
    done = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout + done.stderr


def error_modules(output: str, root: Path) -> set[str]:
    return {module_of(root / match.group("path")) for match in ERROR.finditer(output)}


@pytest.fixture(scope="module")
def cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("mypy-cache")


@pytest.fixture(scope="module")
def tree_errors(cache: Path) -> set[str]:
    """Modules with mypy errors in the current tree: empty when the gate is green."""
    return error_modules(run_mypy(ROOT, INI, cache)[1], ROOT)


@pytest.mark.parametrize("task", sorted(baseline()))
def test_removing_a_block_shows_exactly_its_errors(
    task: str, cache: Path, tmp_path: Path, tree_errors: set[str]
) -> None:
    globs = baseline()[task]
    config = tmp_path / "mypy.ini"
    config.write_text(remove_block(INI.read_text(encoding="utf-8"), task), encoding="utf-8")
    code, output = run_mypy(ROOT, config, cache)
    assert code == 1, output
    modules = error_modules(output, ROOT) - tree_errors
    assert all(any(matches(glob, module) for glob in globs) for module in modules), output
    stale = [glob for glob in globs if not any(matches(glob, module) for module in modules)]
    assert not stale, f"stale baseline sections of {task} (no errors left): {stale}"


def test_real_baseline_only_shrinks() -> None:
    assert cmc.check_config(INI.read_text(encoding="utf-8")) == []
    for task, globs in baseline().items():
        assert set(globs) <= set(cmc.RATCHET[task])


def copy_tree(dest: Path) -> Path:
    skip = shutil.ignore_patterns("__pycache__", ".mypy_cache")
    shutil.copytree(ROOT / "hermes-plugin" / "glasses", dest / "hermes-plugin" / "glasses", ignore=skip)
    shutil.copytree(ROOT / "tools", dest / "tools", ignore=skip)
    shutil.copy(INI, dest / "mypy.ini")
    return dest


def plant(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def test_planted_untyped_function_fails_in_plugin_and_tools(cache: Path, tmp_path: Path, tree_errors: set[str]) -> None:
    root = copy_tree(tmp_path)
    plant(root / "hermes-plugin/glasses/registry.py", PLANTED)
    plant(root / "tools/quality/check_size.py", PLANTED)
    code, output = run_mypy(root, root / "mypy.ini", cache)
    assert code == 1, output
    assert re.search(r"hermes-plugin/glasses/registry\.py:\d+: error: .*\[no-untyped-def\]", output), output
    assert re.search(r"tools/quality/check_size\.py:\d+: error: .*\[no-untyped-def\]", output), output
    assert error_modules(output, root) - tree_errors == {"glasses.registry", "check_size"}


def test_new_tools_file_is_checked(cache: Path, tmp_path: Path) -> None:
    root = copy_tree(tmp_path)
    (root / "tools/new-tool").mkdir()
    plant(root / "tools/new-tool/new_tool.py", PLANTED)
    code, output = run_mypy(root, root / "mypy.ini", cache)
    assert code == 1, output
    assert "tools/new-tool/new_tool.py" in output, output


def test_hermes_tools_import_is_not_our_tools_dir(cache: Path, tmp_path: Path) -> None:
    root = copy_tree(tmp_path)
    plant(root / "hermes-plugin/glasses/registry.py", "\n\nfrom tools import slash_confirm as _sc  # Hermes\n")
    _, output = run_mypy(root, root / "mypy.ini", cache)
    assert 'Module "tools"' not in output, output
    assert "slash_confirm" not in output, output


def test_remove_block_drops_only_that_block() -> None:
    text = "# T1.5\n[mypy-a]\nignore_errors = True\n[mypy-b.*]\nignore_errors = True\n\n# T1.6\n[mypy-c]\nx = 1\n"
    assert cmc.parse_blocks(remove_block(text, "T1.5"))[0] == {"T1.6": ["mypy-c"]}
    assert cmc.parse_blocks(remove_block(text, "T1.6"))[0] == {"T1.5": ["mypy-a", "mypy-b.*"]}


def test_module_of_and_matches() -> None:
    assert module_of(ROOT / "hermes-plugin/glasses/__init__.py") == "glasses"
    assert module_of(ROOT / "hermes-plugin/glasses/gate.py") == "glasses.gate"
    assert module_of(ROOT / "tools/mock-llm/mock_llm.py") == "mock_llm"
    assert matches("glasses.adapter.*", "glasses.adapter")
    assert not matches("glasses.adapter.*", "glasses.adapters")
    assert not matches("glasses", "glasses.gate")
