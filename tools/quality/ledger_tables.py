# SPDX-License-Identifier: Apache-2.0
"""Markdown tables of docs/dependencies.md for the ledger gate (check_ledger.py): each table with the `## ` section
it stands in, the lookup of one table by section and columns, and the raw reader of the gate's source files."""

from pathlib import Path
from typing import NamedTuple

LEDGER = "docs/dependencies.md"


class CannotCheckError(Exception):
    """A source file, section, table or statement the gate needs is missing or unreadable."""


def read(root: Path, name: str) -> str:
    """The UTF-8 text of `root/name` without universal newlines: a CR stays a CR, as sh reads it."""
    try:
        with (root / name).open(encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise CannotCheckError(f"{name}: cannot read ({exc})") from exc


class Table(NamedTuple):
    section: str
    header: list[str]
    rows: list[tuple[int, list[str]]]

    def cell(self, row: list[str], column: str) -> str:
        index = self.header.index(column)
        return row[index].strip("`") if index < len(row) else ""


def tables(text: str) -> list[Table]:
    """Every markdown table with the `## ` section it stands in; rows are (line number, cells)."""
    found: list[Table] = []
    section, previous = "", ""
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            section = line[3:].strip()
        if not line.startswith("|"):
            previous = line
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not previous.startswith("|"):
            found.append(Table(section, cells, []))
        elif not set("".join(cells)) <= set("-: "):
            found[-1].rows.append((number, cells))
        previous = line
    return found


def table(all_tables: list[Table], section: str, columns: tuple[str, ...]) -> Table:
    """The table of `section` whose header holds columns[0]; it must hold every column and at least one row."""
    if not any(t.section == section for t in all_tables):
        raise CannotCheckError(f"{LEDGER}: section '## {section}' not found")
    match = [t for t in all_tables if t.section == section and columns[0] in t.header]
    if not match:
        raise CannotCheckError(f"{LEDGER}: '## {section}' has no table with a '{columns[0]}' column")
    missing = [c for c in columns if c not in match[0].header]
    if missing:
        raise CannotCheckError(f"{LEDGER}: the '## {section}' table has no '{missing[0]}' column")
    if not match[0].rows:
        raise CannotCheckError(f"{LEDGER}: the '## {section}' table has no rows")
    return match[0]
