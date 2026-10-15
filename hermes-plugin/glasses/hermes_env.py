# SPDX-License-Identifier: Apache-2.0
"""The environment side of ``glasses.hermes_facts``: ``<home>/.env`` read-only, the overlay it makes on
``os.environ`` while the pure config reads run, the masking of its values, and Hermes' log silence.

``<home>/.env`` is data, not code: a value of it (four characters or more) must never reach the output (``mask_values``
replaces it; ``name_unless_from_env`` hides a route name that equals any value; a value padded with white space
inside quotes is matched by neither, see ``name_unless_from_env``), and a
name that makes Hermes load code or touch another directory (``PATH``, ``LD_*``, ``PYTHON*``, ``HOME``, the plugin
and managed-overlay switches ...) is not overlaid (``overlay_name_ok``). The file must be a regular file of at most
``MAX_ENV_FILE_BYTES`` (no FIFO that blocks the read, no symlink out of the home, no huge file), else it counts as
absent. The T1.45 contract-test list in ``glasses.hermes_facts`` names the Hermes pieces used here.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import warnings
from collections.abc import Container, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config_access import GATEWAY_ALLOW_ALL_ENV, HERMES_TRUTHY

HOME_ENV = "HERMES_HOME"
MAX_ENV_FILE_BYTES = 1 << 20
MIN_MASKED_LENGTH = 4  # shorter values (``1``, ``en``) would blank out ordinary words in the output
MASK = "<.env value>"
HIDDEN_NAME = "<a name taken from the environment>"  # not an identifier: ``admin_names.shown_name`` never shows it
# Names Hermes reads to find code or directories (plugin discovery, the managed overlay, the dynamic loader, the
# interpreter, the home): never overlaid, whatever the file says.
NOT_OVERLAID_NAMES = frozenset({
    "PATH",
    "HOME",
    "USERPROFILE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "HERMES_BUNDLED_PLUGINS",
    "HERMES_ENABLE_PROJECT_PLUGINS",
    "HERMES_MANAGED_DIR",
})
NOT_OVERLAID_PREFIXES = ("LD_", "DYLD_", "PYTHON", "XDG_", "HERMES_PLUGINS_")


@contextmanager
def hermes_home_env(home: Path) -> Iterator[None]:
    """``HERMES_HOME=<home>`` in this process while the block runs (set before any Hermes import)."""
    before = os.environ.get(HOME_ENV)
    os.environ[HOME_ENV] = str(Path(home).absolute())
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(HOME_ENV, None)
        else:
            os.environ[HOME_ENV] = before


@contextmanager
def silenced_hermes() -> Iterator[None]:
    """No log record and no warning of Hermes reaches stderr while the block runs: they quote config values (a
    rejected route's profile name, an expanded ``${VAR}``)."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logging.disable(previous)


def overlay_name_ok(name: str, value: str) -> bool:
    """Whether ``name=value`` of a ``.env`` may be put on ``os.environ`` (not the home, not code-loading, and
    nothing ``os.environ`` itself refuses)."""
    if not name or name == HOME_ENV or "=" in name or "\x00" in name + value:
        return False
    return name not in NOT_OVERLAID_NAMES and not name.startswith(NOT_OVERLAID_PREFIXES)


def env_file_is_unsafe(home: Path) -> bool:
    """Whether ``<home>/.env`` exists but is not a regular file of at most ``MAX_ENV_FILE_BYTES``: opening it could
    block (FIFO), follow a link out of the home or read without end."""
    try:
        info = (Path(home) / ".env").lstat()
    except OSError:
        return False
    return not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENV_FILE_BYTES


def read_env_file(home: Path) -> dict[str, str]:
    """``<home>/.env`` as Hermes parses a profile's file (``agent.secret_scope.load_env_file``: read only, it
    never touches ``os.environ``), reduced to what may be overlaid (``overlay_name_ok``); absent, unreadable or
    unsafe (``env_file_is_unsafe``) = ``{}``. Hermes' own process-wide loader (``env_loader.load_hermes_dotenv``)
    normalises and rewrites the file: not used (T1.45).
    """
    path = Path(home) / ".env"
    if env_file_is_unsafe(home) or not path.is_file():
        return {}
    from agent.secret_scope import load_env_file

    return {k: v for k, v in load_env_file(path).items() if overlay_name_ok(k, v)}


@contextmanager
def environ_with(values: Mapping[str, str]) -> Iterator[None]:
    """``os.environ`` overlaid with *values* while the block runs; restored exactly afterwards."""
    saved = {name: os.environ.get(name) for name in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for name, before in saved.items():
            if before is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = before


def secret_values(*envs: Mapping[str, str]) -> frozenset[str]:
    """Every value of the given ``.env`` maps that ``mask_values`` hides (``MIN_MASKED_LENGTH`` or longer)."""
    return frozenset(v for env in envs for v in env.values() if len(v) >= MIN_MASKED_LENGTH)


def mask_values(text: str, values: Iterable[str]) -> str:
    """*text* with every occurrence of every value replaced by ``MASK`` (case-insensitive: Hermes lower-cases the
    profile names it returns). Values shorter than ``MIN_MASKED_LENGTH`` are not in *values* (``secret_values``)."""
    found = sorted(set(values), key=len, reverse=True)
    return re.sub("|".join(map(re.escape, found)), MASK, text, flags=re.IGNORECASE) if found else text


def name_unless_from_env(name: str, served: Container[str], *envs: Mapping[str, str]) -> str:
    """A routed profile *name* Hermes returned, or ``HIDDEN_NAME`` when it is not served and equals (any case) a
    value of one of *envs*: a ``${VAR}`` expanded to it. That covers what ``mask_values`` leaves out, a ``.env``
    value shorter than ``MIN_MASKED_LENGTH`` and a process-environment value. Only the whole name is compared:
    a value inside a longer name is hidden by ``mask_values`` (four characters or more), else shown. A value padded
    with white space inside quotes is not matched (Hermes strips the name, the comparison uses the raw value)."""
    word = name.lower()
    if name in served or not any(value.lower() == word for env in envs for value in env.values()):
        return name
    return HIDDEN_NAME


def allow_all_granted(layers: Any) -> bool:
    """Whether ``config.yaml`` grants ``allow_all_users`` the way Hermes' bridge reads it
    (``gateway.config_loader.bridge_core_env_settings``): top-level key, else ``gateway.allow_all_users``;
    ``true``/``1``/``yes`` in any case. *layers* is ``gateway.config_loader.read_yaml_layers`` of the home."""
    if not isinstance(layers, dict):
        return False
    value = layers.get("allow_all_users")
    gateway = layers.get("gateway")
    if value is None and isinstance(gateway, dict):
        value = gateway.get("allow_all_users")
    return str(value).lower() in HERMES_TRUTHY


def allow_all_bridge(layers: Any) -> dict[str, str]:
    """What the bridge would put on ``os.environ`` at gateway start: ``GATEWAY_ALLOW_ALL_USERS=true`` for a grant
    in ``config.yaml``, unless the variable is already set (an explicit env var wins). Call it inside the launch
    ``.env`` overlay: that is the environment the bridge sees."""
    if os.environ.get(GATEWAY_ALLOW_ALL_ENV) or not allow_all_granted(layers):
        return {}
    return {GATEWAY_ALLOW_ALL_ENV: "true"}
