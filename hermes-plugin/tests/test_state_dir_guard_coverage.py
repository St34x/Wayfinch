# SPDX-License-Identifier: Apache-2.0
"""The state-dir guard (SAT-CA-16) is an autouse conftest fixture, so it also covers a test module that does not opt
in: a test that builds a ``Registry`` or a ``CrlRecord`` outside its ``tmp_path``, or resolves the default Hermes home
through the CLI, fails. This module deliberately has no ``pytestmark`` and imports no guard. Neither constructor does
I/O, so the probes below touch nothing outside ``tmp_path``."""

from __future__ import annotations

import io

import pytest

from glasses import admin
from glasses.crl_check import CrlRecord
from glasses.registry import Registry

OUTSIDE_TMP = "outside the test's tmp_path"  # message of the conftest state-dir guard
STORES = (Registry, CrlRecord)


@pytest.mark.parametrize("store", STORES)
def test_a_store_outside_tmp_path_fails_in_a_module_without_opt_in(tmp_path, store):
    with pytest.raises(AssertionError, match=OUTSIDE_TMP):
        store(tmp_path.parent / "outside-state")
    with pytest.raises(AssertionError, match=OUTSIDE_TMP):
        store(tmp_path / ".." / "x")
    store(tmp_path / "state")


def test_the_guard_fails_a_cli_run_that_resolves_the_default_hermes_home(tmp_path, monkeypatch):
    """No HERMES_HOME and no state_dir: ``crl --check`` resolves ~/.hermes/glasses for its rollback record, and the
    guard fails the test before anything is created there."""
    home = tmp_path.parent / f"{tmp_path.name}-home"
    monkeypatch.setenv("HOME", str(home))
    config = tmp_path / "config.yaml"
    lines = ["gateway:", "  platforms:", "    glasses:", "      extra:", "        signer:"]
    config.write_text("\n".join([*lines, f"          public_dir: '{tmp_path}'"]) + "\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=OUTSIDE_TMP):
        admin.main(["crl", "--check", "--config", str(config)], env={}, out=io.StringIO())
    assert not home.exists()


@pytest.fixture(scope="module")
def stores_built_for_the_whole_module(tmp_path_factory):
    """One store of each kind built once per module, outside the session base temp dir (no I/O)."""
    results = []
    for store in STORES:
        try:
            store(tmp_path_factory.getbasetemp().parent / "module-wide-state")
        except AssertionError:
            results.append("refused by the guard")
        else:
            results.append("built")
    return results


def test_a_store_built_in_a_module_scoped_fixture_outside_the_session_temp_dir_is_refused(
    stores_built_for_the_whole_module,
):
    assert stores_built_for_the_whole_module == ["refused by the guard"] * len(STORES)
