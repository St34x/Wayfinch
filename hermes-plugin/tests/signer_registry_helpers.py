# SPDX-License-Identifier: Apache-2.0
"""Shared helpers of the signer-file registry tests: a ``devices.json`` as the signer publishes it (built with the
plugin's own writer in a scratch dir, then copied to a public dir with the published mode)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from server_auth_helpers import FP, OTHER_FP, TEST_CA

from glasses.registry import Device, Registry
from glasses.registry_parse import FILE_NAME

SIGNER_UID = os.geteuid()  # the test process owns every file it makes, so it plays the signer user
__all__ = ["FP", "OTHER_FP", "SIGNER_UID", "make_public_dir", "publish"]


def make_public_dir(tmp_path: Path) -> Path:
    public = tmp_path / "public"
    public.mkdir(mode=0o750, exist_ok=True)
    return public


def publish(tmp_path: Path, *, revoke: bool = False, mode: int = 0o640) -> tuple[Path, Device, str]:
    """Write one device (and the tombstone of a second, revoked one when *revoke*) to ``<tmp>/public/devices.json``,
    and the test CA as ``device-ca.pem`` (0640) next to it."""
    source = Registry(tmp_path / "source")
    device, token = source.add("glasses", FP, ["main"])
    if revoke:
        gone, _ = source.add("lost", OTHER_FP, ["main"])
        source.revoke(gone.device_id)
    public = make_public_dir(tmp_path)
    target = public / FILE_NAME
    target.unlink(missing_ok=True)
    shutil.copyfile(tmp_path / "source" / FILE_NAME, target)
    target.chmod(mode)
    bundle = public / "device-ca.pem"
    bundle.write_bytes(TEST_CA.pem)
    bundle.chmod(0o640)
    return target, device, token
