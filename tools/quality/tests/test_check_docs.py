# SPDX-License-Identifier: Apache-2.0
"""Unit tests of tools/quality/md_links.py, the Markdown layer of check_docs.py (T1.134): parsing, link extraction
and heading slugs."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "md_links.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("md_links_unit_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cd = _load()


def _links(text: str) -> list[tuple[int, str]]:
    return [(link.line, link.target) for link in cd.parse(text).links]


# --- heading slugs (GitHub rules) ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("10. Gates", "10-gates"),
        ("T1.134 — Docs gate: links, anchors", "t1134--docs-gate-links-anchors"),
        ("Public API", "public-api"),
        ("`__init__.py` and _emphasis_", "__init__py-and-emphasis"),
        ("snake_case stays", "snake_case-stays"),
        ("[Linked](other.md) text", "linked-text"),
        ("**Bold** and `code`", "bold-and-code"),
        ("Ünïcode Straße", "ünïcode-straße"),
        ("What's new?", "whats-new"),
        ("Closing hashes ##", "closing-hashes"),
    ],
)
def test_heading_slug(title: str, slug: str) -> None:
    assert cd.parse(f"# {title}\n").anchors == frozenset({slug})


def test_duplicate_headings_get_a_counter() -> None:
    assert cd.parse("# A\n\n## A\n\n### A\n").anchors == frozenset({"a", "a-1", "a-2"})


def test_setext_heading_is_an_anchor() -> None:
    assert cd.parse("Big title\n=========\n\nSmall one\n---\n").anchors == frozenset({"big-title", "small-one"})


def test_thematic_break_after_blank_is_not_a_heading() -> None:
    assert cd.parse("text\n\n---\n").anchors == frozenset()


def test_html_id_and_name_are_anchors() -> None:
    assert cd.parse('<a id="here"></a>\n<a name="there"></a>\n').anchors == frozenset({"here", "there"})


def test_headings_in_fenced_code_are_not_anchors() -> None:
    text = "# Real\n\n```sh\n# comment\n```\n\n~~~\n## Also code\n~~~\n"
    assert cd.parse(text).anchors == frozenset({"real"})


# --- links -----------------------------------------------------------------------------------------------------------


def test_inline_link_image_and_title_forms() -> None:
    text = "See [a](a.md), ![img](pic.png \"Title\") and [b](<with space.md>).\n[c](c.md#part 't')\n"
    assert _links(text) == [(1, "a.md"), (1, "pic.png"), (1, "with space.md"), (2, "c.md#part")]


def test_link_in_link_text_and_badge_forms() -> None:
    assert _links("[![badge](b.svg)](target.md)\n[`code` text](x.md)\n") == [
        (1, "target.md"),
        (1, "b.svg"),
        (2, "x.md"),
    ]


def test_reference_definition_and_html_href() -> None:
    text = '[ref]: docs/a.md\n  [other]: <b.md> "t"\n<a href="c.md#x">c</a>\n'
    assert _links(text) == [(1, "docs/a.md"), (2, "b.md"), (3, "c.md#x")]


def test_links_in_fenced_code_and_code_spans_are_ignored() -> None:
    text = "```\n[a](missing.md)\n```\n   ```md\n[b](missing.md)\n   ```\nUse `[c](missing.md)` here.\n"
    assert _links(text) == []


def test_parenthesis_in_a_table_cell_is_not_a_link() -> None:
    text = '| call | result |\n|---|---|\n| **`globals()["__builtins__"]["open"](…)`** | passes |\n| f (…) | ok |\n'
    assert _links(text) == []


def test_html_comment_is_ignored() -> None:
    assert _links("<!-- [old](gone.md) -->\n[new](here.md)\n") == [(2, "here.md")]


def test_escaped_brackets_are_not_links() -> None:
    assert _links("\\[not\\](a link)\n") == []


def test_line_numbers_count_code_lines() -> None:
    assert _links("a\n```\nb\n```\n\n[x](y.md)\n") == [(6, "y.md")]


def test_parentheses_inside_a_target() -> None:
    assert _links("[w](page_(x).md)\n") == [(1, "page_(x).md")]
