# SPDX-License-Identifier: Apache-2.0
"""The read-only type has no live write path (T1.179): every writer of the base ``Registry`` raises
``ReadOnlyRegistryError`` first, before any validation, and nothing touches the signer's directory."""

from __future__ import annotations

import os

import pytest
from signer_registry_helpers import FP, SIGNER_UID, publish

from glasses.registry import Registry
from glasses.registry_signer_file import ReadOnlyRegistryError, SignerFileRegistry

# Every method of the base Registry that writes or takes the write lock, with arguments validation would refuse
# (a bad name, an unknown id), so the test also pins that the read-only refusal comes first.
WRITERS = [
    ("add", ("bad name", "not a fingerprint", ["no-such-lane"])),
    ("revoke", ("not an id",)),
    ("_write", ([],)),
    ("_transaction", ()),
    ("_open_lock", ()),
    ("_sweep_temps", ()),
]


def spy(func: str, real, calls: list):
    def record(*args, **kwargs):
        calls.append((func, args[:1]))
        return real(*args, **kwargs)

    return record


def test_the_list_names_methods_the_base_registry_has():
    assert all(callable(getattr(Registry, name)) for name, _ in WRITERS)


@pytest.mark.parametrize(("name", "args"), WRITERS)
def test_every_writer_raises_read_only_before_anything_else_and_touches_nothing(tmp_path, monkeypatch, name, args):
    target, _, _ = publish(tmp_path)
    before = (target.read_bytes(), target.stat().st_mtime_ns, sorted(os.listdir(target.parent)))
    calls: list[tuple[str, object]] = []
    for func in ("open", "replace", "rename", "unlink", "mkdir"):
        monkeypatch.setattr(os, func, spy(func, getattr(os, func), calls))
    registry = SignerFileRegistry(tmp_path / "public", SIGNER_UID)
    with pytest.raises(ReadOnlyRegistryError):  # not InvalidDeviceError / UnknownDeviceError
        getattr(registry, name)(*args)
    assert calls == []
    assert (target.read_bytes(), target.stat().st_mtime_ns, sorted(os.listdir(target.parent))) == before


def test_a_valid_add_and_revoke_are_refused_and_reads_still_work(tmp_path):
    _, device, _ = publish(tmp_path)
    registry = SignerFileRegistry(tmp_path / "public", SIGNER_UID)
    with pytest.raises(ReadOnlyRegistryError):
        registry.add("phone", "ef" * 32, ["main"])
    with pytest.raises(ReadOnlyRegistryError):
        registry.revoke(device.device_id)
    assert registry.lookup("x", FP) == (None, False) and len(registry.list()) == 1
