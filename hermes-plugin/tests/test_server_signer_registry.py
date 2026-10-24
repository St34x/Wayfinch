# SPDX-License-Identifier: Apache-2.0
"""The real WS app with the signer's registry file, the deny overlay and the last-seen writer injected (T1.179): the
``on_authenticated`` hook, fail closed on a bad registry file, and the 403 marker for an overlay entry."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
from aiohttp import web
from server_auth_helpers import EDGE_CERT, always, creds, get, refusal, upgrade_headers
from server_helpers import SCHEMA, serving
from signer_registry_helpers import SIGNER_UID, publish

from glasses.generated.protocol_types import Frame
from glasses.registry import Device
from glasses.registry_signer_file import LAST_SEEN_NAME, DenyOverlay, LastSeen, SignerFileRegistry
from glasses.server import ServerDeps, build_app


async def _ignore_frame(device: Device, frame: Frame) -> None:
    return None


def app_with(tmp_path: Path) -> tuple[web.Application, DenyOverlay, LastSeen]:
    overlay, seen = DenyOverlay(tmp_path / "state"), LastSeen(tmp_path / "state")
    return new_app(tmp_path, overlay, seen), overlay, seen


def new_app(tmp_path: Path, overlay: DenyOverlay, seen: LastSeen) -> web.Application:
    deps = ServerDeps(
        gate_ok_async=always(True),
        registry=SignerFileRegistry(tmp_path / "public", SIGNER_UID, overlay),
        is_revoked=overlay.is_denied,
        peer_allowed=lambda _request: True,
        client_fingerprint=EDGE_CERT.fingerprint,
        on_frame=_ignore_frame,
        schema=SCHEMA,
        max_audio_sec=30,
        on_authenticated=seen.touch,
    )
    return build_app(deps)


async def connect(app: web.Application, token: str) -> None:
    async with serving(app) as port, aiohttp.ClientSession() as session:
        headers = {**upgrade_headers(), **creds(token)}
        ws = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers=headers)
        await ws.close()


def test_an_upgrade_reads_the_signers_file_and_records_the_time_in_the_plugins_own_file(tmp_path):
    target, device, token = publish(tmp_path)
    before = target.read_bytes()
    app, _, seen = app_with(tmp_path)
    asyncio.run(asyncio.wait_for(connect(app, token), 20))
    seen.close()
    doc = json.loads((tmp_path / "state" / LAST_SEEN_NAME).read_text())
    assert list(doc["seen"]) == [device.device_id]
    assert target.read_bytes() == before and not (tmp_path / "state" / "devices.json").exists()


def test_a_missing_or_corrupt_file_is_401_and_a_valid_one_restores_the_service(tmp_path):
    target, _, token = publish(tmp_path)
    good = target.read_bytes()
    app, overlay, seen = app_with(tmp_path)
    target.write_bytes(b"garbage")
    assert refusal(get(app, creds(token)), 401, "unauthorized")
    target.unlink()
    assert refusal(get(new_app(tmp_path, overlay, seen), creds(token)), 401, "unauthorized")
    target.write_bytes(good)
    target.chmod(0o640)
    asyncio.run(asyncio.wait_for(connect(new_app(tmp_path, overlay, seen), token), 20))  # the same registry objects
    seen.close()


def test_a_file_with_a_loose_mode_is_401_for_every_device(tmp_path):
    _, _, token = publish(tmp_path, mode=0o664)
    app, _, seen = app_with(tmp_path)
    assert refusal(get(app, creds(token)), 401, "unauthorized")
    seen.close()


def test_a_device_in_the_overlay_gets_the_revocation_marker_and_no_sighting(tmp_path):
    _, device, token = publish(tmp_path)
    app, overlay, seen = app_with(tmp_path)
    overlay.deny(device.device_id)
    assert refusal(get(app, creds(token)), 403, "device_revoked")
    seen.close()
    assert not (tmp_path / "state" / LAST_SEEN_NAME).exists()  # a refused upgrade is not a sighting


def test_a_symlinked_or_writable_public_dir_is_401_and_a_good_one_serves(tmp_path):
    _, _, token = publish(tmp_path)
    public = tmp_path / "public"
    app, overlay, seen = app_with(tmp_path)
    public.chmod(0o777)
    assert refusal(get(app, creds(token)), 401, "unauthorized")
    public.chmod(0o750)
    public.rename(tmp_path / "real")
    public.symlink_to(tmp_path / "real")
    assert refusal(get(new_app(tmp_path, overlay, seen), creds(token)), 401, "unauthorized")
    public.unlink()
    (tmp_path / "real").rename(public)
    asyncio.run(asyncio.wait_for(connect(new_app(tmp_path, overlay, seen), token), 20))
    seen.close()


def test_a_valid_file_after_a_bad_one_restores_the_service_with_the_overlay_still_applied(tmp_path):
    target, device, token = publish(tmp_path)
    good = target.read_bytes()
    app, overlay, seen = app_with(tmp_path)
    overlay.deny(device.device_id)
    target.write_bytes(b"garbage")
    assert refusal(get(app, creds(token)), 401, "unauthorized")
    target.write_bytes(good)
    target.chmod(0o640)
    assert refusal(get(new_app(tmp_path, overlay, seen), creds(token)), 403, "device_revoked")  # overlay applied
    seen.close()
