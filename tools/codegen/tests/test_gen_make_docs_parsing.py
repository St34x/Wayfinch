# SPDX-License-Identifier: Apache-2.0
"""Parsing rules of make_docs_model.py: make rules and `#:` lines, the section 10 gate table and `make <target>`
commands in Markdown code spans and fenced blocks."""

import make_docs_model as model
import pytest


def _messages(text: str, path: str = "mk/x.mk") -> list[str]:
    return [str(finding) for finding in model.collect([(path, text)]).findings]


def test_description_line_directly_above_rule_is_taken() -> None:
    inventory = model.collect([("mk/x.mk", "#: a — does a\na: b\n\t@true\n")])
    assert inventory.targets["a"] == model.Target("a", "mk/x.mk", 2, "does a")
    assert inventory.findings == []


def test_stacked_description_lines_cover_a_multi_target_rule() -> None:
    inventory = model.collect([("mk/x.mk", "#: a — does a\n#: b — does b\na b:\n\t@true\n")])
    assert [inventory.targets[name].description for name in "ab"] == ["does a", "does b"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("#: a — does a\n\na:\n", "mk/x.mk:1: `#: a` is not directly above a rule of `a`"),
        ("#: a — does a\n# note\na:\n", "mk/x.mk:1: `#: a` is not directly above a rule of `a`"),
        ("#: b — does b\na:\n", "mk/x.mk:1: `#: b` is not directly above a rule of `b`"),
        ("#: a - does a\na:\n", "mk/x.mk:1: malformed `#:` line"),
        ("#: a —\na:\n", "mk/x.mk:1: malformed `#:` line"),
        ("#: a — x\na:\n#: a — y\na: c\n", "mk/x.mk:3: `a` already has a `#:` description"),
        ("#: a — x\na:\n#: a — y\n", "mk/x.mk:3: `#: a` is not directly above a rule of `a`"),
        ("$(X):\n", "mk/x.mk:1: rule target `$(X)` is not a literal name"),
        ("TEST_TARGETS += $(MORE)\n", "mk/x.mk:1: TEST_TARGETS entry `$(MORE)` is not a literal target name"),
        ("TEST_TARGETS += ghost\n", "mk/x.mk:1: `make test` runs `ghost`, which no rule defines"),
    ],
)
def test_make_file_findings(text: str, message: str) -> None:
    assert any(found.startswith(message) for found in _messages(text)), _messages(text)


@pytest.mark.parametrize(
    "line",
    ["X := a:b", "Y ::= 1", "Z ?= c", "\trecipe: x", "ifeq ($(A),b:c)", "export A", "-include mk/*.mk", "# a: b"],
)
def test_lines_that_are_not_rules(line: str) -> None:
    assert model.rule_targets(line) is None


def test_special_targets_and_double_colon_rules() -> None:
    assert model.rule_targets(".PHONY: a b") == ([], " a b")
    assert model.rule_targets("a:: b") == (["a"], " b")


def test_continuation_lines_are_joined() -> None:
    inventory = model.collect([("Makefile", "TEST_TARGETS += a \\\n\tb\n#: a — x\na:\n#: b — y\nb:\n")])
    assert [name for name, _path, _line in inventory.entries] == ["a", "b"]
    assert inventory.findings == []


def test_test_rule_prerequisites_are_run_by_make_test() -> None:
    inventory = model.collect([("Makefile", "#: test — all\ntest: scan $(TEST_TARGETS)\n#: scan — s\nscan:\n")])
    assert inventory.entries == [("scan", "Makefile", 2)]


@pytest.mark.parametrize(
    "assignment",
    ["override TEST_TARGETS += a", "export TEST_TARGETS += a", "  TEST_TARGETS += a", "  override TEST_TARGETS = a"],
)
def test_prefixed_or_indented_test_targets_line_is_read(assignment: str) -> None:
    inventory = model.collect([("mk/x.mk", f"ifeq ($(CI),1)\n{assignment}\nendif\n#: a — x\na:\n")])
    assert inventory.entries == [("a", "mk/x.mk", 2)]
    assert inventory.findings == []


def test_space_indented_rule_is_a_rule() -> None:
    assert model.rule_targets("  hidden: dep") == (["hidden"], " dep")
    found = _messages("ifeq ($(CI),1)\n  hidden:\nendif\n")
    assert found == ["mk/x.mk:2: target `hidden` has no `#:` line"]
    inventory = model.collect([("mk/x.mk", "ifeq ($(CI),1)\n#: hidden — h\n  hidden:\nendif\n")])
    assert inventory.targets["hidden"] == model.Target("hidden", "mk/x.mk", 3, "h")


TABLE = """\
## 10. Gates

| Rule | Make target | State |
|---|---|---|
| A | `lint-a`, `type-a` | live |
| B | `tool.py --flag` | live at merge |
| C | `c` | maybe |
| D | `d` |
| E | `e` | planned |
## 11. Next
| X | `x` | live |
"""


def test_gate_rows_targets_states_and_form_findings() -> None:
    rows, findings = model.gate_rows(TABLE, "E.md")
    assert rows[0] == model.GateRow(5, "live", ("lint-a", "type-a"))
    assert rows[1] == model.GateRow(6, "live", ())
    assert [row.targets for row in rows] == [("lint-a", "type-a"), (), ("c",), ("e",)]
    assert [str(found) for found in findings] == [
        "E.md:7: State must start with `live` or `planned`",
        "E.md:8: row has 2 cells, the header 3",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "# No section\n",
        "## 10. Gates\n\n| A | B |\n|---|---|\n",
        "## 10. Gates\n\n| Make target | State |\n| `a` | live |\n| `b` | live |\n",
    ],
)
def test_gate_table_missing_cannot_be_checked(text: str) -> None:
    with pytest.raises(model.ModelError):
        model.gate_rows(text, "E.md")


@pytest.mark.parametrize(
    ("command", "targets"),
    [
        ("make test", ["test"]),
        ("make", []),
        ("make <target>", []),
        ("make -f mk/<name>.mk PY=.local/venv-dev/bin/python <target>", []),
        ("make -f mk/a.mk -C dir -j 4 a b", ["a", "b"]),
        ("VENV=x make venv PYTHON=python3.14", ["venv"]),
        ("$ make a # then b", ["a"]),
        ("make a >log 2>&1", ["a"]),
        ("cd x && make a; (make b) | tee", ["a", "b"]),
        ("mk/   make fragments", []),
        ("echo make a", []),
        ("make: *** [Makefile:1: a] Error 1", []),
    ],
)
def test_make_targets_in_a_command(command: str, targets: list[str]) -> None:
    assert model.make_targets(command) == targets


MARKDOWN = """\
Prose can make this work; `make a` and ``make b``.
A span over
two lines: `make
c`.

```sh
make d
```

~~~
make f
~~~

````md
```
make g
```
````
Unclosed `make h
"""


def test_commands_in_spans_and_fences_with_line_numbers() -> None:
    assert list(model.commands(MARKDOWN)) == [(7, "d"), (11, "f"), (16, "g"), (1, "a"), (1, "b"), (3, "c")]
