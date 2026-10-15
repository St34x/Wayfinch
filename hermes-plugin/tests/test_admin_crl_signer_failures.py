# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin crl`` when the signer call fails: every failure kind is one fixed error line and exit 1 (an unset
key exit 2), with no traceback, no path and no answer text, and the command writes nothing - no state dir, no record,
no temp file - whatever the signer does."""

from __future__ import annotations

import io
import os
from pathlib import Path

import fake_signer
import pytest
from fake_signer import Script, answer

from glasses import admin, signer_client
from glasses.signer_client import SignerClient, Timeouts

FAST = Timeouts(connect_s=1.0, op_s=0.5, enrol_s=1.0)
MARKER = "answer-text-must-not-show"
ENTRY = {"ca_id": fake_signer.CA_ID, "number": 7, "next_update": fake_signer.TIME}


@pytest.fixture
def fake():
    with fake_signer.running() as running:
        yield running


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "gateway:\n  platforms:\n    glasses:\n      extra:\n"
        f"        state_dir: {tmp_path / 'state'}\n"
        "        signer:\n          socket: /run/device-signer/signer.sock\n          user: nobody\n",
        encoding="utf-8",
    )
    return path


def run_on(monkeypatch, tmp_path: Path, socket_path: Path, peer_uid: int | None = None) -> tuple[int, str]:
    client = SignerClient(socket_path, os.geteuid(), peer_uid=peer_uid, timeouts=FAST)
    monkeypatch.setattr(signer_client, "client_from_config", lambda _cfg: client)
    out = io.StringIO()
    code = admin.main(["crl", "--config", str(write_config(tmp_path))], env={}, out=out)
    return code, out.getvalue()


def assert_wrote_nothing(tmp_path: Path, *also: str) -> None:
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["config.yaml", *also])


FAILURES = {
    "rate-limited": (
        Script(raw=lambda rid: answer(rid, {"error": "rate_limited", "retry_after_s": 5}, ok=False)),
        "the signer refused (rate_limited)",
    ),
    "forbidden": (Script(error="forbidden"), "the signer refused (forbidden)"),
    "unknown-error-code": (
        Script(raw=lambda rid: answer(rid, {"error": MARKER}, ok=False)),
        "answer does not match the schema of its op",
    ),
    "rid-not-echoed": (
        Script(raw=lambda rid: answer("0" * 32, {"crl": [ENTRY]})),
        "answer rid does not echo the request rid",
    ),
    "two-answer-lines": (
        Script(raw=lambda rid: answer(rid, {"crl": [ENTRY]}) * 2),
        "answer is not one line holding one strict JSON object",
    ),
    "not-json": (
        Script(raw=lambda rid: f"{MARKER}\n".encode()),
        "answer is not one line holding one strict JSON object",
    ),
    "oversized": (Script(raw=lambda rid: b"{" + b" " * 70000 + b"}\n"), "answer line exceeds its size cap"),
    "extra-key": (
        Script(raw=lambda rid: answer(rid, {"crl": [ENTRY], MARKER: 1})),
        "answer does not match the schema of its op",
    ),
    "terminal-escape-in-next-update": (
        Script(raw=lambda rid: answer(rid, {"crl": [{**ENTRY, "next_update": "2026-10-12\x1b[2J"}]})),
        "answer does not match the schema of its op",
    ),
    "negative-number": (
        Script(raw=lambda rid: answer(rid, {"crl": [{**ENTRY, "number": -1}]})),
        "answer does not match the schema of its op",
    ),
    "drip-past-the-deadline": (Script(drip_s=0.05), "signer did not answer in time"),
}


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_a_failed_signer_call_is_one_fixed_line_exit_1_and_writes_nothing(tmp_path, fake, monkeypatch, case):
    script, message = FAILURES[case]
    fake.scripts["crl_generate"] = script
    code, out = run_on(monkeypatch, tmp_path, fake.socket_path)
    assert (code, out) == (1, f"error: crl: {message}\n")
    assert MARKER not in out and str(tmp_path) not in out
    assert_wrote_nothing(tmp_path)


def test_a_signer_socket_answered_by_another_uid_is_refused_and_writes_nothing(tmp_path, fake, monkeypatch):
    code, out = run_on(monkeypatch, tmp_path, fake.socket_path, peer_uid=os.geteuid() + 1)
    assert (code, out) == (1, "error: crl: signer socket peer is not the signer user\n")
    assert fake.ops == [] and not (tmp_path / "state").exists()


def test_a_regular_file_in_place_of_the_signer_socket_is_refused_without_connecting(tmp_path, monkeypatch):
    planted = tmp_path / "not-a-socket"
    planted.write_text("x")
    code, out = run_on(monkeypatch, tmp_path, planted)
    assert (code, out) == (1, "error: crl: signer socket is a symlink, not a socket, or not owned by the signer user\n")
    assert_wrote_nothing(tmp_path, "not-a-socket")


def test_an_unknown_signer_user_is_refused_before_connecting(tmp_path):
    path = write_config(tmp_path)
    path.write_text(path.read_text().replace("user: nobody", "user: no-such-user-on-this-host"))
    out = io.StringIO()
    assert admin.main(["crl", "--config", str(path)], env={}, out=out) == 1
    assert out.getvalue() == "error: crl: signer.user is unknown, root or the plugin's own user\n"
    assert_wrote_nothing(tmp_path)
