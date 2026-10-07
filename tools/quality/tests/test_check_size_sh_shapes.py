# SPDX-License-Identifier: Apache-2.0
"""sh function shapes of the size gate (tools/quality/size_sh.py through check_size.py).

Brace-line functions with nested groups, braces in comments, quotes and here-documents, CRLF line ends and tab
indents are measured; a function whose end cannot be found fails the gate, never passes it. A brace or parenthesis
inside a quoted string, a parameter expansion or a case pattern never ends a function early, and a one-line function
redirected after its "}" is not measured and does not run on into the next function.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

QUALITY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUALITY))  # check_size.py imports size_sh.py from its own directory
GIT = shutil.which("git") or "git"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_size_sh_shapes_mod", QUALITY / "check_size.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_size_sh_shapes_mod"] = module
    spec.loader.exec_module(module)
    return module


cs = _load()


def body(n: int) -> str:
    return "".join(f"\techo {i}\n" for i in range(n))


def run_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str) -> tuple[int, str]:
    root = tmp_path / "r"
    subprocess.run([GIT, "init", "-q", str(root)], check=True)
    (root / "t.sh").write_bytes(text.encode("utf-8"))
    subprocess.run([GIT, "-C", str(root), "add", "--", "t.sh"], check=True)
    (root / "exempt.txt").write_text("", encoding="utf-8")
    (root / "baseline.txt").write_text("", encoding="utf-8")
    argv = ["--root", str(root), "--exempt", str(root / "exempt.txt"), "--baseline", str(root / "baseline.txt")]
    rc = cs.main(argv)
    out = capsys.readouterr()
    return rc, out.out + out.err


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("f() { a\n\t# }\n" + body(5) + "}\n", [("f", 8)]),
        ("f() { a\n\tcat <<EOF\n}\nEOF\n" + body(5) + "}\n", [("f", 10)]),
        ("f() { a\n\tcat <<-EOF\n\t}\n\tEOF\n\tb; }\n", [("f", 5)]),
        (("f() { a\n" + body(5) + "}\n").replace("\n", "\r\n"), [("f", 7)]),
        (("f() {\n" + body(5) + "}\n").replace("\n", "\r\n"), [("f", 7)]),
        ("if x; then\n\tf() { a\n" + body(5) + "\t}\nfi\n", [("f", 7)]),
        ("f() {\n" + body(5) + "} >x\n", [("f", 7)]),
        ("f() { a; }; g() { b; }\n", []),
        ("f() ( a;\n" + body(5) + ")\n", [("f", 7)]),
    ],
)
def test_brace_line_shapes_are_measured_to_their_end(text: str, expected: list[tuple[str, int]]) -> None:
    assert cs.sh_functions(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "f() { a\n" + body(70),
        "f() { a\n\tcase $1 in\n\t\t*}) b ;;\n\tesac\n" + body(66) + "}\n",
        "f() ( a\n\tcase $1 in\n\t\tx) b ;;\n\tesac\n" + body(66) + ")\n",
        "f() { a\n\techo 'x\n}\n'\n" + body(66) + "}\n",
        "f() { a; }; }\n" + body(70) + "}\n",
    ],
)
def test_brace_line_function_without_a_found_end_fails_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    rc, out = run_gate(tmp_path, capsys, text)
    assert rc == 1 and "cannot find the end of sh function f" in out and "OK" not in out, out


@pytest.mark.parametrize(
    "inner",
    [
        "\techo '{'\n",
        "\techo '}'\n",
        "\techo \\}\n",
        "\techo }\n",
        "\tx=${y%%\\}}\n",
        "\tcat <<EOF\n}\nEOF\n",
    ],
)
def test_brace_line_function_with_a_quoted_or_literal_brace_is_measured_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], inner: str
) -> None:
    text = "f() { a\n" + inner + '\t[ -n "$1" ] || { echo usage; return 1; }\n' + body(66) + "\tb; } 2>/dev/null\n"
    rc, out = run_gate(tmp_path, capsys, text)
    length = len(text.splitlines())
    assert rc == 1 and f"t.sh::f: {length} lines > hard limit 60" in out, out


@pytest.mark.parametrize(
    "inner",
    [
        "\tprintf '%s\\n' '}'\n",
        "\tsed 's/}//' x\n",
        "\tx=${y%%\\}}\n",
        "\techo \\}\n",
        "\tcase $1 in\n\t\t*}) b ;;\n\tesac\n",
    ],
)
def test_classic_function_with_a_quoted_close_brace_is_measured_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], inner: str
) -> None:
    text = "f() {\n" + inner + '\t[ -n "$1" ] || { echo usage; return 1; }\n' + body(66) + "}\n"
    rc, out = run_gate(tmp_path, capsys, text)
    length = len(text.splitlines())
    assert rc == 1 and f"t.sh::f: {length} lines > hard limit 60" in out, out


def test_classic_subshell_function_with_a_case_pattern_is_measured_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "f() (\n\tcase $1 in\n\t\t-h | --help )\n\t\t\techo h ;;\n\tesac\n\t( cd x; ls )\n" + body(60) + ")\n"
    rc, out = run_gate(tmp_path, capsys, text)
    assert rc == 1 and "t.sh::f: 67 lines > hard limit 60" in out, out


@pytest.mark.parametrize("one_line", ["f() { echo; } >&2", "f() { echo; } && x", "f() ( cd x; ls ) 2>&1"])
def test_one_line_function_with_a_redirection_is_not_measured_into_the_next_function(one_line: str) -> None:
    found = dict(cs.sh_functions(one_line + "\ng() {\n" + body(70) + "}\nh() { a\n\tb; }\n"))
    assert found == {"g": 72, "h": 2}, found
