# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b), the registry's answer types: the tombstone is the wipe marker only for an exact ``True`` and a
device is a ``Device`` only for ``type(device) is Device``. A stand-in whose ``isinstance`` claims lie
(``Mock(spec=...)``) is a registry failure (``401``), never the ``403`` the phone wipes on."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from server_auth_helpers import FP, creds, deps_for, get, make_app, refusal

from glasses.registry import Device


class Answers:
    """A registry stand-in that answers *answer* to every lookup."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def lookup(self, token: Any, fingerprint: Any) -> Any:
        return self.answer


def status_of(answer: Any) -> int:
    handled: list[str] = []
    reply = get(make_app(deps_for(Answers(answer)), handled), creds("t" * 43))
    assert handled == []  # never admitted
    return reply.status


def test_a_tombstone_that_only_claims_to_be_a_bool_is_not_the_wipe_marker():
    assert status_of((None, Mock(spec=bool))) in (401, 503)


def test_a_device_that_only_claims_to_be_a_device_is_not_403_whatever_its_revoked_at():
    assert status_of((Mock(spec=Device, revoked_at=Mock()), False)) in (401, 503)


def test_a_device_subclass_is_not_a_device():
    class Sub(Device):
        pass

    assert status_of((Sub("d1", "n", FP, (), (), "t", None, None), False)) == 401


def test_the_exact_tombstone_and_the_real_device_still_get_their_answers():
    assert status_of((None, True)) == 403
    assert status_of((None, False)) == 401
    revoked = Device("d1", "n", FP, (), (), "t", None, "2026-10-17T00:00:00Z")
    assert status_of((revoked, False)) == 403


def test_the_403_body_is_the_marker_only_for_the_exact_tombstone():
    handled: list[str] = []
    reply = get(make_app(deps_for(Answers((None, True))), handled), creds("t" * 43))
    assert refusal(reply, 403, "device_revoked")
