# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b), the answer types of the injected revocation check: only an exact ``True`` is the ``403`` marker
and only an exact ``False`` admits, at the upgrade and for every frame. Objects whose ``__eq__`` or ``__bool__`` lie, an
integer or string subclass, an awaitable that is not a coroutine, and a check that is an ``async def`` are the fixed
``503`` (or a dropped frame) and leave no value, no traceback and no un-awaited coroutine behind."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import Mock

import pytest
from server_auth_helpers import creds, deps_for, enrolled, get, make_app, refusal
from server_helpers import MSG_IN, PING, wiring
from server_raw_ws import TEXT, frame_bytes, reply, say_hello, session

from glasses.server_auth import AuthDeps

SECRET = "secret-value-that-must-stay-out-of-every-log-line"


class LyingEq:
    def __eq__(self, other: object) -> bool:
        return True

    def __bool__(self) -> bool:
        return True

    def __hash__(self) -> int:
        return hash(True)

    def __repr__(self) -> str:
        return SECRET


class ArrayLike:
    """What a numpy scalar or array answers: truthy, equal to everything, ``is``-distinct from ``True``."""

    def __eq__(self, other: object) -> ArrayLike:  # type: ignore[override]
        return self

    def __bool__(self) -> bool:
        return True

    def __hash__(self) -> int:
        return 1

    def __index__(self) -> int:
        return 1

    def __repr__(self) -> str:
        return SECRET


class BoolRaises:
    def __bool__(self) -> bool:
        raise RuntimeError(SECRET)

    __hash__ = None  # type: ignore[assignment]


class OneAsInt(int):
    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = int.__hash__


class TrueAsStr(str):
    def __eq__(self, other: object) -> bool:
        return True

    __hash__ = str.__hash__


class ClaimsToBeBool:
    """``isinstance(x, bool)`` is True for it (a ``__class__`` property, as ``Mock(spec=bool)`` has)."""

    @property
    def __class__(self) -> type:  # type: ignore[override]
        return bool

    def __bool__(self) -> bool:
        return True


def not_a_coroutine() -> Any:
    return asyncio.get_running_loop().create_future()


async def wrongly_async(device_id: str) -> bool:
    return False


ODD_ANSWERS: dict[str, Callable[[str], Any]] = {
    "eq-lies": lambda _id: LyingEq(),
    "array-like": lambda _id: ArrayLike(),
    "bool-raises": lambda _id: BoolRaises(),
    "int-subclass": lambda _id: OneAsInt(1),
    "str-subclass": lambda _id: TrueAsStr("True"),
    "future": lambda _id: not_a_coroutine(),
    "async-def": wrongly_async,
    "sleeping-coroutine": lambda _id: asyncio.sleep(0, result=True),
    "generator": lambda _id: (x for x in (True,)),
    "bytes": lambda _id: b"\x01",
    "numeric-string": lambda _id: "1",
    "the-class-bool": lambda _id: bool,
    "mock-spec-bool": lambda _id: Mock(spec=bool),
    "claims-bool-class": lambda _id: ClaimsToBeBool(),
}


def test_bool_cannot_be_subclassed_so_a_boolean_answer_is_always_the_real_true_or_false():
    with pytest.raises(TypeError):
        type("FakeBool", (bool,), {})


@pytest.mark.parametrize("name", sorted(ODD_ANSWERS))
def test_an_odd_answer_of_the_revocation_check_is_the_fixed_503_at_the_upgrade(tmp_path, name, caplog, recwarn):
    registry, _device, token = enrolled(tmp_path)
    handled: list[str] = []
    caplog.set_level(logging.DEBUG)
    deps = AuthDeps(gate_ok_async=deps_for(registry).gate_ok_async, registry=registry, is_revoked=ODD_ANSWERS[name])
    reply_ = get(make_app(deps, handled), creds(token))
    assert refusal(reply_, 503, "unavailable"), reply_
    assert handled == [] and b"device_revoked" not in reply_.body
    assert SECRET not in caplog.text and token not in caplog.text and "Traceback" not in caplog.text
    gc.collect()
    assert [str(w.message) for w in recwarn if "never awaited" in str(w.message)] == []


def test_an_object_claiming_to_be_a_bool_is_not_the_403_marker(tmp_path):
    registry, _device, token = enrolled(tmp_path)
    handled: list[str] = []
    deps = AuthDeps(
        gate_ok_async=deps_for(registry).gate_ok_async, registry=registry, is_revoked=lambda _id: ClaimsToBeBool()
    )
    assert refusal(get(make_app(deps, handled), creds(token)), 503, "unavailable")


@pytest.mark.parametrize("name", sorted(ODD_ANSWERS))
def test_an_odd_answer_for_a_frame_drops_it_unacknowledged_and_the_session_recovers(tmp_path, name, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    state = {"broken": False}

    def check(device_id: str) -> Any:
        return ODD_ANSWERS[name](device_id) if state["broken"] else False

    def scenario(ws: Any) -> dict[str, Any]:
        say_hello(ws)
        state["broken"] = True
        ws.sock.settimeout(0.4)
        ws.send(frame_bytes(TEXT, json.dumps(MSG_IN).encode()))
        ws.send(frame_bytes(TEXT, json.dumps(PING).encode()))
        assert ws.recv() is None  # neither an acknowledgement nor a pong
        state["broken"] = False
        ws.sock.settimeout(5)
        ws.send(frame_bytes(TEXT, json.dumps(PING).encode()))
        return reply(ws)

    assert session(w, scenario, is_revoked=check)["type"] == "pong"
    assert w.frames == [] and "Traceback" not in caplog.text and SECRET not in caplog.text
