# SPDX-License-Identifier: Apache-2.0
"""POSIX sh function lengths for the size gate (check_size.py).

A function starts at "name()" with "{" or "(" on the same line or on a later line (blank and comment lines may come
between); here-document bodies are skipped. With nothing but a comment after the opener, the function ends at the
first later line that starts with "}"/")" at the opener line's indentation. With code after the opener, the opener
line and the lines after it are scanned with quoted strings, backslash escapes, "${...}" and comments blanked out:
"{" words count up and "}" words in command position (line start, after ";" or "&") count down, or every "(" and ")"
for a "(" body. The function ends on the line where the count returns to zero; if that is the opener line
("f() { a; } >&2") it is a one-line function and is not measured. Such a function is an error, never skipped or cut
short, when no line balances it, the count drops below zero, a quote spans lines or a "case" statement comes before
the end. A here-document started by a one-line function is not measured. Bash "function f" without "()" is not
recognised.
"""

import re

SH_START = re.compile(r"(?P<indent>[ \t]*)(?P<name>[A-Za-z_]\w*)[ \t]*\(\s*\)[ \t]*(?:(?P<open>[{(]).*|#.*)?")
SH_OPEN = re.compile(r"(?P<indent>[ \t]*)(?P<open>[{(]).*")
CLOSE = {"{": "}", "(": ")"}
BRACE_OPEN = re.compile(r"(?:^|(?<=[\s;&|(]))\{(?=\s|$)")
BRACE_CLOSE = re.compile(r"(?:^|[;&])[ \t]*\}(?=$|[\s;&|<>)])")
BLANKED = re.compile(r"""\\.?|'[^']*'|"(?:\\.|[^"\\])*"|\$\{[^{}]*\}|(?<!\S)#.*|(?P<open>['"])""")
CASE = re.compile(r"(?<![\w-])case(?![\w-])")
HEREDOC = re.compile(r"(?<!<)<<(?P<dash>-?)[ \t]*(?P<q>['\"]?)\\?(?P<word>[A-Za-z_]\w*)(?P=q)")

Lines = list[tuple[int, str]]


def sh_code_lines(lines: list[str]) -> Lines:
    """Return (index, line) of the lines outside here-document bodies."""
    code: Lines = []
    pending: list[tuple[str, bool]] = []
    for index, line in enumerate(lines):
        if pending:
            word, dash = pending[0]
            if (line.lstrip("\t") if dash else line) == word:
                pending.pop(0)
            continue
        code.append((index, line))
        if not line.lstrip().startswith("#"):
            pending = [(m["word"], m["dash"] == "-") for m in HEREDOC.finditer(line)]
    if pending:
        raise ValueError(f"unterminated here-document {pending[0][0]}")
    return code


def sh_functions(text: str) -> list[tuple[str, int]]:
    """Return (name, length in lines) of every multi-line function; ValueError if one has no recognised end."""
    code = sh_code_lines(text.splitlines())
    found: list[tuple[str, int]] = []
    for pos, (start, line) in enumerate(code):
        header = SH_START.fullmatch(line)
        opened = _opener(header, code[pos:]) if header else None
        if header is None or opened is None:
            continue
        try:
            end = _sh_end(*opened)
        except ValueError as err:
            raise ValueError(f"cannot find the end of sh function {header['name']} (line {start + 1}): {err}") from None
        if end != opened[1][0][0]:  # closed on its opener line: a one-line function is not measured
            found.append((header["name"], end - start + 1))
    return found


def _opener(header: re.Match[str], rest: Lines) -> tuple[re.Match[str], Lines] | None:
    """The opener match and the lines from the opener line on (blank and comment lines may follow a bare "name()")."""
    if header["open"]:
        return header, rest
    offset = next((n for n, (_, text) in enumerate(rest[1:], 1) if text.strip() and text.lstrip()[0] != "#"), 0)
    match = SH_OPEN.fullmatch(rest[offset][1]) if offset else None
    return (match, rest[offset:]) if match else None


def _sh_end(opener: re.Match[str], lines: Lines) -> int:
    """Index of the line (of `lines`, opener line first) that closes the body `opener` opens."""
    after = opener.string[opener.end("open") :].strip()
    if after and not after.startswith("#"):
        end = _balanced_end(opener, lines)
    else:
        close = opener["indent"] + CLOSE[opener["open"]]
        end = next((index for index, text in lines[1:] if text.startswith(close)), None)
    if end is None:
        raise ValueError("no closing line")
    return end


def _balanced_end(opener: re.Match[str], lines: Lines) -> int | None:
    """The line where the counted opens and closes balance, for a body with code after the opener."""
    depth = 0
    for n, (index, line) in enumerate(lines):
        rest = _blank(line[opener.start("open") :] if n == 0 else line)
        if rest is None or CASE.search(rest):
            raise ValueError(f"line {index + 1} has a quote spanning lines or a case statement")
        if opener["open"] == "{":
            depth += len(BRACE_OPEN.findall(rest)) - len(BRACE_CLOSE.findall(rest))
        else:
            depth += rest.count("(") - rest.count(")")
        if depth < 0:
            raise ValueError(f"more closes than opens at line {index + 1}")
        if depth == 0:
            return index
    return None


def _blank(line: str) -> str | None:
    """`line` with quoted text, escapes and "${...}" as "_" and the comment dropped; None if a quote stays open."""
    if any(m["open"] for m in BLANKED.finditer(line)):
        return None
    return BLANKED.sub(lambda m: "" if m[0].startswith("#") else "_", line)
