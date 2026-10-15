# SPDX-License-Identifier: Apache-2.0
"""T1.54: config.schema.json, schema-driven messages, the example config, planted mistakes."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import pytest
import yaml
from jsonschema import Draft202012Validator

from glasses import config as gcfg

from conftest import PLUGIN_ROOT

REPO = PLUGIN_ROOT.parent
EXAMPLE = REPO / "deploy" / "examples" / "config.example.yaml"
SECRET = "s3cr3t-value-never-shown"


def example_extra() -> dict:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    return data["gateway"]["platforms"]["glasses"]["extra"]


def protocol_defs() -> dict:
    return json.loads((REPO / "protocol" / "envelope.schema.json").read_text(encoding="utf-8"))["$defs"]


def leaf_nodes(node: dict, path: str = ""):
    """Every property node of the schema (nested sections included) with its dotted path."""
    for key, sub in node.get("properties", {}).items():
        yield f"{path}{key}", sub
        yield from leaf_nodes(sub, f"{path}{key}.")


# --------------------------------------------------------------------------------------------
# the schema itself
# --------------------------------------------------------------------------------------------


def test_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(gcfg.schema())
    assert gcfg.schema()["additionalProperties"] is False


def test_schema_covers_every_key_and_defaults_match():
    props = gcfg.schema()["properties"]
    assert set(props) == set(gcfg.DEFAULTS)
    for key, default in gcfg.DEFAULTS.items():
        assert props[key]["default"] == default, key
    for section in ("long_answers", "signer"):
        nested = {k: v["default"] for k, v in props[section]["properties"].items()}
        assert nested == gcfg.DEFAULTS[section], section


def test_every_property_has_a_message_and_closed_sections():
    for path, node in leaf_nodes(gcfg.schema()):
        assert node.get("x-error"), path
        if "properties" in node:
            assert node["additionalProperties"] is False, path


@pytest.mark.parametrize("name", ["lane", "platform", "langTag"])
def test_name_rules_equal_the_protocol(name):
    ours, theirs = gcfg.schema()["$defs"][name], protocol_defs()[name]
    assert (ours["pattern"], ours["maxLength"], ours["not"]) == (theirs["pattern"], theirs["maxLength"], theirs["not"])


def test_bounds_mirror_the_code():
    props = gcfg.schema()["properties"]
    assert props["lanes"]["maxItems"] == gcfg.MAX_LANES
    assert (props["long_threshold"]["minimum"], props["long_threshold"]["maximum"]) == (1, gcfg.MAX_LONG_THRESHOLD)
    assert gcfg.schema()["$defs"]["lane"]["pattern"] == f"^{gcfg.LANE_RE.pattern}$"


# --------------------------------------------------------------------------------------------
# the example config
# --------------------------------------------------------------------------------------------


def test_example_validates_cleanly():
    result = gcfg.check(example_extra())
    assert (result.errors, result.warnings) == ([], [])
    cfg = result.config
    assert cfg.public_url == "wss://glasses.example.com/ws" and cfg.lanes == ("assistant", "work")


def test_example_validates_against_the_raw_schema_too():
    assert list(Draft202012Validator(gcfg.schema()).iter_errors(example_extra())) == []


def strings(node):
    """Every string value of a parsed YAML document."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)


HOST_NAME = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*")
URL_TOKEN = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
DOTTED = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::\d+)?")  # a bare host (or IP) value
LOOPBACK = frozenset({"127.0.0.1", "localhost"})


def example_host(url_or_host):
    """Whether a URL or bare host names example.com or a subdomain of it: the parsed host, never a substring test
    (`evil.net/?example.com`, `example.com.evil.net` fail). Fails closed on anything a browser could read differently:
    a backslash (WHATWG reads it as `/`, urlsplit does not) or a host that is not plain lower-case ASCII labels."""
    if "\\" in url_or_host:
        return False
    try:
        host = urlsplit(url_or_host if "://" in url_or_host else f"//{url_or_host}").hostname or ""
    except ValueError:
        return False
    return HOST_NAME.fullmatch(host) is not None and (host == "example.com" or host.endswith(".example.com"))


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("wss://glasses.example.com/ws", True),
        ("stt.example.com", True),
        ("wss://example.com.evil.net/ws", False),
        ("wss://evil.net/?next=example.com", False),
        ("wss://example.com\x40evil.net/ws", False),
        (r"https://evil.net\.example.com/", False),
        (r"https://evil.net\@example.com/", False),
        ("evil.net", False),
        ("https://evil.net%2f.example.com/", False),
    ],
)
def test_example_host_check_parses_the_host(value, ok):
    assert example_host(value) is ok


def test_example_names_nothing_private():
    text = EXAMPLE.read_text(encoding="utf-8")
    values = list(strings(yaml.safe_load(text)))
    # URL tokens of the raw text (YAML comments too) and of every value, plus bare host values like `evil.net`.
    urls = [token.rstrip("`'\").,;:|>") for token in URL_TOKEN.findall(text)] + [v for v in values if "://" in v]
    hosts = [value for value in values if DOTTED.fullmatch(value) and value not in LOOPBACK]
    assert urls
    bad = [value for value in urls + hosts if not example_host(value)]
    assert not bad, bad
    for word in ("/home/", "/var/lib"):
        assert word not in text, word


# --------------------------------------------------------------------------------------------
# planted mistakes: one precise message each, no secret echoed
# --------------------------------------------------------------------------------------------


def plant(path: str, value):
    extra = example_extra()
    *parents, last = path.split(".")
    node = extra
    for key in parents:
        node = node.setdefault(key, {})
    node[last] = value
    return extra


PLANTED = [
    ("host", "0.0.0.0", "host", "loopback"),
    ("port", "8765", "port", "integer 1-65535"),
    ("bridge_port", 8765, "bridge_port", "must differ from port"),
    ("public_url", f"https://user:{SECRET}@glasses.example.com/ws", "public_url", "wss://"),
    ("lanes", ["assistant", "Work"], "lanes[1]", "lane name"),
    ("lanes", ["assistant", "work", "work"], "lanes", "must not repeat"),
    ("home_lane", "elsewhere", "home_lane", "must be one of lanes"),
    ("client_cert_encoding", "pem", "client_cert_encoding", "pem-urlescaped, der-base64"),
    ("long_threshold", 0, "long_threshold", "1-512"),
    ("long_answers.platform", "glasses", "long_answers.platform", "another platform"),
    ("signer.user", "Signer", "signer.user", "user name"),
    ("signer.public_dir", "relative/dir", "signer.public_dir", "absolute path"),
    ("state_dir", "/srv/glasses\n", "state_dir", "absolute path"),
    ("meeting.language_hints", ["en", "en"], "meeting.language_hints", "must not repeat"),
]


@pytest.mark.parametrize("path,value,where,problem", PLANTED, ids=[f"{p[0]}={p[1]!r}"[:40] for p in PLANTED])
def test_planted_mistake_gives_one_precise_message(path, value, where, problem):
    result = gcfg.check(plant(path, value))
    assert result.config is None
    (msg,) = result.errors
    assert msg.startswith(f"{where}: ") and problem in msg, msg
    assert " (got " in msg and " — " in msg, msg
    assert SECRET not in msg


def test_a_typo_is_a_warning_with_a_suggestion():
    extra = example_extra()
    extra["long_treshold"] = extra.pop("long_threshold")
    result = gcfg.check(extra)
    assert result.errors == []
    assert result.warnings == ["long_treshold: unknown key, ignored — did you mean long_threshold?"]


def test_nested_unknown_key_suggestion():
    assert gcfg.check({"signer": {"uesr": "signer"}}).warnings == [
        "signer.uesr: unknown key, ignored — did you mean signer.user?"
    ]


@pytest.mark.parametrize(
    "value,shown",
    [
        ("0.0.0.0", '"0.0.0.0"'),
        ("::", '"::"'),
        ("192.0.2.10", '"192.0.2.10"'),
        ("localhost", '"localhost"'),
        (8765, "8765"),
        (None, "null"),
        (["127.0.0.1"], "a list of 1 item"),
    ],
)
def test_non_loopback_host_refused_and_shown(value, shown):
    (msg,) = gcfg.check({"host": value}).errors
    assert msg.startswith("host: ") and f"(got {shown})" in msg


@pytest.mark.parametrize(
    "key,value",
    [
        ("allow_all_users", True),
        ("allowed_users", "*"),
        ("allow_from", ["*"]),
        ("dm_policy", "open"),
    ],
)
def test_allow_all_settings_refused_without_echo(key, value):
    result = gcfg.check({key: value, "lanes": [SECRET[:10]]})
    msgs = [e for e in result.errors if e.startswith(f"{key}: ")]
    assert len(msgs) == 1 and "not shown" in msgs[0] and gcfg.ALLOWED_USERS_ENV in msgs[0]
    assert result.config is None


def test_unknown_key_values_are_never_shown():
    result = gcfg.check({"token": SECRET, "signer": {"password": SECRET}})
    assert SECRET not in " ".join(result.errors + result.warnings)


def test_public_url_value_never_shown_but_its_size_is():
    (msg,) = gcfg.check({"public_url": f"wss://u:{SECRET}@glasses.example.com/ws"}).errors
    assert SECRET not in msg and "glasses.example.com" not in msg and "a string of" in msg


def test_long_echo_is_truncated():
    (msg,) = gcfg.check({"signer": {"user": "x" * 500}}).errors
    assert '"' + "x" * gcfg.ECHO_MAX_CHARS + '"...' in msg and "x" * (gcfg.ECHO_MAX_CHARS + 1) not in msg


@pytest.mark.parametrize("value", [8765.0, True, float("nan"), "8765", 2**70])
def test_integers_are_strict(value):
    (msg,) = gcfg.check({"port": value}).errors
    assert msg.startswith("port: must be an integer 1-65535")


def test_every_error_is_reported_once_per_path():
    result = gcfg.check({
        "host": "0.0.0.0",
        "port": "x",
        "lanes": ["A", "b c"],
        "signer": {"user": 1},
        "long_answers": {"platform": "Glasses"},
        "meeting": {"max_minutes": 0},
    })
    paths = [e.split(":", 1)[0] for e in result.errors]
    assert sorted(paths) == sorted([
        "host",
        "port",
        "lanes[0]",
        "lanes[1]",
        "signer.user",
        "long_answers.platform",
        "meeting.max_minutes",
    ])


# --------------------------------------------------------------------------------------------
# reserved meeting section
# --------------------------------------------------------------------------------------------


def test_meeting_section_reserved_and_validated():
    ok = {
        "enabled": False,
        "provider": "soniox",
        "endpoint": "",
        "language_hints": ["en", "pt-BR"],
        "transcript_days": 30,
        "max_minutes": 180,
        "daily_hours": 0,
        "live_notes": False,
        "llm": {"provider": "", "model": ""},
    }
    cfg = gcfg.load({"meeting": ok})
    assert cfg.language_hints == ("en", "pt-BR")
    assert gcfg.load({}).language_hints == ()
    tags = [f"x{chr(97 + i)}" for i in range(9)]
    bad = gcfg.check({"meeting": {"provider": "other", "language_hints": tags}}).errors
    assert sorted(e.split(":", 1)[0] for e in bad) == ["meeting.language_hints", "meeting.provider"]


def test_validate_config_logs_glasses_prefixed_messages(caplog, no_allow_all):
    import glasses

    assert glasses.validate_config({"extra": {"host": "0.0.0.0"}}) is False
    assert any("glasses.host: must be a loopback IP literal" in r.getMessage() for r in caplog.records)
