# SPDX-License-Identifier: Apache-2.0
"""Markdown layer of the docs gate (`check_docs.py`): the links and anchors of one document. Pure, no I/O.

Links: `[t](x)`, `![t](x)`, `[id]: x` and `<a href="x">`, each with its line number. Anchors: GitHub heading slugs
(markup dropped, lower case, punctuation removed, spaces to `-`, `-1`/`-2` for repeats) and HTML `id`/`name`.
Fenced code, code spans and HTML comments (also across lines) are ignored.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

_FENCE = re.compile(r"[ \t]*(`{3,}|~{3,})")
_HEADING = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*")
_SETEXT = re.compile(r" {0,3}(?:=+|-+)[ \t]*")
_BLOCK = re.compile(r"[ \t]*(?:[-*+>|]|\d+[.)])")
_COMMENT_OPEN, _COMMENT_CLOSE = "<!--", "-->"
_CODE_SPAN = re.compile(r"(`+)(?!`)(.*?)(?<!`)\1(?!`)")
_ESCAPE = re.compile(r"\\[\\`*_{}\[\]()#+\-.!<>|]")
_INLINE = re.compile(
    r"(?=\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*(<[^<>\n]*>|(?:[^\s()<>]|\([^\s()]*\))*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\))"
)
_REFDEF = re.compile(r" {0,3}\[[^\]]+\]:[ \t]*(<[^<>]*>|\S+)")
_HREF = re.compile(r"<a\s[^>]*?\bhref=\"([^\"]*)\"", re.IGNORECASE)
_ID = re.compile(r"<a\s[^>]*?\b(?:id|name)=\"([^\"]*)\"", re.IGNORECASE)
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_LINK_TEXT = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_TAG = re.compile(r"<[^>]+>")
_EMPHASIS = re.compile(r"(?<![^\W_])_+|_+(?![^\W_])")


@dataclass(frozen=True)
class Link:
    line: int
    target: str


@dataclass(frozen=True)
class Doc:
    links: tuple[Link, ...]
    anchors: frozenset[str]


def is_external(target: str) -> bool:
    """A target with a URL scheme (`https:`, `mailto:`) or a network path (`//host`): never checked."""
    return bool(_SCHEME.match(target)) or target[:2] == "//"


def _plain(text: str) -> str:
    return _EMPHASIS.sub("", _TAG.sub("", _LINK_TEXT.sub(r"\1", text)))


def slug(title: str) -> str:
    """GitHub heading id: code spans kept literally, other inline markup dropped, then punctuation removed."""
    parts: list[str] = []
    last = 0
    for span in _CODE_SPAN.finditer(title):
        parts += [_plain(title[last : span.start()]), span[2]]
        last = span.end()
    text = "".join([*parts, _plain(title[last:])]).strip().lower()
    return "".join(char for char in text if char.isalnum() or char in "_- ").replace(" ", "-")


def _uncomment(line: str, inside: bool) -> tuple[str, bool]:
    """`line` without HTML comment text, and whether a comment is still open at its end (`inside`: open before)."""
    kept: list[str] = []
    rest = line
    while rest:
        mark = _COMMENT_CLOSE if inside else _COMMENT_OPEN
        found = rest.find(mark)
        if found < 0:
            kept += [] if inside else [rest]
            break
        kept += [] if inside else [rest[:found]]
        rest, inside = rest[found + len(mark) :], not inside
    return "".join(kept), inside


def _outside_code(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Every line with its number; fenced code (fences included) comes back empty, HTML comments removed."""
    fence = ""
    comment = False
    for number, line in enumerate(lines, 1):
        if fence:
            stripped = line.strip()
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                fence = ""
            yield number, ""
            continue
        opened = None if comment else _FENCE.match(line)
        fence = opened[1] if opened else ""
        text, comment = ("", False) if opened else _uncomment(line, comment)
        yield number, text


def _title(line: str, previous: str) -> str | None:
    heading = _HEADING.fullmatch(line)
    if heading:
        return heading[2] or ""
    setext = previous and _SETEXT.fullmatch(line) and not _BLOCK.match(previous)
    return previous.strip() if setext and not _HEADING.fullmatch(previous) else None


def _line_links(text: str) -> list[str]:
    plain = _ESCAPE.sub("", _CODE_SPAN.sub("", text))
    found = [match[1] for match in _INLINE.finditer(plain)]
    definition = _REFDEF.match(plain)
    found += [definition[1]] if definition else []
    found += _HREF.findall(plain)
    return [target[1:-1] if target.startswith("<") else target for target in found]


def _anchors(titles: list[str], ids: list[str]) -> frozenset[str]:
    seen: dict[str, int] = {}
    anchors = set(ids)
    for title in titles:
        base = slug(title)
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchors.add(f"{base}-{count}" if count else base)
    return frozenset(anchors)


def parse(text: str) -> Doc:
    """Links (with line numbers) and anchors of one Markdown document."""
    links: list[Link] = []
    titles: list[str] = []
    ids: list[str] = []
    previous = ""
    for number, line in _outside_code(text.splitlines()):
        title = _title(line, previous)
        if title is not None:
            titles.append(title)
        links += [Link(number, target) for target in _line_links(line)]
        ids += _ID.findall(_CODE_SPAN.sub("", line))
        previous = "" if title is not None else line.strip()
    return Doc(tuple(links), _anchors(titles, ids))
