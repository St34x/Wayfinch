# SPDX-License-Identifier: Apache-2.0
"""Inbound text against the pinned Hermes (T1.08): the real ``GlassesAdapter`` in the Hermes venv
(``.local/venv314``, clone at ``HERMES_REF``) turns a ``msg.in`` into the pinned ``MessageEvent(TEXT)``; with
``gateway.profile_routes`` mapping the lane to a served profile, ``source.profile`` is that profile, and a lane routed
to a profile the gateway does not serve is ``lane_unrouted``. The pinned gateway's own user check
(``GatewayRunner._is_user_authorized``, ``gateway/authz_mixin.py``) admits the event's source with
``GLASSES_ALLOWED_USERS=<deviceId>`` and refuses it with another id or with the list empty or unset. The plugin is
registered through its own ``register()``, so the allowlist variable is the one the plugin declares. Skipped (named
reason) without the venv; a scratch ``HERMES_HOME`` under ``.local/hh-t154``, no gateway, no network.
``SETUP`` (the adapter, runner and recorder above) is shared with ``test_inbound_gateway_control_pinned.py``."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

from conftest import PLUGIN_ROOT
from test_config_check_edges import _scratch
from test_config_check_live import hermes_python

DEVICE_ID = "0123456789abcdef"
TEXT = "planted-text-3317"
SETUP_HOME = PLUGIN_ROOT.parent / "tools" / "test-hermes" / "setup_home.sh"

SETUP = f"""\
import asyncio, json, os, sys
from types import SimpleNamespace
sys.path.insert(0, {str(PLUGIN_ROOT)!r})
import gateway.platforms._shared  # noqa: F401 - the marker that tells the gate Hermes is loaded
import jsonschema
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.pairing import PairingStore
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms import base, event as hermes_event
from gateway.profile_routing import parse_profile_routes
from gateway.run import GatewayRunner
import glasses
from glasses.adapter import ENVELOPE_SCHEMA, GlassesAdapter
from glasses.registry import Device


class Ctx:  # registers the platform as the plugin manager does (hermes_cli/plugins.py register_platform)
    def register_platform(self, name, label, adapter_factory, check_fn, validate_config=None, required_env=None,
                          install_hint="", **kwargs):
        platform_registry.register(PlatformEntry(name=name, label=label, adapter_factory=adapter_factory,
            check_fn=check_fn, validate_config=validate_config, required_env=required_env or [],
            install_hint=install_hint, source="plugin", **kwargs))


glasses.register(Ctx())
extra = {{"lanes": ["assistant", "work", "gone"], "home_lane": "assistant"}}
platform = Platform("glasses")
runner = object.__new__(GatewayRunner)
runner.config = GatewayConfig(multiplex_profiles=True)
runner.config.platforms = {{platform: PlatformConfig(enabled=True, extra=extra)}}
runner.config.profile_routes = parse_profile_routes([
    {{"name": "glasses-work", "platform": "glasses", "profile": "work", "chat_id": "glasses:work"}},
    {{"name": "glasses-gone", "platform": "glasses", "profile": "gone", "chat_id": "glasses:gone"}}])
runner.pairing_store, runner.pairing_stores = PairingStore(profile="default"), {{}}
runner._primary_profile_name = "default"
adapter = GlassesAdapter(PlatformConfig(enabled=True, extra=extra))
adapter.gateway_runner = runner
runner.adapters, runner._profile_adapters = {{platform: adapter}}, {{}}

seen, sent = [], []
base_handle_message = base.BasePlatformAdapter.handle_message
async def record(self, event):  # the base class's handle_message: record the event the adapter hands on
    seen.append(event)
base.BasePlatformAdapter.handle_message = record
schema = jsonschema.Draft202012Validator(json.loads(ENVELOPE_SCHEMA.read_text(encoding="utf-8")))
adapter.router.bind(SimpleNamespace(send=lambda device_id, text: sent.append((device_id, json.loads(text)))), schema)
device = Device({DEVICE_ID!r}, "glasses", "00" * 32, ("assistant", "work", "gone"), (), "2026-10-21T00:00:00Z", None,
                None)

def msg_in(frame_id, lane, text={TEXT!r}):
    return {{"v": 1, "id": frame_id, "type": "msg.in", "ts": "2026-10-21T00:00:00Z", "lane": lane,
            "body": {{"text": text, "origin": "typed"}}}}
"""

RUNNER = (
    SETUP
    + f"""
async def main():
    await adapter.router.on_frame(device, msg_in("01JABCDEFGHJKMNPQRSTVWXYZ0", "work"))
    await adapter.router.on_frame(device, msg_in("01JABCDEFGHJKMNPQRSTVWXYZ1", "gone"))
    await adapter.disconnect()

asyncio.run(main())
(event,) = seen
src = event.source
verdicts = {{}}
for name, value in (("device", {DEVICE_ID!r}), ("other", "fedcba9876543210"), ("empty", ""), ("unset", None)):
    if value is None:
        os.environ.pop("GLASSES_ALLOWED_USERS", None)
    else:
        os.environ["GLASSES_ALLOWED_USERS"] = value
    verdicts[name] = runner._is_user_authorized(src)
print(json.dumps({{
    "gateway": os.path.dirname(os.path.dirname(gateway.platforms._shared.__file__)),
    "event_class": type(event) is hermes_event.MessageEvent,
    "text_type": event.message_type is hermes_event.MessageType.TEXT,
    "fields": [event.text == {TEXT!r}, event.user_id, event.message_id],
    "source": [src.platform.value, src.chat_id, src.chat_type, src.user_id, src.profile],
    "sent": sent,
    "verdicts": verdicts,
}}))
"""
)


def _pinned_ref() -> str:
    found = re.search(r'REF="\$\{HERMES_REF:-([0-9a-f]+)\}"', SETUP_HOME.read_text(encoding="utf-8"))
    assert found, "setup_home.sh no longer names the default HERMES_REF"
    return found.group(1)


def _run(runner: str = RUNNER) -> dict:
    python = hermes_python()  # skips with a named reason when the venv is missing
    with _scratch() as tmp:
        home, user = Path(tmp) / "home", Path(tmp) / "user"
        (home / "profiles" / "work").mkdir(parents=True)
        user.mkdir()
        (home / "profiles" / "work" / "config.yaml").write_text(yaml.safe_dump({"agent": {}}))
        script = Path(tmp) / "runner.py"
        script.write_text(runner, encoding="utf-8")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(user), "HERMES_HOME": str(home)}
        proc = subprocess.run(
            [str(python), str(script)], cwd=tmp, env=env, capture_output=True, text=True, timeout=240, check=False
        )
    assert proc.returncode == 0, proc.stderr[-3000:]
    result: dict = json.loads(proc.stdout.splitlines()[-1])
    return result


def test_pinned_hermes_builds_the_text_event_routes_it_and_applies_the_allowlist_to_the_device_id():
    result = _run()
    head = subprocess.run(
        ["git", "-C", result["gateway"], "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head.startswith(_pinned_ref()), f"the venv's Hermes is at {head}, not HERMES_REF {_pinned_ref()}"
    assert result["event_class"] and result["text_type"], result
    assert result["fields"] == [True, DEVICE_ID, "01JABCDEFGHJKMNPQRSTVWXYZ0"]
    assert result["source"] == ["glasses", "glasses:work", "dm", DEVICE_ID, "work"]
    ((device_id, error),) = result["sent"]  # the lane routed to a profile the gateway does not serve
    assert device_id == DEVICE_ID and error["body"]["code"] == "lane_unrouted" and "seq" not in error
    assert error["corr"] == "01JABCDEFGHJKMNPQRSTVWXYZ1" and TEXT not in json.dumps(error)
    assert result["verdicts"] == {"device": True, "other": False, "empty": False, "unset": False}, result["verdicts"]
