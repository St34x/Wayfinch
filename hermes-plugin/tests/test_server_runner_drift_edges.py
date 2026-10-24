# SPDX-License-Identifier: Apache-2.0
"""The aiohttp-internals swap of ``build_runner`` (T1.07b), the drifts the first tests do not name: a renamed ``_loop``
of ``web.Server``, a ``handle_error`` that is gone altogether, and a request parser without ``feed_data``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web, web_protocol
from server_helpers import app_for, wiring

from glasses.server import AiohttpDrift, build_runner


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, 30))


def test_a_renamed_loop_of_the_server_is_refused_by_setup_and_leaves_nothing_started(tmp_path: Path, monkeypatch):
    real_init = web.Server.__init__

    def renamed(self: web.Server, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        self._event_loop = self.__dict__.pop("_loop")

    monkeypatch.setattr(web.Server, "__init__", renamed)

    async def main() -> None:
        runner = build_runner(app_for(wiring(tmp_path)))
        with pytest.raises(AiohttpDrift, match="_loop") as caught:
            await runner.setup()
        assert "_kwargs" not in str(caught.value)  # only what is missing is named
        assert runner.server is None

    run(main())


def test_a_handle_error_that_is_gone_is_the_typed_error_not_an_attribute_error(tmp_path: Path, monkeypatch):
    monkeypatch.delattr(web.RequestHandler, "handle_error")
    with pytest.raises(AiohttpDrift, match="handle_error"):
        build_runner(app_for(wiring(tmp_path)))


def test_a_handle_error_that_is_not_callable_is_the_typed_error_too(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(web.RequestHandler, "handle_error", None)
    with pytest.raises(AiohttpDrift, match="handle_error"):
        build_runner(app_for(wiring(tmp_path)))


def test_a_log_exception_that_is_gone_is_the_typed_error_not_an_attribute_error(tmp_path: Path, monkeypatch):
    monkeypatch.delattr(web.RequestHandler, "log_exception")
    with pytest.raises(AiohttpDrift, match="log_exception is missing"):
        build_runner(app_for(wiring(tmp_path)))


def test_a_log_exception_that_is_not_callable_is_the_typed_error_too(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(web.RequestHandler, "log_exception", None)
    with pytest.raises(AiohttpDrift, match="log_exception is missing"):
        build_runner(app_for(wiring(tmp_path)))


def test_a_log_exception_with_other_parameters_is_refused_by_build_runner(tmp_path: Path, monkeypatch):
    def log_exception(self, message, exc_info=None):  # `(*args, **kw)` became a fixed shape
        raise AssertionError

    monkeypatch.setattr(web.RequestHandler, "log_exception", log_exception)
    with pytest.raises(AiohttpDrift, match="log_exception has other parameters"):
        build_runner(app_for(wiring(tmp_path)))


def test_a_request_parser_without_feed_data_is_refused_by_setup_and_leaves_nothing_started(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(web_protocol, "HttpRequestParser", lambda *args, **kwargs: object())  # the parser moved

    async def main() -> None:
        runner = build_runner(app_for(wiring(tmp_path)))
        with pytest.raises(AiohttpDrift, match="the request parser has no feed_data"):
            await runner.setup()
        assert runner.server is None

    run(main())
