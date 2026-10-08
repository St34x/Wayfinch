# SPDX-License-Identifier: Apache-2.0
"""Tests of the coverage gate (T1.101): tools/quality/coverage_areas.py with `.coveragerc` and `.importlinter`.

Unit tests build their own config text. The end-to-end tests plant a tiny plugin (one core module, one adapter, their
tests) in a scratch tree with the REAL `.coveragerc` (its ratchet entries removed) and a minimal core-purity contract,
run the real pytest + coverage there and then the real gate script. The real-tree test checks structure only, never
today's core list or ratchet blocks, so a task that adds a core module or deletes its block keeps this file green.
"""

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coverage_areas as ca

TOP = ca.TOP
SCRIPT = Path(ca.__file__).resolve()
RC_TEXT = (TOP / ".coveragerc").read_text(encoding="utf-8")
RC_NO_RATCHET = RC_TEXT.split(f"[{ca.RATCHET}]")[0] + f"[{ca.RATCHET}]\n"
CONTRACT = "[importlinter]\nroot_package = glasses\n\n[importlinter:contract:core-purity]\nname = c\ntype = forbidden\n"
CORE_SRC = "def pick(flag):\n    if flag:\n        return 1\n    return 2\n"
CORE_TEST = (
    "from glasses.core_mod import pick\n\n\ndef test_pick():\n    assert pick(True) == 1\n"
    + "    assert pick(False) == 2\n"
)
ADAPTER_SRC = "def send(text):\n    return text.upper()\n"
ADAPTER_TEST = "from glasses.adapter_mod import send\n\n\ndef test_send():\n    assert send('a') == 'A'\n"


def importlinter(core: str) -> str:
    return f"{CONTRACT}source_modules =\n    {core}\nforbidden_modules =\n    socket\n"


def plant(top: Path, rc_text: str = RC_NO_RATCHET) -> Path:
    """A scratch repository tree: core `glasses.core_mod`, adapter `glasses.adapter_mod`, tests for both."""
    pkg, tests = top / "hermes-plugin/glasses", top / "hermes-plugin/tests"
    for path, text in {
        pkg / "__init__.py": "",
        pkg / "core_mod.py": CORE_SRC,
        pkg / "adapter_mod.py": ADAPTER_SRC,
        pkg / "generated/__init__.py": "",
        pkg / "generated/types.py": "X = 1\nif X:\n    Y = 2\n",
        tests / "test_core.py": CORE_TEST,
        tests / "test_adapter.py": ADAPTER_TEST,
        top / ".coveragerc": rc_text,
        top / ".importlinter": importlinter("glasses.core_mod"),
        top / "pytest.ini": "[pytest]\npythonpath = hermes-plugin\n",
    }.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    # The recipe's `--basetemp=.local/pytest-tmp/<dir>` resolves inside this scratch tree (pytest needs the parent).
    (top / ".local/pytest-tmp").mkdir(parents=True, exist_ok=True)
    return top


def run_tests(top: Path) -> None:
    argv = [sys.executable, "-m", "pytest", "--basetemp=.local/pytest-tmp/plugin-tests", "hermes-plugin/tests", "-q"]
    argv += ["-p", "no:cacheprovider", "--cov"]
    done = subprocess.run([*argv, "--cov-config=.coveragerc", "--cov-report="], cwd=top, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def gate(top: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), "--top", str(top)], capture_output=True, text=True)


def recipe(*, erase: bool = True) -> list[list[str]]:
    """The real `coverage-py` recipe of mk/coverage-py.mk as argv lists with `$(PY)` = this interpreter, minus the
    lines that run this gate's own test files (they need this repository); `erase=False` also drops the `rm` line."""
    body = (TOP / "mk/coverage-py.mk").read_text(encoding="utf-8").split("\ncoverage-py:", 1)[1]
    lines = [line.strip() for line in body.splitlines() if line.startswith("\t")]
    lines = [line for line in lines if "COVERAGE_PY_TESTS" not in line and (erase or not line.startswith("rm "))]
    return [[sys.executable if arg == "$(PY)" else arg for arg in shlex.split(line)] for line in lines]


def run_recipe(top: Path, argvs: list[list[str]]) -> subprocess.CompletedProcess[str]:
    """Run the recipe lines in `top` like make: stop at the first failing line and return it (else the last one)."""
    done = None
    for argv in argvs:
        done = subprocess.run(argv, cwd=top, capture_output=True, text=True)
        if done.returncode != 0:
            break
    assert done is not None
    return done


# --- .coveragerc: exactly the expected keys ---------------------------------------------------------------------


def test_real_coveragerc_is_accepted() -> None:
    assert ca.config_findings(RC_TEXT) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("\nbranch = True\n", "\nbranch = False\n"),
        ("[report]\n", "[report]\nexclude_also = def\n"),
        ("exclude_lines =\n", "exclude_lines = pragma: no cover\n"),
        ("partial_branches =\n", "partial_branches = pragma: no branch\n"),
        ("omit = hermes-plugin/glasses/generated/*", "omit = hermes-plugin/glasses/generated/*, */adapter_mod.py"),
        ("[report]\n", "[paths]\nsource = x\n\n[report]\n"),
        ("[report]\n", "[coverage:report]\nexclude_also = def\n\n[report]\n"),
    ],
)
def test_tampered_coveragerc_fails(old: str, new: str) -> None:
    assert old in RC_NO_RATCHET
    assert ca.config_findings(RC_NO_RATCHET.replace(old, new, 1))


def test_missing_run_section_fails() -> None:
    assert ca.config_findings("[report]\nexclude_lines =\npartial_branches =\n")


def test_unreadable_coveragerc_is_config_error() -> None:
    with pytest.raises(ca.GateError):
        ca.config_findings("[run]\nbranch = True\n[run]\n")


# --- ratchet entries ---------------------------------------------------------------------------------------------


def test_ratchet_blocks_and_sub_ids() -> None:
    text = f"[{ca.RATCHET}]\n\n# T9.99 why\na.py = 5\n# note\nb.py = 7\n\n# T9.06a\nc.py = 0\n"
    assert ca.ratchet(text) == {
        "a.py": ca.Entry(5, "T9.99"),
        "b.py": ca.Entry(7, "T9.99"),
        "c.py": ca.Entry(0, "T9.06a"),
    }


@pytest.mark.parametrize(
    "body",
    [
        "a.py = 5\n",  # no id line
        "# T9.99\na.py = 5\n\nb.py = 6\n",  # a blank line ends the block
        "# no id here\na.py = 5\n",
        "# T9.99\na.py = five\n",
        "# T9.99\na.py = 5\na.py = 6\n",
        "# T9.99\na.py\n",
    ],
)
def test_bad_ratchet_is_config_error(body: str) -> None:
    with pytest.raises(ca.GateError):
        ca.ratchet(f"[{ca.RATCHET}]\n{body}")


def test_missing_ratchet_section_is_config_error() -> None:
    with pytest.raises(ca.GateError):
        ca.ratchet("[run]\nbranch = True\n")


# --- per-file judgement ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("percent", "minimum", "entry", "fails"),
    [
        (89, 90, None, True),
        (90, 90, None, False),
        (69, 70, None, True),
        (70, 70, None, False),
        (8, 70, ca.Entry(8, "T9.99"), False),
        (7, 70, ca.Entry(8, "T9.99"), True),  # dropped
        (9, 70, ca.Entry(8, "T9.99"), True),  # raised: the entry must follow
        (70, 70, ca.Entry(8, "T9.99"), True),  # stale
    ],
)
def test_judge(percent: int, minimum: int, entry: ca.Entry | None, fails: bool) -> None:
    assert (ca.judge("x.py", percent, minimum, entry) is not None) is fails


def test_unmeasured_file_is_config_error() -> None:
    with pytest.raises(ca.GateError, match="unmeasured"):
        ca.file_findings({"a.py": 100}, {"a.py": 90, "b.py": 70}, {})


def test_ratchet_entry_for_unmeasured_file_is_stale() -> None:
    findings = ca.file_findings({"a.py": 100}, {"a.py": 90}, {"gone.py": ca.Entry(3, "T9.99")})
    assert findings == ["stale ratchet entry gone.py (T9.99): not measured"]


# --- areas: the core list comes from .importlinter ---------------------------------------------------------------


def test_areas_read_core_list_and_skip_generated(tmp_path: Path) -> None:
    top = plant(tmp_path)
    core, rest = ca.areas(top, importlinter("glasses.core_mod"))
    assert core == ["hermes-plugin/glasses/core_mod.py"]
    assert rest == ["hermes-plugin/glasses/__init__.py", "hermes-plugin/glasses/adapter_mod.py"]


@pytest.mark.parametrize("core", ["glasses.missing_mod", "glasses.generated.types", "# none"])
def test_bad_core_list_is_config_error(tmp_path: Path, core: str) -> None:
    with pytest.raises(ca.GateError):
        ca.areas(plant(tmp_path), importlinter(core))


def test_empty_package_is_config_error(tmp_path: Path) -> None:
    (tmp_path / "hermes-plugin/glasses").mkdir(parents=True)
    with pytest.raises(ca.GateError, match="no"):
        ca.areas(tmp_path, importlinter("glasses.core_mod"))


def test_real_tree_areas_and_ratchet() -> None:
    core, rest = ca.areas(TOP, (TOP / ".importlinter").read_text(encoding="utf-8"))
    assert core
    assert set(ca.ratchet(RC_TEXT)) <= set(core) | set(rest)


# --- end to end: real pytest + coverage + gate script on a planted plugin ----------------------------------------


def test_planted_plugin_passes(tmp_path: Path) -> None:
    top = plant(tmp_path)
    run_tests(top)
    done = gate(top)
    assert done.returncode == 0, done.stderr
    assert "coverage-py: OK" in done.stdout


def test_deleting_the_core_test_fails(tmp_path: Path) -> None:
    top = plant(tmp_path)
    (top / "hermes-plugin/tests/test_core.py").unlink()
    run_tests(top)
    done = gate(top)
    assert done.returncode == 1
    assert "core_mod.py: 0 % (minimum 90) - below the minimum" in done.stderr
    assert "core area below 90 %" in done.stderr


def test_pragma_no_cover_does_not_exclude(tmp_path: Path) -> None:
    top = plant(tmp_path)
    test = top / "hermes-plugin/tests/test_core.py"
    test.write_text(CORE_TEST.replace("    assert pick(False) == 2\n", ""), encoding="utf-8")
    core = top / "hermes-plugin/glasses/core_mod.py"
    core.write_text(CORE_SRC.replace("return 2", "return 2  # pragma: no cover"), encoding="utf-8")
    run_tests(top)
    done = gate(top)
    assert done.returncode == 1
    assert "core_mod.py: 66 % (minimum 90)" in done.stderr


def test_ratchet_entry_lets_a_low_file_pass(tmp_path: Path) -> None:
    """adapter_mod at 50 % passes with its exact entry; a well-tested module keeps the rest area above 70 %."""
    top = plant(tmp_path, RC_NO_RATCHET + "\n# T9.99\nhermes-plugin/glasses/adapter_mod.py = 50\n")
    big = "def total():\n" + "".join(f"    a{i} = {i}\n" for i in range(10)) + "    return a0\n"
    (top / "hermes-plugin/glasses/big_mod.py").write_text(big, encoding="utf-8")
    only_import = "import glasses.adapter_mod\nfrom glasses.big_mod import total\n\n\ndef test_x():\n"
    only_import += "    assert total() == 0\n"
    (top / "hermes-plugin/tests/test_adapter.py").write_text(only_import, encoding="utf-8")
    run_tests(top)
    done = gate(top)
    assert done.returncode == 0, done.stderr
    entry = "hermes-plugin/glasses/adapter_mod.py = 50"
    (top / ".coveragerc").write_text(RC_NO_RATCHET + f"\n# T9.99\n{entry.replace('50', '49')}\n", encoding="utf-8")
    assert "set the entry to 50" in gate(top).stderr


def test_without_coverage_data_is_config_error(tmp_path: Path) -> None:
    done = gate(plant(tmp_path))
    assert done.returncode == 2
    assert "coverage json failed" in done.stderr


def test_stale_data_from_an_earlier_run_fails(tmp_path: Path) -> None:
    """One full run leaves a data file; then the core test is deleted and a pytest.ini switches coverage off. Without
    the recipe's `rm` line the checker would judge the old data and pass; the real recipe deletes it first: exit 2."""
    top = plant(tmp_path)
    (top / "tools/quality").mkdir(parents=True)
    for name in ("coverage_areas.py", "check_core_purity.py"):
        shutil.copy(SCRIPT.parent / name, top / "tools/quality" / name)
    first = run_recipe(top, recipe())
    assert first.returncode == 0, first.stderr
    assert "coverage-py: OK" in first.stdout
    (top / "hermes-plugin/tests/test_core.py").unlink()
    (top / "pytest.ini").write_text("[pytest]\npythonpath = hermes-plugin\naddopts = --no-cov\n", encoding="utf-8")
    assert run_recipe(top, recipe(erase=False)).returncode == 0  # the hole: old data, coverage off, still "OK"
    done = run_recipe(top, recipe())
    assert done.returncode == 2, done.stdout + done.stderr
    assert "coverage json failed" in done.stderr


def test_missing_config_is_config_error(tmp_path: Path) -> None:
    top = plant(tmp_path)
    (top / ".coveragerc").unlink()
    assert gate(top).returncode == 2
