# SPDX-License-Identifier: Apache-2.0
"""WS server (T1.07b), a ``peer_allowed`` predicate that raises: fail closed (the unknown-path ``404``), one WARNING per
new kind of failure and DEBUG while it repeats, the exception type only."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from server_helpers import app_for, serving, wiring

SECRET = "peer-secret-that-must-stay-out-of-every-log-line"


def test_fifty_requests_with_a_raising_predicate_give_one_warning_and_the_same_404(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)

    def broken(request: object) -> bool:
        raise RuntimeError(SECRET)

    async def main() -> set[int]:
        async with serving(app_for(w, peer_allowed=broken)) as port, aiohttp.ClientSession() as session:
            statuses = set()
            for _ in range(50):
                async with session.get(f"http://127.0.0.1:{port}/ws") as response:
                    statuses.add(response.status)
            return statuses

    assert asyncio.run(asyncio.wait_for(main(), 60)) == {404}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "peer check" in r.getMessage()]
    repeats = [r for r in caplog.records if r.levelno == logging.DEBUG and "peer check" in r.getMessage()]
    assert len(warnings) == 1 and len(repeats) == 49
    assert "RuntimeError" in warnings[0].getMessage() and SECRET not in caplog.text


def test_a_new_kind_of_failure_warns_again_and_so_does_the_same_kind_after_a_good_answer(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    answers: list[object] = [ValueError(), ValueError(), KeyError(), True, KeyError()]

    def scripted(request: object) -> bool:
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return bool(answer)

    async def main() -> None:
        async with serving(app_for(w, peer_allowed=scripted)) as port, aiohttp.ClientSession() as session:
            for _ in range(5):
                async with session.get(f"http://127.0.0.1:{port}/ws"):
                    pass

    asyncio.run(asyncio.wait_for(main(), 30))
    kinds = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "peer check" in r.getMessage()]
    assert [k.split("(")[1].split(")")[0] for k in kinds] == ["ValueError", "KeyError", "KeyError"]
