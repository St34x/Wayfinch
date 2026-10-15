# SPDX-License-Identifier: Apache-2.0
"""``config check --live``: the filters between Hermes' answers and the output, one rule at a time.

What is hidden (``.env`` values, also in warnings), what is never shown (a gate line that is not of the known shape,
even with a harmless head), what a ``.env`` may not rename (temporary directories, the user profile), and that
Hermes' warnings and log level are put back as they were.
"""

from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path

import pytest

from glasses import admin, admin_names
from glasses import hermes_env as he

from test_config_check_edges import GOOD, _hermes_run, _live_scratch, _scratch, live_home, run_live

GATE_LINE = (
    "profile default: GATEWAY_ALLOW_ALL_USERS is enabled and GLASSES_ALLOWED_USERS is empty: the gateway "
    "would admit every glasses sender; set GLASSES_ALLOWED_USERS"
)


# -- Hermes' warnings and log level ------------------------------------------------------------------------------


def test_silenced_hermes_drops_warnings_and_puts_the_log_level_back():
    logging.disable(logging.NOTSET)
    try:
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            with he.silenced_hermes():
                warnings.warn("quotes a config value", UserWarning, stacklevel=1)
                assert logging.root.manager.disable == logging.CRITICAL
            assert logging.root.manager.disable == logging.NOTSET
            warnings.warn("outside the block", UserWarning, stacklevel=1)
        assert [str(w.message) for w in seen] == ["outside the block"]
    finally:
        logging.disable(logging.NOTSET)


def test_silenced_hermes_keeps_a_level_somebody_else_set():
    logging.disable(logging.WARNING)
    try:
        with he.silenced_hermes():
            pass
        assert logging.root.manager.disable == logging.WARNING
    finally:
        logging.disable(logging.NOTSET)


# -- names a .env may not rename ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["TMP", "TEMP", "TMPDIR", "USERPROFILE", "HOME", "XDG_CACHE_HOME", "LD_AUDIT"])
def test_a_dotenv_may_not_move_the_temporary_directory_or_the_user_profile(name):
    assert not he.overlay_name_ok(name, "/elsewhere")


# -- the shape of a gate line --------------------------------------------------------------------------------------


def test_a_gate_line_of_the_known_shape_is_shown_as_it_is():
    assert admin_names.shown_problem(GATE_LINE) == GATE_LINE


@pytest.mark.parametrize(
    "tail",
    ["\nconfig check: OK (0 error(s), 0 warning(s))", "\x1b[2J", "\u200b", " \u2028forged", "\r", "\t"],
    ids=["line break + forged verdict", "escape", "zero-width space", "line separator", "carriage return", "tab"],
)
def test_a_gate_line_with_anything_but_printable_ascii_after_the_shape_is_not_shown(tail):
    shown = admin_names.shown_problem(GATE_LINE + tail)
    assert shown.startswith("<a gate problem naming a value that is not shown") and "forged" not in shown
    assert "\n" not in shown and "\x1b" not in shown


# -- .env values are hidden in warnings as well ----------------------------------------------------------------------


def test_a_dotenv_value_is_hidden_in_a_warning_line(tmp_path):
    odd = admin.ProfileFacts("", "", GOOD.glasses_tools, GOOD.telegram_tools, 0, 300)  # no stt.provider: a warning
    facts = admin.HermesFacts(
        frozenset({"default", "workname"}),
        {"work": "workname"},
        {"default": GOOD, "workname": odd},
        env_values=frozenset({"workname"}),
    )
    code, out = run_live(live_home(tmp_path), facts)
    assert code == admin.EXIT_OK and "stt.provider is not set" in out, out
    assert "workname" not in out and f"warning: profile {he.MASK}: stt.provider" in out, out


# -- real Hermes: the toolset table and the values the masking covers ------------------------------------------------

FACTS_RUNNER = f"""\
import json, sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from glasses import config as gcfg
from glasses import hermes_facts as hf
facts = hf.hermes_facts(gcfg.load({{"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant", "work"]}}))
print(json.dumps({{"known": sorted(facts.known_toolsets), "values": sorted(facts.env_values)}}))
"""


def test_the_toolset_table_and_the_masked_values_of_every_served_profile():
    with _scratch() as tmp:
        home = _live_scratch(Path(tmp))
        (home / ".env").write_text("LAUNCH_KEY=launchSecretWord\nSHORT=ab\n", encoding="utf-8")
        (home / "profiles" / "work" / ".env").write_text("WORK_KEY=workSecretWord\n", encoding="utf-8")
        proc = _hermes_run(FACTS_RUNNER, tmp)
    assert proc.returncode == 0, proc.stderr[-2000:]
    seen = json.loads(proc.stdout.strip().splitlines()[-1])
    assert {"web", "terminal", "hermes-glasses"} <= set(seen["known"]), seen["known"][:20]
    assert seen["values"] == ["launchSecretWord", "workSecretWord"], seen["values"]  # two characters: not masked
