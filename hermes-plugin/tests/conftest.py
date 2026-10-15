# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the glasses plugin unit tests.

The unit tests run in the plain dev venv and must never import Hermes: the plugin package is
imported as the top-level package ``glasses`` from ``hermes-plugin/``, and every test is checked
afterwards for Hermes modules in ``sys.modules``.
"""

from __future__ import annotations

import importlib
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


# State-dir guard: every store a test builds must live under that test's tmp_path, and a store built outside a
# test function (a module- or session-scoped fixture) under the pytest session's base temp dir, so no test reads or
# writes a real $state_dir (one resolved from HERMES_HOME or the ~/.hermes default). (module, class) of each store
# whose constructor takes its directory first; a new store class is added here.
GUARDED_STORES = (
    ("glasses.crl_check", "CrlRecord"),
    ("glasses.registry", "Registry"),
)

# The running test's ``request`` while a test function's fixtures and body run; empty outside a test function.
_current_test: list[pytest.FixtureRequest] = []


class StateDirOutsideTmpError(AssertionError):
    """A test resolved a state dir outside its ``tmp_path`` (or outside the session base temp dir)."""


def check_under(state_dir: Path, root: Path, what: str = "the test's tmp_path") -> None:
    resolved, base = Path(state_dir).resolve(), root.resolve()
    if resolved != base and base not in resolved.parents:
        raise StateDirOutsideTmpError(f"state dir {resolved} is outside {what} {base}")


def _check_store_dir(state_dir: Path, basetemp: Path) -> None:
    if _current_test:
        check_under(state_dir, _current_test[-1].getfixturevalue("tmp_path"))
    else:
        check_under(state_dir, basetemp, "the pytest session base temp dir")


@pytest.fixture(scope="session", autouse=True)
def _state_dir_guard_session(tmp_path_factory):
    basetemp = tmp_path_factory.getbasetemp()
    with pytest.MonkeyPatch.context() as patch:
        for module, name in GUARDED_STORES:
            cls = getattr(importlib.import_module(module), name)

            def guarded(self, state_dir, *args, _original=cls.__init__, **kwargs):
                _check_store_dir(Path(state_dir), basetemp)
                _original(self, state_dir, *args, **kwargs)

            patch.setattr(cls, "__init__", guarded)
        _guard_signer(patch, basetemp)
        yield


# Signer guard: the socket of every SignerClient a test builds and every signer public file it resolves lie under the
# test's tmp_path, the session base temp dir, or a directory a running fake signer registered (fake_signer.SOCKET_DIRS;
# an AF_UNIX path holds at most 107 bytes, fewer than a tmp_path in a worktree leaves), so no test reaches a real
# signer.
def _check_signer_path(path: Path, basetemp: Path) -> None:
    from fake_signer import SOCKET_DIRS

    roots = [basetemp, *SOCKET_DIRS, *(test.getfixturevalue("tmp_path") for test in _current_test[-1:])]
    resolved = Path(path).resolve()
    if not any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots):
        raise StateDirOutsideTmpError(f"signer path {resolved} is outside the test's allowed temp dirs")


def _guard_signer(patch: pytest.MonkeyPatch, basetemp: Path) -> None:
    client = importlib.import_module("glasses.signer_client")

    def guarded_client(self, socket_path, *args, _original=client.SignerClient.__init__, **kwargs):
        _check_signer_path(socket_path, basetemp)
        _original(self, socket_path, *args, **kwargs)

    def guarded_public_dir(cfg, _original=client._public_dir):
        path = _original(cfg)
        _check_signer_path(path, basetemp)
        return path

    def guarded_public_file(cfg, name, _original=client.public_file):
        path = _original(cfg, name)
        _check_signer_path(path, basetemp)
        return path

    # The class __init__ and the module-global ``_public_dir`` (looked up at call time) cover names bound by
    # ``from glasses.signer_client import ...`` at collection; ``public_file`` also resolves the file itself.
    patch.setattr(client.SignerClient, "__init__", guarded_client)
    patch.setattr(client, "_public_dir", guarded_public_dir)
    patch.setattr(client, "public_file", guarded_public_file)


@pytest.fixture(autouse=True)
def _state_dir_guard(request, _state_dir_guard_session):
    _current_test.append(request)
    yield
    _current_test.pop()
