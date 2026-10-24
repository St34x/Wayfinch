# SPDX-License-Identifier: Apache-2.0
"""The allow-all gate against real Hermes: which profiles it reads (served ones only) and how fast a change shows.

Each test builds a scratch Hermes home with a served, a parked (``gateway.parked`` marker) and a standalone
(``gateway.standalone: true``) profile and runs the real Hermes venv in a subprocess, with the real
``profiles_to_serve`` and ``build_profile_secret_scope``. The `.env` values are a sentinel that must not appear
in any output.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from conftest import PLUGIN_ROOT
from test_config_check_edges import _scratch
from test_config_check_live import hermes_python

SENTINEL = "sentinel-value-4417"
OPEN = f"GLASSES_ALLOW_ALL_USERS={SENTINEL}\n"

RUNNER = f"""\
import asyncio, json, os, sys, time
from pathlib import Path
sys.path.insert(0, {str(PLUGIN_ROOT)!r})
import gateway.platforms._shared  # noqa: F401 - the marker that tells the gate Hermes is loaded
from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from glasses import config as gcfg
from glasses import gate
from glasses.adapter import GlassesAdapter

platform_registry.register(PlatformEntry(name="glasses", label="Glasses", adapter_factory=lambda c: None,
    check_fn=lambda: True, validate_config=gcfg.validate_config, required_env=[], install_hint="", source="plugin"))

home = Path(os.environ["HERMES_HOME"])
served = home / "profiles" / "served"
out = {{}}
adapter = GlassesAdapter(PlatformConfig(enabled=True, extra={{"public_url": "wss://glasses.example.com/ws",
                                                            "lanes": ["assistant", "work"]}}))

def until(wanted, limit=3.0):
    start = time.monotonic()
    while time.monotonic() - start < limit:
        if asyncio.run(adapter.gate_ok_async()) is wanted:
            return round(time.monotonic() - start, 3)
        time.sleep(0.02)
    return None

def settle(wanted):
    # The verdict was just recomputed: the next change has to wait for the ttl (the worst case).
    assert until(wanted) is not None

mode = sys.argv[1]
if mode == "scope":
    out["labels"] = [label for label, _ in gate.sources()]
    out["problems"] = gate.violations()
    out["ok"] = adapter.gate_ok()
elif mode == "latency":
    settle(True)
    (served / ".env").write_text({OPEN!r})
    out["refused_after"] = until(False)
    settle(False)
    (served / ".env").write_text("OTHER=1\\n")
    out["cleared_after"] = until(True)
    settle(True)
    (served / ".env").write_text({OPEN!r})
    assert until(False) is not None
    settle(False)
    (served / "gateway.parked").touch()
    out["parked_after"] = until(True)
    settle(True)
    (served / "gateway.parked").unlink()
    out["unparked_after"] = until(False)
    settle(False)
    (served / "config.yaml").write_text("gateway:\\n  standalone: true\\n")
    out["standalone_after"] = until(True)
    out["problems"] = gate.violations()
elif mode == "managed":
    settle(True)
    (Path(os.environ["HERMES_MANAGED_DIR"]) / ".env").write_text({OPEN!r})
    out["uncached"] = gate.violations()
    out["refused_after"] = until(False)
    for _ in range(3):  # and at every later expiry
        time.sleep(1.1)
        out.setdefault("later", []).append(adapter.gate_ok())
print(json.dumps(out))
"""


def _profile(home: Path, name: str, *, env: str | None = None, **gateway) -> Path:
    path = home / "profiles" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(yaml.safe_dump({"gateway": gateway} if gateway else {"agent": {}}))
    if env is not None:
        (path / ".env").write_text(env)
    return path


def _run(mode: str, home: Path, user: Path, env_extra: dict[str, str] | None = None) -> dict:
    script = home.parent / "runner.py"
    script.write_text(RUNNER, encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(user), "HERMES_HOME": str(home), **(env_extra or {})}
    proc = subprocess.run(
        [str(hermes_python()), str(script), mode],
        cwd=home.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert SENTINEL not in proc.stdout + proc.stderr, "a .env value reached the output"
    return json.loads(proc.stdout.splitlines()[-1])


def _home(tmp: str, **served) -> tuple[Path, Path]:
    home, user = Path(tmp) / "home", Path(tmp) / "user"
    home.mkdir()
    user.mkdir()
    _profile(home, "served", **served)
    return home, user


def test_real_hermes_a_parked_or_standalone_profile_with_allow_all_is_not_read():
    with _scratch() as tmp:
        home, user = _home(tmp)
        parked = _profile(home, "parked", env=OPEN)
        (parked / "gateway.parked").touch()
        _profile(home, "standalone", env=OPEN, standalone=True)
        result = _run("scope", home, user)
    assert result["labels"] == ["process environment", "active scope", "profile default", "profile served"], result
    assert result["problems"] == [] and result["ok"] is True, result


def test_real_hermes_a_served_profile_with_allow_all_refuses_and_names_no_value():
    with _scratch() as tmp:
        home, user = _home(tmp)
        (home / "profiles" / "served" / ".env").write_text(OPEN)
        parked = _profile(home, "parked", env=OPEN)
        (parked / "gateway.parked").touch()
        result = _run("scope", home, user)
    (problem,) = result["problems"]
    assert problem.startswith("profile served: GLASSES_ALLOW_ALL_USERS is set") and result["ok"] is False
    assert problem.endswith("or remove the switch from that profile")


def test_real_hermes_every_change_of_the_served_set_is_seen_within_a_second():
    with _scratch() as tmp:
        home, user = _home(tmp)
        result = _run("latency", home, user)
    for key in ("refused_after", "cleared_after", "parked_after", "unparked_after", "standalone_after"):
        assert result[key] is not None and result[key] <= 1.5, (key, result)
    assert result["problems"] == []


def test_real_hermes_a_switch_in_the_managed_env_is_seen_within_a_second():
    with _scratch() as tmp:
        home, user = _home(tmp)
        managed = Path(tmp) / "managed"
        managed.mkdir()
        result = _run("managed", home, user, {"HERMES_MANAGED_DIR": str(managed)})
    assert result["uncached"], result  # Hermes itself reads the managed file at once
    assert result["refused_after"] is not None and result["refused_after"] <= 1.5, result
    assert result["later"] == [False, False, False], result  # no clean verdict is renewed for ever
