# SPDX-License-Identifier: Apache-2.0
"""Adversarial inputs for tools/quality/check_size.py."""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

QUALITY = Path(__file__).resolve().parents[1]
TOP = QUALITY.parents[1]
GIT = shutil.which("git") or "git"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_size_edges_mod", QUALITY / "check_size.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_size_edges_mod"] = module
    spec.loader.exec_module(module)
    return module


cs = _load()


def lines(n: int, prefix: str = "x = 1", eol: str = "\n") -> str:
    return "".join(f"{prefix}{eol}" for _ in range(n))


def body(n: int, indent: str = "    ") -> str:
    return "".join(f"{indent}v = {i}\n" for i in range(n))


def sh_body(n: int) -> str:
    return "".join(f"\techo {i}\n" for i in range(n))


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        subprocess.run([GIT, "init", "-q", str(root)], check=True)
        self.exempt = "*.schema.json\n**/generated/**\n"
        self.baseline = ""
        self.add("ok.py", lines(10))

    def add(self, path: str, data: str | bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_bytes(data.encode("utf-8"))
        self.track(path)

    def track(self, path: str) -> None:
        subprocess.run([GIT, "-C", str(self.root), "add", "--", path], check=True)

    def run(self, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
        (self.root / "exempt.txt").write_text(self.exempt, encoding="utf-8")
        (self.root / "baseline.txt").write_text(self.baseline, encoding="utf-8")
        argv = ["--root", str(self.root), "--exempt", str(self.root / "exempt.txt")]
        rc = cs.main([*argv, "--baseline", str(self.root / "baseline.txt")])
        out = capsys.readouterr()
        return rc, out.out + out.err


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "r")


def errors(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("check-size: error")]


# --- line endings, whitespace, long lines ---------------------------------------------------------------


@pytest.mark.parametrize(("eol", "n", "rc"), [("\r\n", 401, 1), ("\r\n", 400, 0), ("\r", 401, 1), ("\r", 400, 0)])
def test_crlf_and_cr_only_files_count_lines(
    repo: Repo, capsys: pytest.CaptureFixture[str], eol: str, n: int, rc: int
) -> None:
    repo.add("crlf.py", lines(n, eol=eol))
    got, out = repo.run(capsys)
    assert got == rc, out


def test_crlf_python_function_61_fails(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("f.py", ("def f() -> None:\n" + body(60)).replace("\n", "\r\n"))
    rc, out = repo.run(capsys)
    assert rc == 1 and any("f.py::f: 61" in e for e in errors(out)), out


def test_tab_indented_python_and_no_trailing_newline(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("t.py", "def f():\n" + body(59, "\t") + "\tv = 99")  # 61 lines, last without newline
    rc, out = repo.run(capsys)
    assert rc == 1 and any("t.py::f: 61" in e for e in errors(out)), out


def test_very_long_lines_count_as_one_line_each(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("long.py", lines(3, "x = '" + "a" * 100_000 + "'"))
    rc, out = repo.run(capsys)
    assert rc == 0, out


# --- where a Python function is measured from -------------------------------------------------------------


def test_function_length_starts_at_def_not_at_decorators(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    deco = "@a\n@b(\n    1,\n)\n"
    repo.add("d.py", "a = b = lambda *x: (lambda f: f)\n" + deco + "def f() -> None:\n" + body(59))  # def..end = 60
    rc, out = repo.run(capsys)
    assert rc == 0, out


def test_nested_function_is_measured_alone_and_inside_its_parent(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    inner = "    def inner() -> None:\n" + body(60, "        ")  # 61
    repo.add("n.py", "def outer() -> None:\n" + inner + "    inner()\n")  # 63
    rc, out = repo.run(capsys)
    errs = errors(out)
    assert rc == 1
    assert any("n.py::outer.inner: 61" in e for e in errs), out
    assert any("n.py::outer: 63" in e for e in errs), out


def test_async_method_and_class_in_function_keys(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    meth = "        async def m(self) -> None:\n" + body(60, "            ")
    repo.add("c.py", "def factory() -> None:\n    class C:\n" + meth)
    rc, out = repo.run(capsys)
    assert rc == 1 and any("c.py::factory.C.m: 61" in e for e in errors(out)), out


def test_same_name_defined_twice_keeps_the_longest(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    prop = "class C:\n    @property\n    def p(self) -> int:\n        return 1\n\n"
    prop += "    @p.setter\n    def p(self, v: int) -> None:\n"
    repo.add("p.py", prop + body(60, "        "))
    rc, out = repo.run(capsys)
    assert rc == 1 and any("p.py::C.p: 61" in e for e in errors(out)), out


def test_multiline_lambda_is_not_a_function(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("l.py", "f = lambda: [\n" + lines(70, "    1,") + "]\n")
    rc, out = repo.run(capsys)
    assert rc == 0, out  # pinned behaviour: only def/async def are measured


# --- odd sh syntax ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("head", ["f ( ) {", "f() { # comment", "f()(", "  f() {"])
def test_sh_function_header_variants_are_measured(repo: Repo, capsys: pytest.CaptureFixture[str], head: str) -> None:
    indent = head[: len(head) - len(head.lstrip())]
    close = ")" if head.endswith("(") else "}"
    repo.add("t.sh", f"{head}\n{sh_body(59)}{indent}{close} >&2\n")
    rc, out = repo.run(capsys)
    assert rc == 1 and any("t.sh::f: 61" in e for e in errors(out)), out


def test_sh_one_line_function_is_not_measured(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("t.sh", "f() { echo a; }\n" + lines(100, "echo b"))
    assert repo.run(capsys)[0] == 0


def test_sh_brace_on_next_line_is_measured(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("t.sh", "f()\n{\n" + sh_body(60) + "}\n")
    rc, out = repo.run(capsys)
    assert rc == 1 and "t.sh::f" in out, out


def test_sh_heredoc_brace_does_not_end_function(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("t.sh", "f() {\n\tcat <<EOF\n{\n}\nEOF\n" + sh_body(60) + "}\n")
    rc, out = repo.run(capsys)
    assert rc == 1 and "t.sh::f: 66" in out, out


# --- symlinks, binary files, unicode file names -----------------------------------------------------------


def test_tracked_symlink_is_skipped_and_its_target_measured(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("big.py", lines(401))
    os.symlink("big.py", repo.root / "link.py")
    repo.track("link.py")
    rc, out = repo.run(capsys)
    assert rc == 1 and any("big.py: 401" in e for e in errors(out)) and "link.py" not in out, out


def test_symlink_to_untracked_big_file_is_not_followed(
    repo: Repo, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / "outside.py").write_text(lines(900), encoding="utf-8")
    os.symlink(tmp_path / "outside.py", repo.root / "link.py")
    repo.track("link.py")
    assert repo.run(capsys)[0] == 0


def test_binary_python_file_fails_closed(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("bin.py", b"\x00\xff\xfe" * 50 + b"\n")
    rc, out = repo.run(capsys)
    assert rc == 1 and "bin.py: cannot measure" in out, out


def test_binary_kotlin_file_is_line_counted_without_crash(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("B.kt", b"\xff\xfe\x00\n" * 401)
    rc, out = repo.run(capsys)
    assert rc == 1 and "B.kt: 401" in out, out


@pytest.mark.parametrize("name", ["ünï/çødé.py", "a b/sp ace.py", "nl\nname.py", "emoji-😀.sh"])
def test_unicode_and_odd_file_names(repo: Repo, capsys: pytest.CaptureFixture[str], name: str) -> None:
    repo.add(name, lines(401, "# l"))
    rc, out = repo.run(capsys)
    assert rc == 1 and name in out, out


# --- @generated marker ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("line_no", "rc"), [(10, 0), (11, 1)])
def test_generated_marker_only_in_first_ten_lines(
    repo: Repo, capsys: pytest.CaptureFixture[str], line_no: int, rc: int
) -> None:
    repo.add("g.py", lines(line_no - 1, "# h") + "# @generated by tools/codegen\n" + lines(500))
    got, out = repo.run(capsys)
    assert got == rc, out


def test_generated_mentioned_in_prose_does_not_exempt(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("tool.py", '"""Checker.\n\nSkips files whose header says "@generated".\n"""\n' + lines(500))
    rc, out = repo.run(capsys)
    assert rc == 1, out


def test_check_size_itself_is_measured() -> None:
    assert not cs.is_generated((QUALITY / "check_size.py").read_text(encoding="utf-8"))


# --- baseline edits ---------------------------------------------------------------------------------------


def test_crlf_baseline_parses(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("big.py", lines(450))
    repo.baseline = "# T1.111\r\nbig.py:450 # T1.111 split it\r\n\r\n"
    rc, out = repo.run(capsys)
    assert rc == 0, out


def test_removing_a_block_while_the_file_is_still_big_fails(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("big.py", lines(450))
    repo.baseline = "# T1.110\n# (block removed by T1.110 although the file was not split)\n"
    rc, out = repo.run(capsys)
    assert rc == 1 and "big.py: 450 lines > hard limit 400" in out, out


def test_baseline_entry_for_an_exempt_file_is_stale(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("pkg/generated/t.py", lines(900))
    repo.baseline = "# T1.110\npkg/generated/t.py:900 # T1.110\n"
    rc, out = repo.run(capsys)
    assert rc == 1 and "stale" in out, out


def test_baseline_entry_for_target_range_file_is_stale(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("mid.py", lines(350))
    repo.baseline = "# T1.110\nmid.py:350 # T1.110\n"
    rc, out = repo.run(capsys)
    assert rc == 1 and "stale" in out, out


def test_baseline_accepts_task_sub_ids(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.add("big.py", lines(450))
    repo.baseline = "# T1.15a\nbig.py:450 # T1.15a\n"
    rc, out = repo.run(capsys)
    assert rc == 0, out


# --- exempt globs must not over-match ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "x.json.py",
        "LICENSE.py",
        "tools/LICENSE_check.sh",
        "tests/golden_test.py",
        "goldenx/a.py",
        "generated.py",
        "a/generatedx/b.py",
        "requirements-dev.txt.py",
        "NOTICE.sh",
        "my.schema.json.py",
    ],
)
def test_real_exempt_globs_do_not_over_match(path: str) -> None:
    patterns = cs.parse_exempt((QUALITY / "size-exempt.txt").read_text(encoding="utf-8"))
    assert not cs.is_exempt(path, patterns)


def test_no_tracked_source_file_is_exempted_by_a_glob_on_the_real_tree() -> None:
    patterns = cs.parse_exempt((QUALITY / "size-exempt.txt").read_text(encoding="utf-8"))
    hit = [p for p in cs.tracked_files(TOP) if cs.is_source(p) and cs.is_exempt(p, patterns)]
    assert all("/generated/" in f"/{p}" or p.startswith("android/third_party/") for p in hit), hit


# --- fail closed on a broken repository -------------------------------------------------------------------


def test_corrupt_git_index_is_a_usage_error(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    (repo.root / ".git" / "index").write_bytes(b"garbage\n")
    rc, out = repo.run(capsys)
    assert rc == 2, out


def test_repo_without_source_files_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "empty"
    subprocess.run([GIT, "init", "-q", str(root)], check=True)
    (root / "e.txt").write_text("", encoding="utf-8")
    argv = ["--root", str(root), "--exempt", str(root / "e.txt"), "--baseline", str(root / "e.txt")]
    assert cs.main(argv) == 2
    assert "no source files" in capsys.readouterr().err
