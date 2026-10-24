# SPDX-License-Identifier: Apache-2.0
"""``edge.socket`` / ``edge.user`` (the unix-socket hop of the TLS edge) and the retired ``host`` / ``port`` keys.

The negatives are the contract of the listener: every value it will trust has passed these rules, and an error
names the key without echoing a value that could carry a credential. ``host`` and ``port`` are ordinary unknown
keys now (a warning, never an error, never echoed). No Hermes import (conftest.py).
"""

from __future__ import annotations

import dataclasses
import io

import pytest
import yaml

from glasses import admin
from glasses import config as gcfg

SOCK = "/run/glasses-plugin/edge.sock"


def errors_of(extra):
    return gcfg.check(extra).errors


def one_error(extra, prefix):
    (msg,) = errors_of(extra)
    assert msg.startswith(prefix), msg
    assert " — " in msg  # every message carries a hint
    return msg


# -- defaults ----------------------------------------------------------------------------------------------


def test_unset_edge_loads_as_empty_strings():
    for extra in ({}, {"edge": None}, {"edge": {}}, {"edge": {"socket": None, "user": None}}):
        cfg = gcfg.load(extra)
        assert (cfg.edge_socket, cfg.edge_user) == ("", ""), extra


def test_defaults_declare_edge_and_no_host_or_port():
    assert gcfg.DEFAULTS["edge"] == {"socket": "", "user": ""}
    assert "host" not in gcfg.DEFAULTS and "port" not in gcfg.DEFAULTS
    props = gcfg.schema()["properties"]
    assert "host" not in props and "port" not in props
    names = {f.name for f in dataclasses.fields(gcfg.GlassesConfig)}
    assert {"edge_socket", "edge_user", "bridge_port"} <= names
    assert not names & {"host", "port"}


def test_both_keys_are_built_since_the_listener_gives_them_an_effect():
    edge = gcfg.schema()["properties"]["edge"]
    assert "x-status" not in edge
    for leaf in edge["properties"].values():
        assert "x-status" not in leaf and leaf["x-echo"] is True
        assert leaf["x-error"] and leaf["x-hint"]
    assert edge["properties"]["socket"]["maxLength"] == 107


# -- edge.socket -------------------------------------------------------------------------------------------


def test_socket_accepts_an_absolute_path_and_exactly_107_bytes():
    assert gcfg.load({"edge": {"socket": SOCK}}).edge_socket == SOCK
    edge_107 = "/" + "a" * 106
    assert gcfg.load({"edge": {"socket": edge_107}}).edge_socket == edge_107
    three_byte = "/" + "\u20ac" * 35 + "a"  # 36 characters, 107 bytes
    assert len(three_byte.encode()) == 107
    assert gcfg.load({"edge": {"socket": three_byte}}).edge_socket == three_byte
    assert gcfg.load({"edge": {"socket": "/run/a..b/x.sock"}}).edge_socket == "/run/a..b/x.sock"


@pytest.mark.parametrize(
    "path",
    [
        "run/edge.sock",
        "./edge.sock",
        " /run/edge.sock",
        "/run/edge.sock ",
        "/run/../etc/edge.sock",
        "/run/..",
        "/..",
        "/run/edge\x00.sock",
        "/run/edge\n.sock",
        "/run/edge\r.sock",
        "/run/edge\u2028.sock",
        "/" + "a" * 107,  # 108 bytes
        "/" + "\u20ac" * 35 + "ab",  # 37 characters but 108 bytes
        "/" + "\U0001f600" * 27,  # 28 characters, 109 bytes
        "unix:///run/edge.sock",
        "http://example.com/edge.sock",
        "https://user:pw@example.com/edge.sock",
        5,
        True,
        ["/run/edge.sock"],
        {"a": "/run/edge.sock"},
    ],
    ids=lambda v: repr(v)[:30],
)
def test_socket_negatives_name_the_key(path):
    one_error({"edge": {"socket": path}}, "edge.socket:")


@pytest.mark.parametrize("path", ["https://user:pw@example.com/edge.sock", "/run/u:pw@host/edge.sock/.."])
def test_socket_error_does_not_echo_a_credential_shape(path):
    msg = one_error({"edge": {"socket": path}}, "edge.socket:")
    assert "pw" not in msg and "example.com" not in msg


# -- edge.user ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("user", ["edge-proxy", "_edge", "www-data", "a", "a" * 32, "nginx2"])
def test_user_accepts_a_system_user_name(user):
    assert gcfg.load({"edge": {"user": user}}).edge_user == user


@pytest.mark.parametrize(
    "user",
    [
        "root",
        "0",
        "1000",
        "Edge",
        "EDGE-PROXY",
        "edge proxy",
        "edge\tproxy",
        " edge",
        "edge ",
        "edge\n",
        "a" * 33,
        "-edge",
        "ed.ge",
        "edge:proxy",
        "edge@example.com",
        "root\n",
        0,
        1000,
        True,
        ["edge"],
        {"a": "edge"},
    ],
    ids=lambda v: repr(v)[:30],
)
def test_user_negatives_name_the_key(user):
    one_error({"edge": {"user": user}}, "edge.user:")


def test_user_root_is_refused_by_name_and_other_names_are_not():
    assert "root" in one_error({"edge": {"user": "root"}}, "edge.user:")
    assert errors_of({"edge": {"user": "rootless"}}) == []


def test_user_error_does_not_echo_a_credential_shape():
    msg = one_error({"edge": {"user": "u:s3cr3t@host"}}, "edge.user:")
    assert "s3cr3t" not in msg


# -- the section -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("edge", ["nginx", 5, True, ["socket"], "/run/edge.sock"], ids=repr)
def test_a_non_mapping_edge_is_a_type_error(edge):
    one_error({"edge": edge}, "edge:")


def test_both_non_strings_give_two_errors_naming_both_keys():
    msgs = errors_of({"edge": {"socket": 5, "user": 7}})
    assert len(msgs) == 2
    assert {m.split(":", 1)[0] for m in msgs} == {"edge.socket", "edge.user"}


def test_the_retired_edge_keys_warn_as_unknown_nested_keys():
    result = gcfg.check({"edge": {"kind": "nginx", "tls_dir": "/etc/nginx/glasses", "socket": SOCK}})
    assert result.errors == []
    assert result.warnings == [
        "edge.<an unknown key of 4 characters>: unknown key, ignored",
        "edge.<an unknown key of 7 characters>: unknown key, ignored",
    ]
    assert result.config is not None and result.config.edge_socket == SOCK


def test_a_misspelt_edge_key_gets_a_hint():
    result = gcfg.check({"edge": {"sokcet": SOCK, "usr": "edge-proxy"}})
    assert "edge.sokcet: unknown key, ignored — did you mean edge.socket?" in result.warnings
    assert "edge.usr: unknown key, ignored — did you mean edge.user?" in result.warnings


# -- host / port are ordinary unknown keys -----------------------------------------------------------------


@pytest.mark.parametrize("key,value", [("host", "127.0.0.1"), ("port", 8765), ("host", "0.0.0.0"), ("port", "x")])
def test_removed_keys_are_a_warning_and_the_value_is_never_echoed(key, value):
    result = gcfg.check({key: value})
    assert result.errors == []
    assert result.warnings == [f"<an unknown key of {len(key)} characters>: unknown key, ignored"]
    assert str(value) not in " ".join(result.warnings)
    assert gcfg.load({key: value}) == gcfg.load({})


def test_bridge_port_is_still_validated_and_no_longer_has_to_differ_from_anything():
    assert gcfg.load({"bridge_port": 8765}).bridge_port == 8765
    assert gcfg.load({"bridge_port": 1}).bridge_port == 1
    for bad in (0, 65536, "8766", True, 8766.0):
        one_error({"bridge_port": bad}, "bridge_port:")
    node = gcfg.schema()["properties"]["bridge_port"]
    assert "x-errors" not in node and "differ" not in node["description"]
    assert "!= bridge_port" not in gcfg.schema()["description"]


# -- through the real CLI ----------------------------------------------------------------------------------

GLASSES = {"enabled": True, "extra": {"public_url": "wss://glasses.example.com/ws"}}


def run_check(tmp_path, glasses):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"gateway": {"platforms": {"glasses": glasses}}}), encoding="utf-8")
    out = io.StringIO()
    return admin.main(["config", "check", "--config", str(path)], {}, out), out.getvalue()


@pytest.mark.parametrize(
    "glasses",
    [
        {**GLASSES, "extra": {**GLASSES["extra"], "host": "127.0.0.1", "port": 8765}},
        {**GLASSES, "host": "127.0.0.1", "port": 8765},  # platform level: the gateway merge promotes it into extra
    ],
    ids=["under-extra", "platform-level"],
)
def test_cli_warns_about_host_and_port_exits_0_and_never_echoes_them(tmp_path, glasses):
    code, out = run_check(tmp_path, glasses)
    assert code == admin.EXIT_OK, out
    assert "warning: glasses.<an unknown key of 4 characters>: unknown key, ignored" in out
    assert out.count("unknown key, ignored") == 2 and "127.0.0.1" not in out and "8765" not in out
    assert "deprecated" not in out.lower() and "removed" not in out.lower()
    assert out.strip().endswith("config check: OK (0 error(s), 2 warning(s))")


def test_cli_accepts_the_edge_section_and_names_a_bad_edge_key(tmp_path):
    ok = {**GLASSES, "extra": {**GLASSES["extra"], "edge": {"socket": SOCK, "user": "edge-proxy"}}}
    code, out = run_check(tmp_path, ok)
    assert code == admin.EXIT_OK and out.strip() == "config check: OK (0 error(s), 0 warning(s))", out
    bad = {**GLASSES, "extra": {**GLASSES["extra"], "edge": {"socket": "run/x.sock", "user": "root"}}}
    code, out = run_check(tmp_path, bad)
    assert code == admin.EXIT_ERRORS
    assert "error: glasses.edge.socket:" in out and "error: glasses.edge.user:" in out
