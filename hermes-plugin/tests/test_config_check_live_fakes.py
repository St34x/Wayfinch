# SPDX-License-Identifier: Apache-2.0
"""``config check --live`` with fakes (no Hermes import, conftest.py): unset
``stt.provider`` is a warning (Hermes auto-detects) and ``--hermes-home`` reaches the Hermes facts.
The real-Hermes run with ``--hermes-home`` and a scratch HOME is in test_config_check_live.py."""

from __future__ import annotations

import io
import os
from dataclasses import replace

import pytest
import yaml

from glasses import admin
from glasses import config as gcfg
from glasses import hermes_facts as hf

GOOD = admin.ProfileFacts(
    stt_provider="local",
    stt_language="",
    glasses_tools=frozenset({"web"}),
    telegram_tools=frozenset({"web"}),
    clarify_timeout=0,
    approvals_timeout=300,
)


def probes(hermes):
    return admin.Probes(port_free=lambda _h, _p: True, resolves=lambda _h: True, hermes=hermes)


def facts(**profiles):
    return admin.HermesFacts(
        frozenset({"default", "work"}), {"work": "work"}, {"default": GOOD, "work": GOOD, **profiles}
    )


def write_home(home):
    (home / "glasses").mkdir(mode=0o700, parents=True)
    (home / "glasses").chmod(0o700)
    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "gateway": {
                "platforms": {
                    "glasses": {
                        "enabled": True,
                        "extra": {"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant", "work"]},
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    return home


def test_unset_stt_provider_is_a_warning_not_an_error(tmp_path):
    write_home(tmp_path)
    config = gcfg.load({"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant", "work"]})
    report = admin.live_problems(config, tmp_path, probes(lambda _c: facts(work=replace(GOOD, stt_provider=""))))
    assert report.errors == []
    (msg,) = report.warnings
    assert msg.startswith("profile work: stt.provider is not set — Hermes picks the first available")


def test_profile_problems_returns_errors_and_warnings_apart():
    report = admin.profile_problems("work", replace(GOOD, stt_provider="", clarify_timeout=5), ["fr"])
    assert len(report.warnings) == 1 and len(report.errors) == 1
    assert "clarify timeout resolves to a positive number" in report.errors[0]


# -- (d) the Hermes home of --live ------------------------------------------------------------------------


@pytest.mark.parametrize("before", [None, "/elsewhere"])
def test_hermes_home_is_set_during_the_facts_and_restored(tmp_path, monkeypatch, before):
    if before is None:
        monkeypatch.delenv("HERMES_HOME", raising=False)
    else:
        monkeypatch.setenv("HERMES_HOME", before)
    seen = []
    home = write_home(tmp_path / "home")

    def hermes(_cfg):
        seen.append(os.environ.get("HERMES_HOME"))
        return facts()

    args = ["config", "check", "--live", "--hermes-home", str(home)]
    assert admin.main(args, {}, io.StringIO(), probes(hermes)) == admin.EXIT_OK
    assert seen == [str(home)] and os.environ.get("HERMES_HOME") == before


def test_env_hermes_home_is_used_when_no_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    write_home(tmp_path)
    seen = []
    admin.main(
        ["config", "check", "--live"],
        {"HERMES_HOME": str(tmp_path)},
        io.StringIO(),
        probes(lambda _c: seen.append(os.environ.get("HERMES_HOME")) or facts()),
    )
    assert seen == [str(tmp_path)] and "HERMES_HOME" not in os.environ


def test_live_refuses_a_config_outside_the_hermes_home(tmp_path):
    other = write_home(tmp_path / "other")
    out = io.StringIO()
    called = []
    args = ["config", "check", "--live", "--hermes-home", str(tmp_path), "--config", str(other / "config.yaml")]
    assert admin.main(args, {}, out, probes(lambda _c: called.append(1) or facts())) == admin.EXIT_UNREADABLE
    assert called == [] and "is not its config.yaml" in out.getvalue() and "--hermes-home" in out.getvalue()
    out = io.StringIO()  # the home's own config.yaml is fine
    args = ["config", "check", "--live", "--hermes-home", str(other), "--config", str(other / "config.yaml")]
    assert admin.main(args, {}, out, probes(lambda _c: facts())) == admin.EXIT_OK, out.getvalue()


def test_static_check_with_config_outside_the_home_still_works(tmp_path):
    other = write_home(tmp_path / "other")
    out = io.StringIO()
    args = ["config", "check", "--hermes-home", str(tmp_path), "--config", str(other / "config.yaml")]
    assert admin.main(args, {}, out) == admin.EXIT_OK, out.getvalue()


# -- copied Hermes behaviour fails loudly on an unexpected shape --------------------------------------------


def test_contract_error_is_reported_clearly(tmp_path):
    write_home(tmp_path)
    config = gcfg.load({"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant", "work"]})

    def broken(_cfg):
        raise hf.HermesContractError("Hermes config section 'stt' is a list, not a mapping")

    (msg,) = admin.live_problems(config, tmp_path, probes(broken)).errors
    assert (
        msg.startswith("hermes: unexpected Hermes behaviour — Hermes config section 'stt' is a list") and "T1.45" in msg
    )


def test_import_error_names_the_missing_symbol(tmp_path):
    write_home(tmp_path)
    config = gcfg.load({"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant", "work"]})

    def moved(_cfg):
        raise ImportError("cannot import name", name="tools.transcription_common")

    (msg,) = admin.live_problems(config, tmp_path, probes(moved)).errors
    assert "(ImportError: tools.transcription_common)" in msg


@pytest.mark.parametrize("config,key", [([], "stt"), ({"stt": "local"}, "stt"), ({"approvals": [1]}, "approvals")])
def test_section_shape_is_asserted(config, key):
    with pytest.raises(hf.HermesContractError):
        hf.section(config, key)


def test_approvals_timeout_copy():
    assert hf.approvals_timeout({}) == 300 and hf.approvals_timeout({"approvals": None}) == 300
    assert hf.approvals_timeout({"approvals": {"timeout": "0"}}) == 0
    assert hf.approvals_timeout({"approvals": {"timeout": "x"}}) == 300
    with pytest.raises(hf.HermesContractError):
        hf.approvals_timeout({"approvals": "60"})


@pytest.mark.parametrize(
    "stt,provider,env,expected",
    [
        ({"elevenlabs": {"language_code": "fr"}, "language": "en"}, "elevenlabs", "", "fr"),  # provider alias
        ({"local": {"language_code": "fr"}, "language": "en"}, "local", "", "en"),  # alias is per provider
        ({"language": ""}, "local", "en", "en"),  # HERMES_LOCAL_STT_LANGUAGE
        ({"language": "de"}, "local", "en", "de"),
    ],
)
def test_stt_language_env_and_aliases(stt, provider, env, expected):
    assert hf.stt_language(stt, provider, env) == expected


def test_tool_names_shape():
    assert hf._tool_names({"web"}) == frozenset({"web"})
    for bad in (["web"], "web", {1}):
        with pytest.raises(hf.HermesContractError):
            hf._tool_names(bad)
