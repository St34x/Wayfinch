# SPDX-License-Identifier: Apache-2.0
"""The registry takes a fingerprint as an exact ``str`` only (T1.184c, Security N3): a ``str`` subclass whose
``lower()`` returns an enrolled (or tombstoned) fingerprint is refused by ``norm_fp`` and by both answers of
``Registry.lookup``; ``edge_cert`` hands over the plain ``hexdigest()`` string (pinned in ``test_edge_cert_chain``)."""

from __future__ import annotations

import pytest

from glasses.registry import Registry
from glasses.registry_parse import norm_fp

FP_A = "ab" * 32
FP_B = "cd" * 32


def evil(target: str) -> str:
    class Evil(str):
        def lower(self) -> str:  # type: ignore[override]
            return target

    return Evil("00" * 32)


@pytest.fixture
def registry(tmp_path) -> tuple[Registry, str]:
    registry = Registry(tmp_path / "state")
    _active, token = registry.add("active", FP_A, ["main"])
    gone, _ = registry.add("gone", FP_B, ["main"])
    registry.revoke(gone.device_id)
    return registry, token


@pytest.mark.parametrize("where", ["norm_fp", "lookup-device", "lookup-tombstone"])
def test_a_str_subclass_is_refused_everywhere_a_fingerprint_is_read(registry, where):
    reg, token = registry
    assert reg.lookup(token, FP_A)[0] is not None and reg.lookup(token, FP_B) == (None, True)  # the real ones work
    if where == "norm_fp":
        assert norm_fp(evil(FP_A)) is None and norm_fp(FP_A.upper()) == FP_A
    elif where == "lookup-device":
        assert reg.lookup(token, evil(FP_A)) == (None, False)
    else:
        assert reg.lookup(token, evil(FP_B)) == (None, False)
