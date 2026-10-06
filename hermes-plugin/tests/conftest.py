# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the glasses plugin unit tests.

The unit tests run in the plain dev venv and must never import Hermes: the plugin package is
imported as the top-level package ``glasses`` from ``hermes-plugin/``, and every test is checked
afterwards for Hermes modules in ``sys.modules``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = PLUGIN_ROOT / "glasses"

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# Top-level modules of the Hermes code base that a unit test must not pull in.
HERMES_MODULES = ("gateway", "hermes_cli", "hermes_constants", "run_agent", "tools", "agent")


def hermes_modules_loaded() -> list[str]:
    return sorted(name for name in sys.modules if name.split(".", 1)[0] in HERMES_MODULES)


@pytest.fixture(autouse=True)
def _no_hermes_imports():
    before = set(hermes_modules_loaded())
    yield
    leaked = sorted(set(hermes_modules_loaded()) - before)
    assert not leaked, f"unit test imported Hermes modules: {leaked}"


@pytest.fixture
def plugin_dir() -> Path:
    return PLUGIN_DIR


# Gate variables the plugin reads (glasses.config.gate_violations); the host env must not leak in.
GATE_ENV = ("GLASSES_ALLOW_ALL_USERS", "GLASSES_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS")


@pytest.fixture(autouse=True)
def _clean_gate_env(monkeypatch):
    for name in GATE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_allow_all(monkeypatch):
    """The allow-all switch is never set in a correct deployment; make sure the host env does not leak in."""
    monkeypatch.delenv("GLASSES_ALLOW_ALL_USERS", raising=False)
