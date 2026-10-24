# SPDX-License-Identifier: Apache-2.0
"""The registry's size cap is judged from ``st_size``: a file over the cap is refused without being read.

``MAX_FILE_BYTES`` bounds memory, so the check must come before ``read()``. A cap applied to the length of the
bytes already read would refuse the same files but only after loading them (a multi-gigabyte sparse file).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from glasses import registry as reg
from glasses import registry_parse as rp

FP_A, FP_B = "ab" * 32, "cd" * 32
NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


class _NeverRead:
    """Wraps the file object ``Registry._read`` opens; any ``read`` is a failure of the test."""

    def __init__(self, fh):
        self._fh = fh

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def fileno(self):
        return self._fh.fileno()

    def read(self, *args):
        raise AssertionError("a file over the size cap was read")


@pytest.mark.parametrize("extra", [1, 4096, 1 << 28])
@pytest.mark.parametrize("operation", ["list", "lookup-device", "lookup-tombstone", "add"])
def test_a_file_over_the_cap_is_refused_from_its_size_without_reading_it(tmp_path, monkeypatch, operation, extra):
    registry = reg.Registry(tmp_path / "glasses", clock=lambda: NOW)
    registry.add("Phone", FP_A, ["work"])
    path = tmp_path / "glasses" / reg.FILE_NAME
    with path.open("r+b") as fh:
        fh.truncate(rp.MAX_FILE_BYTES + extra)  # sparse: the size is reported, nothing is stored
    real_fdopen = os.fdopen
    monkeypatch.setattr(os, "fdopen", lambda fd, mode="r", *a, **k: _NeverRead(real_fdopen(fd, mode, *a, **k)))
    calls = {
        "list": registry.list,
        "lookup-device": lambda: registry.lookup("t" * 43, FP_A)[0],
        "lookup-tombstone": lambda: registry.lookup("x" * 43, FP_A)[1],
        "add": lambda: registry.add("Other", FP_B, []),
    }
    with pytest.raises(reg.RegistryCorruptError, match=str(rp.MAX_FILE_BYTES)):
        calls[operation]()
