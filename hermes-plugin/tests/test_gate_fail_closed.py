# SPDX-License-Identifier: Apache-2.0
"""The allow-all gate fails closed on every path when a Hermes gate reader raises an error type the gate does not
expect (outside ``glasses.gate.SCOPE_ERRORS``): the startup hook ``validate_config``, the adapter at runtime
(``connect`` and every inbound event) and ``glasses-admin config check --live`` all refuse.

The real ``glasses.gate._hermes_api`` wiring and the real ``glasses.hermes_facts.hermes_facts`` run against stand-in
Hermes modules placed in ``sys.modules`` (no Hermes import; the stand-ins are removed before conftest's check).
Each path is first shown to accept with well-behaved readers, so a refusal is caused by the raised error alone.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import logging
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

import glasses
from glasses import admin, gate

from test_adapter import ADAPTER_MODULE, _fake_hermes_modules

DETAIL = "gate-reader-detail-4417"  # must never reach a log line or the CLI output


class UnexpectedHermesError(Exception):
    """An error type a Hermes reader might raise that no SCOPE_ERRORS entry covers."""


UNEXPECTED = [
    UnexpectedHermesError(DETAIL),
    ZeroDivisionError(DETAIL),
    AssertionError(DETAIL),
    yaml.YAMLError(DETAIL),
    StopIteration(DETAIL),
]
SITES = ["installed_profiles", "secret_scope", "active_scope", "scope_value"]


class RaisingScope(Mapping[str, str]):
    """A profile secret scope whose every read raises."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def __getitem__(self, key: str) -> str:
        raise self.exc

    def get(self, key, default=None):
        raise self.exc

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


class FakeHermes:
    """Stand-in Hermes readers; ``site`` names the one that raises ``exc`` (None = all behave)."""

    def __init__(self, home: Path, site: str | None = None, exc: BaseException | None = None) -> None:
        self.home, self.site, self.exc = home, site, exc

    def _raise_at(self, site: str) -> None:
        if self.site == site and self.exc is not None:
            raise self.exc

    def platform_gate_env(self, name: str, default: str = "") -> str:
        self._raise_at("active_scope")
        return default

    def build_profile_secret_scope(self, home: Path) -> Mapping[str, str]:
        self._raise_at("secret_scope")
        if self.site == "scope_value" and self.exc is not None:
            return RaisingScope(self.exc)
        return {"GLASSES_ALLOWED_USERS": "dev_1"}

    def profiles_to_serve(self, multiplex: bool, *, include_standalone: bool = False, include_parked: bool = False):
        if include_parked:  # the gate's call: every installed profile
            self._raise_at("installed_profiles")
        return [("default", self.home), ("work", self.home / "work")]

    def modules(self) -> dict[str, ModuleType | SimpleNamespace]:
        mods: dict[str, ModuleType | SimpleNamespace] = dict(_fake_hermes_modules())
        mods["gateway.config"].load_gateway_config = lambda: SimpleNamespace(multiplex_profiles=True, profile_routes=[])
        mods["gateway.platforms._shared"] = SimpleNamespace(platform_gate_env=self.platform_gate_env)
        mods["gateway.profile_routing"] = SimpleNamespace(
            match_profile_route=lambda _routes, _platform, chat_id: (
                SimpleNamespace(profile="work") if chat_id.endswith(":work") else None
            )
        )
        mods["agent.secret_scope"] = SimpleNamespace(build_profile_secret_scope=self.build_profile_secret_scope)
        mods["hermes_cli.profiles"] = SimpleNamespace(profiles_to_serve=self.profiles_to_serve)
        mods["hermes_cli.config"] = SimpleNamespace(load_config_readonly=lambda: {"stt": {"provider": "local"}})
        mods["hermes_cli.tools_config"] = SimpleNamespace(_get_platform_tools=lambda _config, _platform: {"web"})
        mods["hermes_constants"] = SimpleNamespace(
            set_hermes_home_override=lambda home: home, reset_hermes_home_override=lambda token: None
        )
        mods["tools.clarify_gateway"] = SimpleNamespace(resolve_clarify_timeout=lambda _config: 0)
        mods["tools.transcription_common"] = SimpleNamespace(LOCAL_STT_LANGUAGE_ENV="GLASSES_TEST_UNSET_STT_LANGUAGE")
        for parent in ("agent", "hermes_cli", "tools", "gateway.platforms"):
            mods.setdefault(parent, SimpleNamespace())
        return mods


@pytest.fixture
def hermes(tmp_path: Path) -> Iterator[FakeHermes]:
    """Stand-in Hermes loaded (marker module present); ``glasses.adapter`` imported against it."""
    fake = FakeHermes(tmp_path)
    with pytest.MonkeyPatch.context() as mp:
        for name, module in fake.modules().items():
            mp.setitem(sys.modules, name, module)
        mp.delitem(sys.modules, ADAPTER_MODULE, raising=False)
        try:
            yield fake
        finally:
            sys.modules.pop(ADAPTER_MODULE, None)
            if hasattr(glasses, "adapter"):
                delattr(glasses, "adapter")


def fail_with(fake: FakeHermes, site: str, exc: BaseException) -> None:
    fake.site, fake.exc = site, exc


def platform_config() -> SimpleNamespace:
    return SimpleNamespace(enabled=True, extra={})


def write_home(home: Path) -> Path:
    (home / "glasses").mkdir(mode=0o700, exist_ok=True)
    (home / "glasses").chmod(0o700)
    extra = {"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant", "work"]}
    data = {"gateway": {"platforms": {"glasses": {"enabled": True, "extra": extra}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return home


def config_check_live(home: Path) -> tuple[int, str]:
    out = io.StringIO()
    probes = admin.Probes(port_free=lambda _h, _p: True, resolves=lambda _h: True, hermes=admin.hermes_facts)
    code = admin.main(["config", "check", "--live", "--hermes-home", str(home)], {}, out, probes)
    return code, out.getvalue()


def adapter_run(adapter_module: ModuleType) -> tuple[bool, list[tuple[str, str, bool]], list[object], object]:
    adapter = adapter_module.GlassesAdapter(platform_config())
    inbound = SimpleNamespace(_gateway_accepted=None, text="hello")
    connected = asyncio.run(adapter.connect())
    asyncio.run(adapter.handle_message(inbound))
    return connected, adapter.fatal_errors, adapter.handed_on, inbound._gateway_accepted


def test_unexpected_error_types_are_outside_the_scope_errors():
    assert not any(isinstance(exc, gate.SCOPE_ERRORS) for exc in UNEXPECTED)


def test_every_path_accepts_while_the_hermes_readers_behave(hermes):
    assert gate.violations() == []
    assert glasses.validate_config(platform_config()) is True
    connected, fatal, handed_on, accepted = adapter_run(importlib.import_module(ADAPTER_MODULE))
    assert [code for code, _, _ in fatal] == ["not_implemented"]
    assert len(handed_on) == 1 and accepted is None
    code, out = config_check_live(write_home(hermes.home))
    assert code == admin.EXIT_OK, out


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("exc", UNEXPECTED, ids=lambda e: type(e).__name__)
def test_startup_validation_refuses_on_an_unexpected_gate_reader_error(hermes, caplog, site, exc):
    fail_with(hermes, site, exc)
    with pytest.raises(type(exc)):
        gate.violations()
    with caplog.at_level(logging.DEBUG):
        assert glasses.validate_config(platform_config()) is False
    assert type(exc).__name__ in caplog.text and DETAIL not in caplog.text


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("exc", UNEXPECTED, ids=lambda e: type(e).__name__)
def test_adapter_refuses_connect_and_drops_events_on_an_unexpected_gate_reader_error(hermes, caplog, site, exc):
    adapter_module = importlib.import_module(ADAPTER_MODULE)
    fail_with(hermes, site, exc)
    with caplog.at_level(logging.DEBUG):
        connected, fatal, handed_on, accepted = adapter_run(adapter_module)
    assert connected is False
    assert [code for code, _, _ in fatal] == ["allow_all_gate"]
    assert handed_on == [] and accepted is False
    assert f"allow-all gate check failed ({type(exc).__name__})" in caplog.text and DETAIL not in caplog.text


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("exc", UNEXPECTED, ids=lambda e: type(e).__name__)
def test_config_check_live_exits_non_zero_on_an_unexpected_gate_reader_error(hermes, site, exc):
    home = write_home(hermes.home)
    fail_with(hermes, site, exc)
    code, out = config_check_live(home)
    assert code == admin.EXIT_ERRORS, out
    assert f"cannot read the Hermes configuration ({type(exc).__name__})" in out and DETAIL not in out


@pytest.mark.parametrize("site", SITES)
def test_config_check_live_reports_an_unreadable_scope_as_a_gate_error(hermes, site):
    home = write_home(hermes.home)
    fail_with(hermes, site, OSError(DETAIL))
    code, out = config_check_live(home)
    assert code == admin.EXIT_ERRORS, out
    assert "error: allow-all gate: profile scopes unreadable (OSError)" in out and DETAIL not in out
