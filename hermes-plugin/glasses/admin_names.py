# SPDX-License-Identifier: Apache-2.0
"""What ``glasses-admin config check --live`` may print of the names Hermes returns (profile, tool and gate-problem
names), which come from config files and can hold anything: a URL, a credential, a ``${VAR}`` expansion of a
``.env`` value, a line break or an escape sequence that would forge a ``config check:`` line, a very long token.
A name is printed only if it comes from a set that exists without any environment value: a served profile
(an identifier), Hermes' toolset table, a gate line of the known shape. The check never compares config text with an
environment value or masks one (the output would depend on it: T1.196a). See the ``glasses.admin`` docstring."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .config import may_hold_credentials
from .config_core import is_key_like

MAX_LISTED_TOOLS = 10


def shown_name(name: str, quote: bool = False) -> str:
    """A profile name for a message: only the name of a served profile that is an identifier
    (``glasses.config.is_key_like``: a letter, then letters, digits, ``_``, ``-``, at most 32 characters). Route
    names that are not served arrive as ``hermes_env.HIDDEN_NAME`` (``hermes_facts``), which is no identifier. A name
    from a config file may be a URL, a credential, an expanded ``.env`` value or hold a line break or an escape
    sequence: it is not shown."""
    if not is_key_like(name):
        return "<a profile name not shown: only a served profile with a plain identifier name is printed>"
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
    """*lines* unchanged. ``glasses.admin.live_problems`` still passes ``HermesFacts.env_values`` through here; since
    T1.196a nothing is masked, because masking by an environment value makes the output depend on that value (a line
    holding config text would show whether the config text equals or contains it). Both go with that call."""
    return list(lines)
