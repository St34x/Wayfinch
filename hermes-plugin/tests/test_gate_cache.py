# SPDX-License-Identifier: Apache-2.0
"""Validity of the allow-all gate's verdict cache (``glasses.gate.VerdictCache``): one second, then always recomputed.

A verdict, clean or not, is reused for ``ttl`` seconds of a monotonic clock and is then recomputed from scratch;
nothing renews it (no file stamp: the gateway's scope also reads the administrator-managed ``.env``, the external-secret
snapshot and ``config.yaml`` ``secrets:``). The clock is injected: no test sleeps. Edges (threads, errors, the event
loop) are in test_gate_cache_edges.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from glasses import gate

from test_gate import SENTINEL, Hermes, install_hermes

OPEN_SWITCH = {"GLASSES_ALLOW_ALL_USERS": "true"}


@pytest.fixture
def install():
    """The stand-in Hermes modules stay in ``sys.modules`` until this test ends (before conftest's leak check)."""
    with pytest.MonkeyPatch.context() as mp:
        yield lambda hermes: install_hermes(mp, hermes)


class Counting:
    """A fake gate: ``compute`` returns ``problems`` and counts; ``clock`` returns ``now``."""

    def __init__(self):
        self.now = 0.0
        self.problems: list[str] = []
        self.error: BaseException | None = None
        self.computed = 0

    def clock(self):
        return self.now

    def compute(self):
        self.computed += 1
        if self.error is not None:
            raise self.error
        return list(self.problems)

    def cache(self, ttl=1.0):
        return gate.VerdictCache(ttl=ttl, clock=self.clock, compute=self.compute)


def test_a_thousand_calls_inside_the_window_recompute_once():
    fake = Counting()
    cache = fake.cache()
    assert all(cache.violations() == [] for _ in range(1000))
    assert fake.computed == 1


def test_a_clean_verdict_older_than_one_second_is_recomputed_although_nothing_changed():
    fake = Counting()
    cache = fake.cache()
    cache.violations()
    fake.now = 0.999
    cache.violations()
    assert fake.computed == 1  # still valid
    fake.now = 1.0
    cache.violations()
    assert fake.computed == 2  # exactly one second: expired, recomputed, not renewed
    for second in (2.0, 3.0, 4.0):  # and so on, once per second, for ever
        fake.now = second
        cache.violations()
    assert fake.computed == 5


def test_a_recomputed_verdict_is_valid_for_a_full_second_from_its_own_recompute():
    fake = Counting()
    cache = fake.cache()
    cache.violations()
    fake.now = 1.4
    cache.violations()  # recompute at 1.4
    fake.now = 2.39
    cache.violations()
    assert fake.computed == 2
    fake.now = 2.4
    cache.violations()
    assert fake.computed == 3


def test_a_refusal_is_recomputed_after_one_second_like_an_all_clear():
    fake = Counting()
    fake.problems = ["profile dev: bad"]
    cache = fake.cache()
    assert all(cache.violations() == ["profile dev: bad"] for _ in range(100))
    assert fake.computed == 1
    fake.problems, fake.now = [], 1.0
    assert cache.violations() == []
    assert fake.computed == 2


def test_a_change_inside_the_window_waits_for_the_window_to_end():
    fake = Counting()
    cache = fake.cache()
    assert cache.violations() == []
    fake.problems, fake.now = ["profile dev: bad"], 0.99
    assert cache.violations() == []  # still inside the one-second window
    fake.now = 1.0
    assert cache.violations() == ["profile dev: bad"]


# The scope dict stands for what the gateway reads besides <home>/.env (the administrator-managed .env, the
# external-secret snapshot, config.yaml secrets:): it changes in memory and no file the profile owns is touched, so a
# stamp of <home>/.env would not move. The real compute (``gate.violations``) runs against it.
def test_a_switch_added_in_a_scope_no_file_stamp_could_see_is_picked_up_after_a_second(install):
    hermes = install(Hermes({"dev": {}}))
    fake = Counting()
    cache = gate.VerdictCache(clock=fake.clock)
    assert cache.violations() == []
    hermes.scopes["dev"].update(OPEN_SWITCH)
    fake.now = 0.99
    assert cache.violations() == []  # inside the window
    for second in (1.0, 2.0, 3.5, 10.0):  # every expiry sees it, however long the scope stayed unchanged before
        fake.now = second
        (problem,) = cache.violations()
        assert problem.startswith("profile dev: GLASSES_ALLOW_ALL_USERS is set")


def test_a_switch_removed_in_a_scope_no_file_stamp_could_see_is_picked_up_after_a_second(install):
    hermes = install(Hermes({"dev": dict(OPEN_SWITCH)}))
    fake = Counting()
    cache = gate.VerdictCache(clock=fake.clock)
    assert len(cache.violations()) == 1
    hermes.scopes["dev"].clear()
    fake.now = 0.99
    assert len(cache.violations()) == 1
    fake.now = 1.0
    assert cache.violations() == []


def test_a_changed_process_switch_is_seen_at_once(monkeypatch):
    fake = Counting()
    cache = fake.cache()
    cache.violations()
    monkeypatch.setenv("GLASSES_ALLOW_ALL_USERS", "1")
    fake.problems = ["process environment: bad"]
    assert cache.violations() == ["process environment: bad"]  # same clock reading: no waiting for the ttl
    monkeypatch.delenv("GLASSES_ALLOW_ALL_USERS")
    fake.problems = []
    assert cache.violations() == []
    assert fake.computed == 3


@pytest.mark.parametrize("name", ["GLASSES_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS"])
def test_every_gate_variable_is_part_of_the_key(monkeypatch, name):
    fake = Counting()
    cache = fake.cache()
    cache.violations()
    monkeypatch.setenv(name, "x")
    cache.violations()
    assert fake.computed == 2


def test_a_returned_list_cannot_change_the_cached_verdict():
    fake = Counting()
    fake.problems = ["process environment: bad"]
    cache = fake.cache()
    cache.violations().clear()
    assert cache.violations() == ["process environment: bad"]
    assert fake.computed == 1


def test_an_unexpected_error_propagates_and_leaves_no_verdict():
    fake = Counting()
    cache = fake.cache()
    cache.violations()
    fake.error = ZeroDivisionError(SENTINEL)
    fake.now = 1.0
    for _ in range(2):
        with pytest.raises(ZeroDivisionError):
            cache.violations()
    fake.error, fake.problems = None, ["profile dev: bad"]
    assert cache.violations() == ["profile dev: bad"]  # same clock reading: not served from the old verdict


# -- the clock ----------------------------------------------------------------------------------------------------


def test_the_window_is_measured_with_the_monotonic_clock_not_the_wall_clock(monkeypatch):
    now = {"monotonic": 100.0}

    def wall_clock() -> float:
        raise AssertionError("the verdict cache must not read the wall clock")

    # The module's ``time`` is swapped, so the default clock is whatever the cache takes from it; a wall-clock
    # reading (time.time, a jump of the system time) would raise.
    monkeypatch.setattr(gate, "time", SimpleNamespace(monotonic=lambda: now["monotonic"], time=wall_clock))
    computed = []
    cache = gate.VerdictCache(compute=lambda: computed.append(1) or [])
    cache.violations()
    now["monotonic"] = 100.9
    cache.violations()
    assert len(computed) == 1
    now["monotonic"] = 101.0
    cache.violations()
    assert len(computed) == 2


def test_a_clock_reading_before_the_verdict_counts_as_expired():
    fake = Counting()
    fake.now = 50.0
    cache = fake.cache()
    cache.violations()
    fake.now = 10.0  # a clock that went back: never trust a verdict from the future
    cache.violations()
    assert fake.computed == 2
