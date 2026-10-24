# SPDX-License-Identifier: Apache-2.0
"""Behaviour of the ``glasses`` adapter skeleton (``glasses.adapter``) without Hermes.

The adapter subclasses Hermes' ``BasePlatformAdapter``. These tests import it against small stand-in
``gateway.*`` modules that record what the adapter asks of its base class; the stand-ins and the
imported adapter module are removed again before conftest's Hermes-import check runs.
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace

import pytest

import glasses
from glasses import config as gcfg
from glasses import gate

ADAPTER_MODULE = "glasses.adapter"
ROUTER_MODULE = "glasses.adapter_router"
EDGE_SOCKET = "/run/glasses-plugin/edge.sock"


class FakePlatform:
    """Stands in for ``gateway.config.Platform`` (an enum keyed by the platform name)."""

    def __init__(self, value: str) -> None:
        self.value = value


@dataclass
class FakeSendResult:
    success: bool
    error: str | None = None
    retryable: bool = False


class FakeBaseAdapter:
    """Stands in for ``gateway.platforms.base.BasePlatformAdapter``; records the calls the adapter makes."""

    def __init__(self, config, platform) -> None:
        self.config = config
        self.platform = platform
        self.fatal_errors: list[tuple[str, str, bool]] = []
        self.disconnected = False
        self.handed_on: list[object] = []

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self.fatal_errors.append((code, message, retryable))

    def _mark_disconnected(self) -> None:
        self.disconnected = True

    async def handle_message(self, event) -> None:
        self.handed_on.append(event)

    def build_source(self, chat_id, chat_type="dm", user_id=None):
        """As the pinned base: a route to a profile the gateway does not serve marks the source (here: by chat id)."""
        rejected = chat_id in getattr(self, "unrouted_chats", ())
        return SimpleNamespace(chat_id=chat_id, chat_type=chat_type, user_id=user_id, profile_route_rejected=rejected)


class FakeMessageType(enum.Enum):
    TEXT = "text"


@dataclass
class FakeMessageEvent:
    """Stands in for ``gateway.platforms.event.MessageEvent`` (the fields the adapter sets)."""

    text: str
    message_type: FakeMessageType
    user_id: str | None = None
    source: object = None
    message_id: str | None = None
    allow_gateway_control: bool = True  # the pinned default
    _gateway_accepted: bool | None = None


def _fake_hermes_modules() -> dict[str, ModuleType]:
    modules = {name: ModuleType(name) for name in ("gateway", "gateway.config", "gateway.platforms")}
    modules["gateway.config"].Platform = FakePlatform
    base = ModuleType("gateway.platforms.base")
    base.BasePlatformAdapter = FakeBaseAdapter
    base.SendResult = FakeSendResult
    modules["gateway.platforms.base"] = base
    event_module = ModuleType("gateway.platforms.event")
    event_module.MessageEvent = FakeMessageEvent
    event_module.MessageType = FakeMessageType
    modules["gateway.platforms.event"] = event_module
    return modules


@pytest.fixture
def adapter_module() -> Iterator[ModuleType]:
    with pytest.MonkeyPatch.context() as mp:
        for name, module in _fake_hermes_modules().items():
            mp.setitem(sys.modules, name, module)
        for name in (ADAPTER_MODULE, ROUTER_MODULE):  # both import the stand-in gateway modules
            mp.delitem(sys.modules, name, raising=False)
        try:
            yield importlib.import_module(ADAPTER_MODULE)
        finally:
            _forget_adapter_modules()


def _forget_adapter_modules() -> None:
    for name in (ADAPTER_MODULE, ROUTER_MODULE):
        sys.modules.pop(name, None)
        if hasattr(glasses, name.removeprefix("glasses.")):
            delattr(glasses, name.removeprefix("glasses."))


@pytest.fixture
def router_module(adapter_module: ModuleType) -> ModuleType:
    """``glasses.adapter_router`` as the adapter fixture imported it (owns ``_unrouted`` and the event types)."""
    return sys.modules[ROUTER_MODULE]


def platform_config(extra=None):
    """A stand-in for Hermes' PlatformConfig (only the attributes the plugin reads)."""
    return SimpleNamespace(enabled=True, extra={} if extra is None else extra)


def new_adapter(adapter_module, extra=None):
    return adapter_module.GlassesAdapter(platform_config(extra))


def event():
    return SimpleNamespace(_gateway_accepted=None, text="hello")


# --------------------------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------------------------


def test_adapter_is_built_for_the_glasses_platform_with_loaded_settings(adapter_module):
    adapter = new_adapter(adapter_module, {"edge": {"socket": EDGE_SOCKET, "user": "edge-proxy"}, "bridge_port": 9100})
    assert adapter.platform.value == gcfg.PLATFORM_NAME
    settings = adapter.settings
    assert (settings.edge_socket, settings.edge_user, settings.bridge_port) == (EDGE_SOCKET, "edge-proxy", 9100)


def test_adapter_without_extra_uses_the_defaults(adapter_module):
    adapter = adapter_module.GlassesAdapter(SimpleNamespace(enabled=True, extra=None))
    assert (adapter.settings.edge_socket, adapter.settings.edge_user) == ("", "")


def test_adapter_refuses_an_invalid_config(adapter_module):
    with pytest.raises(gcfg.ConfigError):
        new_adapter(adapter_module, {"bridge_port": "not a port"})


@pytest.mark.parametrize("extra", [["bridge_port", 9100], "bridge_port: 9100", 9100])
def test_adapter_refuses_an_extra_that_is_not_a_mapping(adapter_module, extra):
    with pytest.raises(gcfg.ConfigError):
        adapter_module.GlassesAdapter(SimpleNamespace(enabled=True, extra=extra))


@pytest.mark.parametrize("config", [SimpleNamespace(enabled=True), {"enabled": True, "extra": {"bridge_port": 9100}}])
def test_adapter_is_not_built_with_defaults_from_a_config_without_an_extra_attribute(adapter_module, config):
    # Hermes' PlatformConfig always carries ``extra``; any other shape fails construction (the gateway then builds
    # no adapter) instead of silently running on the default settings.
    with pytest.raises(AttributeError):
        adapter_module.GlassesAdapter(config)


def test_adapter_ignores_extra_keyword_arguments_from_the_gateway(adapter_module):
    adapter = adapter_module.GlassesAdapter(platform_config(), session_store=object())
    assert adapter.settings.bridge_port == gcfg.DEFAULTS["bridge_port"]


def test_adapter_carries_the_gateway_caps(adapter_module):
    assert adapter_module.GlassesAdapter.MAX_MESSAGE_LENGTH == gcfg.MAX_MESSAGE_LENGTH
    assert adapter_module.GlassesAdapter.splits_long_messages is True


def test_registered_factory_builds_the_adapter(adapter_module):
    calls = []
    glasses.register(SimpleNamespace(register_platform=lambda **kwargs: calls.append(kwargs)))
    (call,) = calls
    adapter = call["adapter_factory"](platform_config({"bridge_port": 9101}))
    assert isinstance(adapter, adapter_module.GlassesAdapter)
    assert adapter.settings.bridge_port == 9101


# --------------------------------------------------------------------------------------------
# connect / disconnect: fails closed and retryable until the transport exists
# --------------------------------------------------------------------------------------------


def test_connect_without_edge_settings_reports_a_retryable_edge_listener_error(adapter_module, caplog):
    adapter = new_adapter(adapter_module, {"edge": {"socket": EDGE_SOCKET}})
    with caplog.at_level(logging.WARNING, logger=ADAPTER_MODULE):
        assert asyncio.run(adapter.connect()) is False
    assert adapter.fatal_errors == [("edge_listener", "glasses edge listener not started: edge.user: not set", True)]
    assert "edge listener not started: edge.user: not set" in caplog.text


def test_reconnect_attempt_fails_the_same_way(adapter_module):
    adapter = new_adapter(adapter_module)
    assert asyncio.run(adapter.connect(is_reconnect=True, attempt=3)) is False
    assert [code for code, _, retryable in adapter.fatal_errors if retryable] == ["edge_listener"]


@pytest.mark.parametrize(
    ("name", "value"),
    [("GLASSES_ALLOW_ALL_USERS", "1"), ("GLASSES_ALLOWED_USERS", "*"), ("GATEWAY_ALLOW_ALL_USERS", "true")],
)
def test_connect_refuses_while_the_allow_all_gate_is_open(adapter_module, monkeypatch, caplog, name, value):
    adapter = new_adapter(adapter_module)
    monkeypatch.setenv(name, value)
    with caplog.at_level(logging.WARNING, logger=ADAPTER_MODULE):
        assert asyncio.run(adapter.connect()) is False
    assert adapter.fatal_errors == [("allow_all_gate", "glasses allow-all gate open (see log)", True)]
    assert f"refusing: process environment: {name}" in caplog.text
    assert "edge listener not started" not in caplog.text


def test_connect_rechecks_the_gate_on_every_attempt(adapter_module, monkeypatch):
    adapter = new_adapter(adapter_module)
    asyncio.run(adapter.connect())
    monkeypatch.setenv("GLASSES_ALLOW_ALL_USERS", "1")
    asyncio.run(adapter.connect(is_reconnect=True))
    assert [code for code, _, _ in adapter.fatal_errors] == ["edge_listener", "allow_all_gate"]


def test_disconnect_marks_the_adapter_disconnected(adapter_module):
    adapter = new_adapter(adapter_module)
    asyncio.run(adapter.disconnect())
    assert adapter.disconnected is True


# --------------------------------------------------------------------------------------------
# inbound events
# --------------------------------------------------------------------------------------------


def test_inbound_event_is_handed_to_the_gateway_while_the_gate_is_closed(adapter_module):
    adapter = new_adapter(adapter_module)
    inbound = event()
    assert asyncio.run(adapter.handle_message(inbound)) is None
    assert adapter.handed_on == [inbound]
    assert inbound._gateway_accepted is None  # left to the base class


def test_inbound_event_is_dropped_while_the_gate_is_open(adapter_module, monkeypatch, caplog):
    adapter = new_adapter(adapter_module)
    monkeypatch.setenv("GLASSES_ALLOWED_USERS", "dev_1,*")
    inbound = event()
    with caplog.at_level(logging.WARNING, logger=ADAPTER_MODULE):
        assert asyncio.run(adapter.handle_message(inbound)) is None
    assert adapter.handed_on == []
    assert inbound._gateway_accepted is False
    assert "inbound event dropped (allow-all gate open)" in caplog.text
    assert "hello" not in caplog.text  # message text never logged


# --------------------------------------------------------------------------------------------
# outbound before connect(): nothing to store into (the outbox paths: test_outbox_send.py)
# --------------------------------------------------------------------------------------------


def test_send_before_connect_stores_nothing_and_is_retryable_and_a_bad_chat_is_not(adapter_module):
    adapter = new_adapter(adapter_module)
    result = asyncio.run(adapter.send("glasses:assistant", "x" * (gcfg.MAX_MESSAGE_LENGTH + 1), metadata={"k": 1}))
    assert result == FakeSendResult(success=False, error=adapter_module.NOT_CONNECTED, retryable=True)
    bad = asyncio.run(adapter.send("telegram:assistant", "x"))
    assert bad == FakeSendResult(success=False, error=adapter_module.outbound.BAD_CHAT, retryable=False)


def test_chat_info_names_a_direct_chat(adapter_module):
    adapter = new_adapter(adapter_module)
    assert asyncio.run(adapter.get_chat_info("glasses:assistant")) == {"name": "glasses:assistant", "type": "dm"}


# --------------------------------------------------------------------------------------------
# gate errors: unreadable scopes are violations; anything else is refused at the adapter boundary
# --------------------------------------------------------------------------------------------


class UnexpectedGateBug(Exception):
    pass


def _hermes_api_raising(exc: BaseException):
    def installed():
        raise exc

    return lambda: SimpleNamespace(
        platform_gate_env=lambda name, default="": default,
        build_profile_secret_scope=lambda home: {},
        served_profiles=installed,
    )


@pytest.mark.parametrize(
    "exc",
    [
        OSError("io"),
        ValueError("parse"),
        KeyError("k"),
        ImportError("drift"),
        AttributeError("x"),
        TypeError("sig"),
        RuntimeError("r"),
    ],
)
def test_unreadable_or_drifted_hermes_scope_is_a_violation(monkeypatch, exc):
    monkeypatch.setattr(gate, "_hermes_api", _hermes_api_raising(exc))
    (problem,) = gate.violations()
    assert problem.startswith(f"profile scopes unreadable ({type(exc).__name__})")


def test_unexpected_gate_error_propagates_from_violations(monkeypatch):
    monkeypatch.setattr(gate, "_hermes_api", _hermes_api_raising(UnexpectedGateBug("detail")))
    with pytest.raises(UnexpectedGateBug):
        gate.violations()
    assert glasses.validate_config(platform_config()) is False


def test_adapter_refuses_on_an_unexpected_gate_error_and_logs_only_its_type(adapter_module, monkeypatch, caplog):
    adapter = new_adapter(adapter_module)
    monkeypatch.setattr(gate, "_hermes_api", _hermes_api_raising(UnexpectedGateBug("scope detail")))
    inbound = event()
    with caplog.at_level(logging.WARNING, logger=ADAPTER_MODULE):
        assert asyncio.run(adapter.connect()) is False
        asyncio.run(adapter.handle_message(inbound))
    assert adapter.fatal_errors == [("allow_all_gate", "glasses allow-all gate open (see log)", True)]
    assert adapter.handed_on == [] and inbound._gateway_accepted is False
    assert "allow-all gate check failed (UnexpectedGateBug)" in caplog.text
    assert "scope detail" not in caplog.text
