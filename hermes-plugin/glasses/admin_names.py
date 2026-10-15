# SPDX-License-Identifier: Apache-2.0
"""What ``glasses-admin config check --live`` may print of the names Hermes returns (profile, tool and gate-problem
names), which come from config files and can hold anything: a URL, a credential, a ``${VAR}`` expansion of a
``.env`` value, a line break or an escape sequence that would forge a ``config check:`` line, a very long token.
See the ``glasses.admin`` docstring for the rules."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .config import may_hold_credentials
from .config_core import is_key_like
from .hermes_env import mask_values

MAX_LISTED_TOOLS = 10


def shown_name(name: str, quote: bool = False) -> str:
    """A profile name Hermes returned, for a message: only an identifier (``glasses.config.is_key_like``: a letter,
    then letters, digits, ``_``, ``-``, at most 32 characters). A name from a config file may be a URL, a
    credential, an expanded ``.env`` value or hold a line break or an escape sequence: it is not shown."""
    if not is_key_like(name):
        return "<a profile name not shown: it is not a plain identifier or it comes from the environment>"
    return repr(name) if quote else name


# The shapes of a line of ``glasses.gate.violations``: ``<scope>: <constant text naming variables>``.
GATE_LINE = re.compile(
    r"(?:process environment|active scope|profile [A-Za-z0-9][A-Za-z0-9_-]{0,63}): [ -~]+"
    r"|profile scopes unreadable \([A-Za-z0-9_]+\); [ -~]+"
)


def shown_problem(problem: str) -> str:
    """A gate problem line from Hermes (it names a profile): only a line of the known shape (printable ASCII, the
    profile an identifier), and none that could carry credentials. A look-alike character, a line break or an
    escape sequence in a profile name is not shown."""
    if may_hold_credentials(problem) or not GATE_LINE.fullmatch(problem):
        return "<a gate problem naming a value that is not shown: not a plain profile name and variable list>"
    return problem


def names_shown(names: Iterable[str], known: frozenset[str]) -> str:
    """Tool names for a message: those of Hermes' toolset table; any other name (a config value) is only counted."""
    given = set(names)
    listed = sorted(given & known)
    text = ", ".join(listed[:MAX_LISTED_TOOLS])
    if len(listed) > MAX_LISTED_TOOLS:
        text += f" (+{len(listed) - MAX_LISTED_TOOLS} more)"
    if len(given) > len(listed):
        text = ", ".join(filter(None, (text, f"<{len(given) - len(listed)} unknown toolset(s)>")))
    return text or "none"


def mask_lines(lines: Iterable[str], env_values: Iterable[str]) -> list[str]:
    """*lines* without any value of the ``.env`` files the facts were read with (a ``${VAR}`` in a config expands
    to one, and Hermes returns the result as a profile or tool name)."""
    values = frozenset(env_values)
    return [mask_values(line, values) for line in lines]
