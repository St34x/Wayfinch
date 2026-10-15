# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin config check`` never echoes a name that could carry credentials: profile names and gate lines
from Hermes, and a ``--config`` / ``--hermes-home`` URL (``Path`` collapses ``//``, so the raw argv string is tested).
The tool-name, route and ``--config`` cases are in test_config_check_edges.py."""

from __future__ import annotations

import io

import pytest

from glasses import admin

from test_config_check_edges import GOOD, SECRET, live_home, run_live

URL = f"https://u:{SECRET}@host/x"
BARE_URL = f"scheme://{SECRET}/x"  # no '@': only the '://' test catches it


def test_profile_named_like_a_credential_url_is_not_shown_in_its_problems(tmp_path):
    odd = admin.ProfileFacts("", "", frozenset({"web"}), frozenset({"web"}), 0, 0)
    facts = admin.HermesFacts(frozenset({"default", URL}), {"work": URL}, {"default": GOOD, URL: odd})
    code, out = run_live(live_home(tmp_path), facts)
    assert code == admin.EXIT_ERRORS and "approvals.timeout resolves to 0" in out and SECRET not in out, out


def test_gate_problem_naming_a_credential_looking_profile_is_not_shown(tmp_path):
    facts = admin.HermesFacts(
        frozenset({"default"}), {}, {"default": GOOD}, (f"profile {URL}: GLASSES_ALLOW_ALL_USERS is set",)
    )
    code, out = run_live(live_home(tmp_path), facts)
    assert code == admin.EXIT_ERRORS and "error: allow-all gate: " in out and SECRET not in out, out


def test_gate_problem_without_credentials_is_shown_as_it_is(tmp_path):
    facts = admin.HermesFacts(
        frozenset({"default"}), {}, {"default": GOOD}, ("profile work: GLASSES_ALLOW_ALL_USERS is set",)
    )
    _code, out = run_live(live_home(tmp_path), facts)
    assert "error: allow-all gate: profile work: GLASSES_ALLOW_ALL_USERS is set" in out


@pytest.mark.parametrize("flag", ["--config", "--hermes-home"])
@pytest.mark.parametrize("value", [URL, BARE_URL])
@pytest.mark.parametrize("live", [False, True])
def test_url_given_as_a_path_is_refused_in_one_line_without_the_value(flag, value, live):
    out = io.StringIO()
    code = admin.main(["config", "check", flag, value, *(["--live"] if live else [])], {}, out)
    lines = out.getvalue().splitlines()
    assert code == admin.EXIT_UNREADABLE and lines == [
        f"error: {flag}: not a local path (it contains '://'; the value is not shown)"
    ]


def test_paths_are_still_read_after_the_string_conversion(tmp_path):
    (tmp_path / "config.yaml").write_text("gateway: {}\n", encoding="utf-8")
    out = io.StringIO()
    by_home = admin.main(["config", "check", "--hermes-home", str(tmp_path)], {}, out)
    by_file = admin.main(["config", "check", "--config", str(tmp_path / "config.yaml")], {}, out)
    assert by_home == by_file == admin.EXIT_UNREADABLE  # both read the file: it has no glasses block
    assert out.getvalue().count("no gateway.platforms.glasses mapping") == 2, out.getvalue()


def test_a_plain_path_with_at_sign_is_still_hidden_not_refused(tmp_path):
    out = io.StringIO()
    admin.main(["config", "check", "--config", f"/nonexistent/u:{SECRET}@h/config.yaml"], {}, out)
    assert SECRET not in out.getvalue() and "path not shown" in out.getvalue(), out.getvalue()


# -- look-alikes and query strings: only a plain identifier is shown ---------------------------------------------------

LOOKALIKES = [
    f"user:{SECRET}\uff20host",  # FULLWIDTH COMMERCIAL AT
    f"s3\uff1a//{SECRET}",  # FULLWIDTH COLON
    f"s3:\uff0f\uff0f{SECRET}",  # FULLWIDTH SOLIDUS
    f"work?token={SECRET}",
    f"work;token={SECRET}",
    f"work#{SECRET}",
    f"work\u200b@{SECRET}",  # zero-width space
    f"wo\u0440k{SECRET}",  # Cyrillic er
]


@pytest.mark.parametrize("name", LOOKALIKES)
def test_look_alike_and_query_string_names_are_not_shown_as_profile_tool_or_gate_names(name, tmp_path):
    odd = admin.ProfileFacts("", "", frozenset({name, "web"}), frozenset({"web"}), 0, 0)
    facts = admin.HermesFacts(
        frozenset({"default", name}),
        {"work": name},
        {"default": GOOD, name: odd},
        (f"profile {name}: GLASSES_ALLOW_ALL_USERS is set",),
        known_toolsets=frozenset({"web"}),
    )
    code, out = run_live(live_home(tmp_path), facts)
    assert code == admin.EXIT_ERRORS and SECRET not in out and "error: allow-all gate: " in out, out
    unserved = admin.HermesFacts(frozenset({"default"}), {"work": name}, {"default": GOOD})
    _code, out = run_live(live_home(tmp_path / "again"), unserved)
    assert "is routed to profile" in out and SECRET not in out, out
