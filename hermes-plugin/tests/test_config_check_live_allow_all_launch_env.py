# SPDX-License-Identifier: Apache-2.0
"""``config check --live`` against real Hermes: the allow-all gate runs inside the launch ``.env`` overlay.

The gateway loads ``<home>/.env`` into its environment before it reads anything, so a ``GLASSES_ALLOWED_USERS``
that only the launch ``.env`` holds counts against an ``allow_all_users`` grant of ``config.yaml``: the glasses lane
is then restricted to that list, and the check passes. Without the overlay around the gate the same home fails.
"""

from __future__ import annotations

from pathlib import Path

from glasses import admin

from test_config_check_allow_all import _gate_lines, _grant
from test_config_check_edges import _live_scratch, _scratch
from test_config_check_env_secrets import _run


def _homes(tmp: str) -> tuple[Path, Path]:
    home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
    user.mkdir()
    return home, user


def test_real_live_an_allow_list_only_in_the_launch_dotenv_restricts_an_allow_all_grant():
    with _scratch() as tmp:
        home, user = _homes(tmp)
        _grant(home, {"allow_all_users": True})
        proc = _run(home, user, "GLASSES_ALLOWED_USERS=u1\n")
    assert proc.returncode == admin.EXIT_OK and _gate_lines(proc.stdout) == [], proc.stdout
    assert proc.stdout == "config check: OK (0 error(s), 0 warning(s))\n" and proc.stderr == "", proc.stderr


def test_real_live_the_same_grant_without_an_allow_list_still_fails():
    with _scratch() as tmp:
        home, user = _homes(tmp)
        _grant(home, {"allow_all_users": True})
        proc = _run(home, user, "UNRELATED=value\n")
    assert proc.returncode == admin.EXIT_ERRORS and len(_gate_lines(proc.stdout)) == 2, proc.stdout
