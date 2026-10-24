# SPDX-License-Identifier: Apache-2.0
"""The outbox under a group-writable umask (0002, the default for users with their own group on Ubuntu): a state
directory someone else made with a plain ``mkdir`` comes out 0775 and is refused, while the one the outbox makes
itself is 0700 whatever the umask. The modes here come from the umask alone, never from a ``chmod``."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from outbox_helpers import store

from glasses.outbox import FILE_NAME, OutboxError

DIRECTORY_TEXT = "its directory must belong to the plugin user, not be group- or world-writable"


@contextmanager
def umask(mask: int) -> Iterator[None]:
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def test_a_state_dir_made_by_a_plain_mkdir_under_umask_0002_is_group_writable_and_refused(tmp_path):
    state = tmp_path / "state"
    with umask(0o002):
        state.mkdir()
        box = store(state)
        with pytest.raises(OutboxError, match=DIRECTORY_TEXT):
            box.open()
    assert stat.S_IMODE(state.stat().st_mode) == 0o775
    assert not (state / FILE_NAME).exists()
    box.close()


def test_the_state_dir_the_outbox_creates_itself_under_umask_0002_is_0700_and_accepted(tmp_path):
    state = tmp_path / "state"
    with umask(0o002):
        box = store(state)
        box.open()
    try:
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert stat.S_IMODE((state / FILE_NAME).stat().st_mode) == 0o600
    finally:
        box.close()
