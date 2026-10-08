#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Docs gate (T1.134; ENGINEERING.md section 9). Stdlib only; never fetches a URL. Over every tracked `*.md` (any
case of the extension), parsed by `md_links.py`:
- links: each relative target (`[t](x)`, `![t](x)`, `[id]: x`, `<a href="x">`; `/x` = from the root) is a tracked
  file or a directory holding one, inside the repository; targets with a URL scheme or `//` are external, skipped;
- anchors: `#a` on a `*.md`, a directory (its README) or alone (same file) is a GitHub heading slug or an HTML
  `id`/`name` (rules in `md_links.py`);
- private folder (`PRIVATE_DIR` of `check_hygiene.py`, the one folder a public export lacks): a public document (any
  other, `.github/` included) never links into it; its knowledge-base index (`PRIVATE_INDEX`) is required as soon as
  any tracked file lies in it (a public export without the folder skips it);
- reachability: every public document is reachable from `ROOT_DOC` by links between public documents; a directory
  link reaches its README;
- indexes: every `INDEX.md` links each other `*.md` of its directory but `README.md`, in any order.
Usage: check_docs.py (inside the repository).
Exit: 0 clean, 1 findings (`file:line: ...` on stderr), 2 cannot check (git error, no tracked `*.md`, a required
file untracked, a tracked document missing, a symlink or not UTF-8).
"""

import posixpath
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from check_hygiene import PRIVATE_DIR
from md_links import Doc, is_external, parse

ROOT_DOC = "README.md"
REQUIRED = (ROOT_DOC, "docs/troubleshooting/INDEX.md")
INDEX_NAME = "INDEX.md"
README_NAME = "README.md"
PRIVATE_INDEX = f"{PRIVATE_DIR}kb/{INDEX_NAME}"
UNREACHABLE = f"not reachable by links from {ROOT_DOC} (index it in docs/README.md)"
INTO_PRIVATE = "links into the private folder, which a public export lacks"


class DocsError(Exception):
    """The check could not run (git failure, missing or unreadable document): exit 2, never a pass."""


@dataclass(frozen=True)
class Result:
    findings: list[str]
    files: int
    links: int
    indexes: int


def resolve(source: str, target: str) -> tuple[str, str]:
    """Repository path (`.` = the root, `..` prefix = outside) and anchor of a relative link target."""
    path, _, fragment = target.partition("#")
    path = unquote(path.partition("?")[0])
    if not path:
        return source, unquote(fragment)
    base = "" if path.startswith("/") else posixpath.dirname(source)
    return posixpath.normpath(posixpath.join(base, path.lstrip("/"))), unquote(fragment)


def is_public(name: str) -> bool:
    """Outside the private folder (the folder itself counts as private)."""
    return not f"{name}/".startswith(PRIVATE_DIR)


def required(tracked: Iterable[str]) -> tuple[str, ...]:
    """Files that must be tracked: `REQUIRED`, plus `PRIVATE_INDEX` once anything is tracked in the private folder."""
    return REQUIRED + ((PRIVATE_INDEX,) if any(not is_public(name) for name in tracked) else ())


@dataclass(frozen=True)
class Tree:
    tracked: frozenset[str]
    dirs: frozenset[str]
    docs: dict[str, Doc]

    def doc_at(self, path: str) -> str | None:
        """The document a link to `path` reaches: the file itself or a directory's README."""
        if path in self.tracked:
            return path if path in self.docs else None
        readme = README_NAME if path == "." else f"{path}/{README_NAME}"
        return readme if readme in self.docs else None


def _anchor_problem(tree: Tree, path: str, fragment: str) -> str | None:
    if not fragment or (path in tree.tracked and path not in tree.docs):
        return None
    doc = tree.doc_at(path)
    if doc is not None and fragment in tree.docs[doc].anchors:
        return None
    return f"no heading or id '{fragment}'"


def check_link(tree: Tree, source: str, target: str) -> tuple[str | None, str | None]:
    """(finding, reached document) of one relative link; the finding has no `file:line:` prefix."""
    if not target:
        return "empty link target", None
    path, fragment = resolve(source, target)
    if path == ".." or path.startswith("../"):
        return f"broken link '{target}': leaves the repository", None
    if is_public(source) and not is_public(path):
        return f"'{target}' {INTO_PRIVATE}", None
    if path not in tree.tracked and path not in tree.dirs:
        return f"broken link '{target}': no tracked file or directory {path}", None
    problem = _anchor_problem(tree, path, fragment)
    if problem:
        return f"broken anchor '{target}': {problem}", None
    return None, tree.doc_at(path)


def link_findings(tree: Tree) -> tuple[list[tuple[str, int, str]], dict[str, set[str]], int]:
    """Broken links and anchors, the documents each document links to, and the number of relative links."""
    findings: list[tuple[str, int, str]] = []
    edges: dict[str, set[str]] = {name: set() for name in tree.docs}
    count = 0
    for source, doc in tree.docs.items():
        for link in (link for link in doc.links if not is_external(link.target)):
            count += 1
            problem, reached = check_link(tree, source, link.target)
            if problem:
                findings.append((source, link.line, problem))
            if reached:
                edges[source].add(reached)
    return findings, edges, count


def unreachable(docs: list[str], edges: dict[str, set[str]]) -> list[str]:
    """Public documents no chain of links between public documents reaches from `ROOT_DOC`."""
    seen = {ROOT_DOC}
    queue = [ROOT_DOC]
    while queue:
        for target in edges.get(queue.pop(), set()):
            if is_public(target) and target not in seen:
                seen.add(target)
                queue.append(target)
    return [name for name in docs if is_public(name) and name not in seen]


def unlisted(docs: list[str], edges: dict[str, set[str]]) -> list[tuple[str, str]]:
    """(entry, index) for every `*.md` beside an `INDEX.md` that the index does not link."""
    missing: list[tuple[str, str]] = []
    for index in (name for name in docs if posixpath.basename(name) == INDEX_NAME):
        folder = posixpath.dirname(index)
        for name in docs:
            entry = posixpath.dirname(name) == folder and posixpath.basename(name) not in {INDEX_NAME, README_NAME}
            if entry and name not in edges[index]:
                missing.append((name, index))
    return missing


def _git(top: Path | None, *args: str) -> str:
    command = ["git", *(["-C", str(top)] if top is not None else []), *args]
    try:
        done = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            command, capture_output=True, encoding="utf-8", errors="surrogateescape", check=False
        )
    except OSError as exc:
        raise DocsError(f"cannot run git: {exc}") from exc
    if done.returncode != 0:
        raise DocsError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def _read(top: Path, name: str) -> str:
    path = top / name
    if path.is_symlink() or not path.is_file():
        raise DocsError(f"{name}: tracked but missing, a symlink or not a regular file")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DocsError(f"{name}: unreadable or not UTF-8 ({exc})") from exc


def load(top: Path) -> Tree:
    """Tracked files, their directories and every parsed tracked `*.md` of the repository at `top`."""
    if Path(_git(top, "rev-parse", "--show-toplevel").strip()).resolve() != top.resolve():
        raise DocsError(f"{top}: not the top level of a git repository")
    tracked = frozenset(name for name in _git(top, "-c", "core.quotePath=false", "ls-files", "-z").split("\0") if name)
    names = sorted(name for name in tracked if name.lower().endswith(".md"))
    if not names:
        raise DocsError("no tracked *.md files")
    for name in required(tracked):
        if name not in tracked:
            raise DocsError(f"{name}: required, but missing or untracked")
    dirs = {str(parent) for name in tracked for parent in PurePosixPath(name).parents}
    return Tree(tracked, frozenset(dirs), {name: parse(_read(top, name)) for name in names})


def check_tree(top: Path) -> Result:
    """All findings as `file:line: message`, sorted, plus the counts for the OK line."""
    tree = load(top)
    found, edges, links = link_findings(tree)
    docs = list(tree.docs)
    missing = unlisted(docs, edges)
    listed_nowhere = {name for name, _index in missing}  # one finding each: the index is where the link belongs
    found += [(name, 1, UNREACHABLE) for name in unreachable(docs, edges) if name not in listed_nowhere]
    found += [(name, 1, f"not listed in {index}") for name, index in missing]
    findings = [f"{name}:{line}: {message}" for name, line, message in sorted(found)]
    indexes = sum(posixpath.basename(name) == INDEX_NAME for name in docs)
    return Result(findings, len(docs), links, indexes)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        sys.stderr.write("usage: check_docs.py   (no arguments)\n")
        return 2
    try:
        result = check_tree(Path(_git(None, "rev-parse", "--show-toplevel").strip()))
    except DocsError as exc:
        sys.stderr.write(f"check-docs: {exc}\n")
        return 2
    for finding in result.findings:
        sys.stderr.write(f"{finding}\n")
    if result.findings:
        sys.stderr.write(f"check-docs: {len(result.findings)} finding(s)\n")
        return 1
    sys.stdout.write(f"check-docs: OK ({result.files} docs, {result.links} relative links, {result.indexes} indexes)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
