# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin crl`` against the fake signer: one ``crl_generate`` call, one line per CA, and every failed call
(signer down, timeout, refusal, unusable answer, unset or refused signer setting) a typed error with a fixed message
and a non-zero exit, never a traceback, a path or answer text; the path needs no ``cryptography``; ``ca init`` is gone.
``crl --check`` is covered in ``test_crl_check*.py``."""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

import fake_signer
import pytest

import glasses
from glasses import admin, signer_client
from glasses.config import DependencyMissing
from glasses.signer_client import SignerClient, Timeouts

FAST = Timeouts(connect_s=1.0, op_s=0.5, enrol_s=1.0)
OK_LINE = f"crl: CA {fake_signer.CA_ID}: CRL number 7, next update 2026-10-12\n"
MARKER = "answer-text-must-not-show"


@pytest.fixture
def fake():
    with fake_signer.running() as running:
        yield running


def write_config(tmp_path: Path, *signer: str) -> Path:
    path = tmp_path / "config.yaml"
    lines = ["gateway:", "  platforms:", "    glasses:", "      extra:", f"        state_dir: {tmp_path / 'state'}"]
    lines += ["        signer:", *(f"          {line}" for line in signer)] if signer else []
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def use_socket(monkeypatch, socket_path: Path) -> None:
    """``client_from_config`` returns a client on *socket_path* under the test's own uid (the real one refuses it)."""
    client = SignerClient(socket_path, os.geteuid(), timeouts=FAST)
    monkeypatch.setattr(signer_client, "client_from_config", lambda _cfg: client)


def run_crl(tmp_path: Path, *signer: str) -> tuple[int, str]:
    out = io.StringIO()
    code = admin.main(["crl", "--config", str(write_config(tmp_path, *signer))], env={}, out=out)
    return code, out.getvalue()


def test_crl_sends_one_crl_generate_and_prints_one_line_per_ca(tmp_path, fake, monkeypatch):
    use_socket(monkeypatch, fake.socket_path)
    assert run_crl(tmp_path) == (0, OK_LINE)
    assert fake.ops == ["crl_generate"]
    assert not (tmp_path / "state").exists()  # the plugin writes no CRL and no number record


def test_two_cas_in_rotation_overlap_give_two_lines(tmp_path, fake, monkeypatch):
    second = {"ca_id": "fedcba9876543210", "number": 1, "next_update": "2026-11-11T00:00:00Z"}
    first = {"ca_id": fake_signer.CA_ID, "number": 7, "next_update": fake_signer.TIME}
    fake.scripts["crl_generate"] = fake_signer.Script(raw=lambda rid: fake_signer.answer(rid, {"crl": [first, second]}))
    use_socket(monkeypatch, fake.socket_path)
    code, out = run_crl(tmp_path)
    assert (code, out) == (0, OK_LINE + "crl: CA fedcba9876543210: CRL number 1, next update 2026-11-11\n")


@pytest.mark.parametrize("error", ["crl_failed", "not_initialised", "internal"])
def test_a_signer_refusal_prints_its_code_and_exits_1(tmp_path, fake, monkeypatch, error):
    fake.scripts["crl_generate"] = fake_signer.Script(error=error)
    use_socket(monkeypatch, fake.socket_path)
    assert run_crl(tmp_path) == (1, f"error: crl: the signer refused ({error})\n")


def test_a_signer_that_is_down_is_a_fixed_error_and_exit_1(tmp_path, monkeypatch):
    use_socket(monkeypatch, tmp_path / "absent.sock")
    assert run_crl(tmp_path) == (1, "error: crl: signer connection failed\n")


def test_a_signer_that_does_not_answer_in_time_is_a_timeout_and_exit_1(tmp_path, fake, monkeypatch):
    fake.scripts["crl_generate"] = fake_signer.Script(delay_s=2.0)
    use_socket(monkeypatch, fake.socket_path)
    assert run_crl(tmp_path) == (1, "error: crl: signer did not answer in time\n")


UNUSABLE = {
    "closed-without-answer": (fake_signer.Script(close=True), "answer is not one line holding one strict JSON object"),
    "no-crl-entry": (
        fake_signer.Script(raw=lambda rid: fake_signer.answer(rid, {"crl": []})),
        "answer does not match the schema of its op",
    ),
    "foreign-text": (
        fake_signer.Script(raw=lambda rid: fake_signer.answer(rid, {"crl": [{"ca_id": MARKER}]})),
        "answer does not match the schema of its op",
    ),
}


@pytest.mark.parametrize("case", sorted(UNUSABLE))
def test_an_unusable_answer_is_its_fixed_message_without_answer_text(tmp_path, fake, monkeypatch, case):
    script, message = UNUSABLE[case]
    fake.scripts["crl_generate"] = script
    use_socket(monkeypatch, fake.socket_path)
    assert run_crl(tmp_path) == (1, f"error: crl: {message}\n")


@pytest.mark.parametrize(
    ("signer", "key"),
    [((), "signer.socket"), (("socket: /run/device-signer/signer.sock",), "signer.user")],
)
def test_an_unset_signer_key_exits_2_naming_it(tmp_path, signer, key):
    assert run_crl(tmp_path, *signer) == (2, f"error: {key} is not set in the glasses config\n")


def test_root_as_the_signer_user_is_refused_before_connecting(tmp_path):
    code, out = run_crl(tmp_path, "socket: /run/device-signer/signer.sock", "user: root")
    assert (code, out) == (1, "error: crl: signer.user is unknown, root or the plugin's own user\n")


def test_crl_needs_no_cryptography(tmp_path, fake, monkeypatch):
    monkeypatch.delattr(glasses, "crl_check", raising=False)
    monkeypatch.delitem(sys.modules, "glasses.crl_check", raising=False)
    monkeypatch.setitem(sys.modules, "cryptography", None)
    use_socket(monkeypatch, fake.socket_path)
    assert run_crl(tmp_path) == (0, OK_LINE)
    assert "glasses.crl_check" not in sys.modules


def test_crl_check_maps_only_a_missing_cryptography_to_exit_2(tmp_path, monkeypatch):
    config = str(write_config(tmp_path, f"public_dir: '{tmp_path / 'public'}'"))
    monkeypatch.delattr(glasses, "crl_check", raising=False)
    monkeypatch.delitem(sys.modules, "glasses.crl_check", raising=False)
    monkeypatch.setitem(sys.modules, "cryptography", None)
    out = io.StringIO()
    assert admin.main(["crl", "--check", "--config", config], env={}, out=out) == 2
    assert out.getvalue() == f"error: {DependencyMissing('cryptography')}\n"
    monkeypatch.setitem(sys.modules, "glasses.crl_check", None)
    with pytest.raises(ImportError):
        admin.main(["crl", "--check", "--config", config], env={}, out=io.StringIO())
    assert not (tmp_path / "state").exists()


def test_an_unreadable_config_exits_2(tmp_path):
    out = io.StringIO()
    assert admin.main(["crl", "--config", str(tmp_path / "absent.yaml")], env={}, out=out) == 2
    assert out.getvalue().startswith("error: ") and "Traceback" not in out.getvalue()


def test_ca_init_is_gone_a_usage_error_and_not_in_the_help(capsys):
    with pytest.raises(SystemExit) as info:
        admin.main(["ca", "init"], env={}, out=io.StringIO())
    assert info.value.code == 2
    parser = admin.build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert sorted(commands.choices) == ["config", "crl", "revoke"]
    assert "ca init" not in parser.format_help() and "{config,crl,revoke}" in parser.format_help()
    capsys.readouterr()
