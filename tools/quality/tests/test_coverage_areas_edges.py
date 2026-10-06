# SPDX-License-Identifier: Apache-2.0
"""Edge cases of the coverage gate: config bypasses beyond test_coverage_areas.py, ratchet line forms, the core
list edge cases, the recipe order (gate tests, delete the previous run's data, plugin suite, checker), and the
erase of the previous run's data (it also defeats `--cov-append`; it removes the `.coveragerc` data dir).
"""

import configparser
import re
import shlex
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coverage_areas as ca

from test_coverage_areas import plant, recipe, run_recipe

TOP = ca.TOP
RC_TEXT = (TOP / ".coveragerc").read_text(encoding="utf-8")
RC_NO_RATCHET = RC_TEXT.split(f"[{ca.RATCHET}]")[0] + f"[{ca.RATCHET}]\n"


@pytest.mark.parametrize(
    "text",
    [
        "[DEFAULT]\nexclude_also = return\n\n" + RC_NO_RATCHET,  # DEFAULT keys leak into every section
        RC_NO_RATCHET.replace("generated/*", "generated/*${EXTRA_OMIT}"),  # coverage expands env vars
        RC_NO_RATCHET.replace("branch = True", "branch = True ; off"),
        RC_NO_RATCHET.replace("branch = True", "branch = 1"),
        RC_NO_RATCHET.replace("relative_files = True", "relative_files = True\nplugins = planted_plugin"),
        RC_NO_RATCHET.replace(
            "relative_files = True", "relative_files = True\ninclude = hermes-plugin/glasses/gate.py"
        ),
        RC_NO_RATCHET + "\n[html]\ndirectory = x\n",
        RC_NO_RATCHET.replace("[run]", "[RUN]"),
    ],
)
def test_more_tampered_coveragerc_fails(text: str) -> None:
    assert ca.config_findings(text)


def test_duplicate_section_is_config_error() -> None:
    with pytest.raises(ca.GateError):
        ca.config_findings(RC_NO_RATCHET + "\n[report]\nexclude_also = return\n")


@pytest.mark.parametrize(
    "body",
    [
        "# T1.113\nhermes-plugin/glasses/adapter.py = 0008\n",
        "# T1.113\nhermes-plugin/glasses/adapter.py = 8 %\n",
        "# T1.113\nhermes-plugin/glasses/adapter.py: 8\n",
        "# T1.113\nexclude_also = return\n",
        "# TODO\nhermes-plugin/glasses/adapter.py = 8\n",
        "# T1.113\n\nhermes-plugin/glasses/adapter.py = 8\n",  # the blank line ends the block
    ],
)
def test_bad_ratchet_line_forms_are_config_errors(body: str) -> None:
    with pytest.raises(ca.GateError):
        ca.ratchet(RC_NO_RATCHET + body)


@pytest.mark.parametrize("header", ["# T1.113a", "# P.04", "# S07b", "# T1.113 adapter tests (comment)"])
def test_ratchet_header_forms_accepted(header: str) -> None:
    entries = ca.ratchet(RC_NO_RATCHET + f"{header}\nhermes-plugin/glasses/adapter.py = 8\n")
    assert entries["hermes-plugin/glasses/adapter.py"].percent == 8


def test_core_list_naming_generated_code_is_config_error(tmp_path: Path) -> None:
    pkg = tmp_path / "hermes-plugin/glasses"
    (pkg / "generated").mkdir(parents=True)
    for name in ("__init__.py", "core_mod.py", "generated/__init__.py", "generated/types.py"):
        (pkg / name).write_text("X = 1\n", encoding="utf-8")
    contract = "[importlinter]\nroot_package = glasses\n\n[importlinter:contract:core-purity]\nname = c\n"
    text = contract + "type = forbidden\nsource_modules =\n    glasses.generated\nforbidden_modules =\n    socket\n"
    with pytest.raises(ca.GateError):
        ca.areas(tmp_path, text)


def _coverage_recipe() -> list[str]:
    text = (TOP / "mk/coverage-py.mk").read_text(encoding="utf-8")
    recipe = text.split("\ncoverage-py:", 1)[1]
    return [line.strip() for line in recipe.splitlines() if line.startswith("\t")]


def test_recipe_runs_gate_tests_then_plugin_suite_then_checker() -> None:
    lines = _coverage_recipe()
    cov = next(i for i, line in enumerate(lines) if "--cov-config=.coveragerc" in line)
    gate = next(i for i, line in enumerate(lines) if "coverage_areas.py" in line)
    assert cov < gate


def test_recipe_erases_old_coverage_data_before_the_run() -> None:
    """A run that collects nothing (coverage switched off, e.g. by a pytest.ini `addopts = --no-cov`) must not leave
    the previous run's data for the checker to judge."""
    lines = _coverage_recipe()
    cov = next(i for i, line in enumerate(lines) if "--cov-config=.coveragerc" in line)
    erase = re.compile(r"coverage erase|rm -r?f[^;]*\.local/coverage-py")
    assert any(erase.search(line) for line in lines[:cov])


def test_cov_append_does_not_union_the_previous_run(tmp_path: Path) -> None:
    """A full run, then the core test deleted and `addopts = --cov-append`: the real recipe judges only this run."""
    top = plant(tmp_path)
    (top / "tools/quality").mkdir(parents=True)
    for name in ("coverage_areas.py", "check_core_purity.py"):
        shutil.copy(TOP / "tools/quality" / name, top / "tools/quality" / name)
    first = run_recipe(top, recipe())
    assert first.returncode == 0, first.stdout + first.stderr
    (top / "hermes-plugin/tests/test_core.py").unlink()
    (top / "pytest.ini").write_text("[pytest]\npythonpath = hermes-plugin\naddopts = --cov-append\n", encoding="utf-8")
    assert run_recipe(top, recipe(erase=False)).returncode == 0  # the hole without the erase line
    done = run_recipe(top, recipe())
    assert done.returncode == 1, done.stdout + done.stderr
    assert "core_mod.py: 0 %" in done.stderr


def test_erase_line_removes_the_configured_data_directory() -> None:
    """The `rm` line must delete the directory of `[run] data_file`, not some other path."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(RC_TEXT)
    data_dir = Path(parser["run"]["data_file"]).parent
    assert data_dir != Path(".")
    erase = [argv for argv in recipe() if argv[0] == "rm"]
    assert len(erase) == 1, erase
    assert Path(erase[0][-1]) == data_dir, shlex.join(erase[0])
    assert {"-rf", "-fr"} & set(erase[0]), shlex.join(erase[0])
