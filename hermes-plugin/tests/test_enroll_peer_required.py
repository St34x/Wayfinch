# SPDX-License-Identifier: Apache-2.0
"""``EnrollRelay`` has no default peer check (N1): ``peer_allowed`` is required, the loopback helper is gone from the
plugin, and the plugin has no TCP site and one runner for ``/ws`` and ``/enroll``."""

from __future__ import annotations

import inspect
import re
import subprocess
from pathlib import Path

import pytest

from glasses.enroll import EnrollRelay, RateLimiter

PLUGIN = Path(__file__).resolve().parents[1] / "glasses"


def _sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(PLUGIN.glob("*.py"))}


def test_a_relay_without_peer_allowed_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="peer_allowed"):
        EnrollRelay(lambda: pytest.fail("no signer call"), limiter=RateLimiter())  # type: ignore[call-arg,arg-type,return-value]
    parameter = inspect.signature(EnrollRelay).parameters["peer_allowed"]
    assert parameter.default is inspect.Parameter.empty and parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_plugin_has_no_loopback_peer_helper_and_no_tcp_host_or_port() -> None:
    for name, text in _sources().items():
        assert "is_loopback_peer" not in text, name
        assert not re.search(r"TCPSite|TCPServer|host=|create_server\(", text), name
        assert not re.search(r"\b(settings|cfg|config)\.(host|port)\b", text), name


def test_the_plugin_builds_exactly_one_runner() -> None:
    found = [(name, m) for name, text in _sources().items() for m in re.findall(r"AppRunner\(|web\.Server\(", text)]
    runners = [name for name, text in _sources().items() if re.search(r"\b_Runner\(", text)]
    assert found == [] and runners == ["server_runner.py"]


def test_git_grep_finds_no_loopback_helper_in_the_plugin() -> None:
    out = subprocess.run(
        ["git", "grep", "-n", "is_loopback_peer", "--", "glasses"],
        cwd=PLUGIN.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode in (0, 1) and out.stdout == ""
