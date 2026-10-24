# SPDX-License-Identifier: Apache-2.0
"""The aiohttp-internals swap of ``build_runner`` fails loudly at start, not only in the test suite: every drift of
what ``server_runner`` relies on is one typed error from ``build_runner`` or the runner's ``setup``, never a silent
server that answers a parse error with the offending line."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from server_helpers import app_for, wiring

from glasses import server_runner
from glasses.server import AiohttpDrift, build_runner

LEDGER = Path(__file__).resolve().parents[2] / "docs" / "dependencies.md"


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, 30))


def runner_of(tmp_path: Path) -> web.AppRunner:
    return build_runner(app_for(wiring(tmp_path)))


def test_the_tested_minor_is_the_one_of_the_aiohttp_row_of_the_ledger():
    row = next(line for line in LEDGER.read_text(encoding="utf-8").splitlines() if line.startswith("| aiohttp |"))
    pin = re.fullmatch(r"\| aiohttp \| (\d+\.\d+)\.\d+ \|.*", row)
    assert pin is not None and pin.group(1) == server_runner.TESTED_AIOHTTP


def test_the_installed_aiohttp_passes_the_self_check_and_the_swap_is_applied(tmp_path):
    async def main() -> type:
        runner = runner_of(tmp_path)
        await runner.setup()
        try:
            assert runner.server is not None
            return type(runner.server)
        finally:
            await runner.cleanup()

    assert run(main()) is server_runner._Server


@pytest.mark.parametrize("version", ["3.13.9", "3.15.0", "4.0.0", "2.3.10"])
def test_another_minor_of_aiohttp_is_refused_by_build_runner_naming_the_version(tmp_path, monkeypatch, version):
    monkeypatch.setattr(aiohttp, "__version__", version)
    with pytest.raises(AiohttpDrift, match=re.escape(version)):
        runner_of(tmp_path)


def test_a_patch_release_of_the_tested_minor_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(aiohttp, "__version__", f"{server_runner.TESTED_AIOHTTP}.99")
    assert isinstance(runner_of(tmp_path), web.AppRunner)


def test_a_changed_handle_error_signature_is_refused_by_build_runner(tmp_path, monkeypatch):
    def handle_error(self, request, status=500, exc=None):  # the `message` parameter is gone
        raise AssertionError

    monkeypatch.setattr(web.RequestHandler, "handle_error", handle_error)
    with pytest.raises(AiohttpDrift, match="handle_error"):
        runner_of(tmp_path)


def test_a_removed_make_server_is_refused_by_build_runner(tmp_path, monkeypatch):
    monkeypatch.delattr(web.AppRunner, "_make_server")
    with pytest.raises(AiohttpDrift, match="_make_server"):
        runner_of(tmp_path)


def test_a_renamed_kwargs_of_the_server_is_refused_by_setup_and_leaves_nothing_started(tmp_path, monkeypatch):
    real_init = web.Server.__init__

    def renamed(self: web.Server, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        self._options = self.__dict__.pop("_kwargs")

    monkeypatch.setattr(web.Server, "__init__", renamed)

    async def main() -> None:
        runner = runner_of(tmp_path)  # the self-check passes: the attribute is on the instance
        with pytest.raises(AiohttpDrift, match="_kwargs"):
            await runner.setup()
        assert runner.server is None  # setup undid itself

    run(main())


def test_a_swap_that_aiohttp_never_called_is_refused_by_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(server_runner._Runner, "_make_server", web.AppRunner._make_server)

    async def main() -> None:
        runner = runner_of(tmp_path)
        with pytest.raises(AiohttpDrift, match="swap"):
            await runner.setup()
        assert runner.server is None

    run(main())


def test_the_error_is_a_runtime_error_and_names_no_request_value(tmp_path, monkeypatch):
    monkeypatch.setattr(aiohttp, "__version__", "9.9.9")
    with pytest.raises(RuntimeError) as caught:
        runner_of(tmp_path)
    assert isinstance(caught.value, AiohttpDrift) and "9.9.9" in str(caught.value)
