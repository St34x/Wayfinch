# SPDX-License-Identifier: Apache-2.0
"""``edge_helpers.listening_tcp_inodes`` backs the "nothing listens on TCP" assertions: when no TCP table can be read
it must fail, never return an empty set (that would let ``listening_tcp_inodes() <= before`` pass without a check)."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest
from edge_helpers import listening_tcp_inodes


def test_an_unreadable_tcp_table_is_an_error_not_an_empty_set(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreadable(self: Path, *_args: object, **_kwargs: object) -> str:
        raise OSError(errno.EACCES, "unreadable")

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(RuntimeError, match="no TCP table"):
        listening_tcp_inodes()


def test_a_readable_table_gives_this_processs_listening_sockets() -> None:
    assert isinstance(listening_tcp_inodes(), set)
