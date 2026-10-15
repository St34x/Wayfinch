<!-- SPDX-License-Identifier: Apache-2.0 -->
# Integration test instance (pinned Hermes Agent)

`make test` needs no Hermes install: the plugin tests use fakes (the live ones are skipped without the venv below),
and `make test-hermes-harness` tests the harness scripts with a fake `hermes`. This page is for the two things that do
need a real Hermes Agent:

- `make test-hermes-up` / `make test-hermes-down`: a real Hermes gateway with the minimal test platform
  `tools/test-hermes/glasses_min` (a WebSocket server on loopback) and the scripted mock LLM `tools/mock-llm/`;
- the live tests in `hermes-plugin/tests/test_config_check_live.py`, `test_config_check_edges.py`,
  `test_config_check_live_readonly.py`, `test_config_check_live_home_exact.py`, `test_config_check_live_names_exact.py`,
  `test_config_check_allow_all.py`, `test_config_check_live_allow_all_launch_env.py`, `test_config_check_env_secrets.py`,
  `test_config_check_live_secret_edges.py`, `test_config_check_live_timeouts_and_names_not_echoed.py`,
  `test_config_check_names_and_masks.py`, `test_config_check_merge.py` and `test_config_check_env_overlay.py`
  (`config check --live` against real Hermes code; `live_readonly` and `live_home_exact` also prove the scratch home
  is unchanged by a run, `allow_all` that `allow_all_users` of a config fails the check, `env_secrets`,
  `live_secret_edges`, `live_timeouts_and_names_not_echoed` and `names_and_masks` what a `.env` value may do to the
  output, `live_names_exact` that names and arguments are never echoed, `merge` that the glasses platform is merged
  as the gateway merges it, `env_overlay` that a `.env` makes the checker run no code). Without the venv below they
  are skipped, and the fakes in the same file still run.

`make test-hermes-up` / `make test-hermes-down` are not part of `make test`: they need the pinned venv and run a real
gateway. The live tests are part of `make test`: it runs the whole plugin suite (`test-plugin`), so they run when `.local/venv314`
exists in the checkout (or in the main checkout of a worktree) and are skipped otherwise. Both stay away from any
Hermes install you use: every `hermes` call runs with a clean environment (`env -i`), `HOME` and `HERMES_HOME` point
into the scratch directory, and all ports are on `127.0.0.1`.

What `config check --live` prints of a `.env` or of the process environment: a `.env` value of four or more
characters is replaced wherever it appears in the output (shorter values would blank ordinary words). A routed
profile name that is not a served profile and equals a value of a `.env` or of the process environment is not shown,
and a timeout that came out of a `${VAR}` is described, not printed. What remains: a `.env` value of one to three
characters, or a process-environment value, that sits inside a longer plain-identifier profile name (for example
`pre-${CANARY}-suffix`) can be printed. Keep secrets out of the profile names of `gateway.profile_routes`.

Two limits are known. A `.env` value that is padded with white space inside quotes (for example `NAME=" value "`) is
not matched: Hermes strips and lower-cases a route profile name, while the mask and the hiding compare the raw value,
so such a value used as a whole route profile name is printed in its stripped, lower-cased form. And whether a route
name is printed or hidden tells a reader of the output whether that name equals some `.env` or process-environment
value (one yes or no per run), so the output of `config check --live` is for the operator and must not be shown to
people who can edit `config.yaml` but must not learn secrets.

## One-time setup

Requirements: git, network access, and **Python 3.14** for the Hermes venv (every core dependency of the pinned Hermes
is marked `python_version >= '3.14'`; on an older Python the install succeeds but nothing runs, see
[troubleshooting](troubleshooting/test-hermes-venv-no-module-ruamel.md)). From the repository root:

```sh
mkdir -p .local && cd .local
git init -q hermes-agent
git -C hermes-agent fetch --depth 1 --no-tags https://github.com/NousResearch/hermes-agent.git a4648c5f58bad6304a0034dc3405fada207ac6a4
git -C hermes-agent checkout -q FETCH_HEAD
python3.14 -m venv venv314
venv314/bin/python -m pip install -e "./hermes-agent[messaging,mcp]"
venv314/bin/python -m pip install PyYAML==6.0.3
cd ..
```

- The commit is the Hermes version this repository is tested against. `setup_home.sh` refuses a clone at another
  commit (`HERMES_REF`, default `a4648c5f58`). Moving to a newer Hermes is its own change.
- `[messaging]` provides `aiohttp` (the test platform's WebSocket server); `[mcp]` provides `jsonschema` (plugin config
  validation). `PyYAML` is needed by `config check --live` and comes with neither extra
  ([troubleshooting](troubleshooting/test-hermes-venv-pyyaml-missing.md)).
- The venv is not hash-pinned: the pinned Hermes pins almost all of its direct dependencies to exact versions, but
  pip resolves their transitive dependencies from PyPI. It is a test fixture, not the dev venv (`make venv`).

## Run

```sh
make test-hermes-up      # test home + mock LLM (background) + gateway (foreground); Ctrl-C stops the gateway
make test-hermes-down    # from a second shell: stops the gateway and the mock LLM via their pid files
mkdir -p .local/pytest-tmp && .local/venv-dev/bin/python -m pytest --basetemp=.local/pytest-tmp/manual \
    hermes-plugin/tests/test_config_check_live.py -q -rs
```

`test-hermes-up` builds the test home `.local/hh` (a default profile and the test profiles `alpha` and `beta`, each
answering through the mock LLM), starts the mock LLM on port 18080 and runs the gateway with the test platform on
port 18700. When `test-hermes-down` stops the gateway, `make test-hermes-up` ends with `Error 1`: the gateway exits
with code 1 on a signal, which is expected. The live tests find `.local/venv314` on their own.

## Settings

Environment or `make` command-line variables, read by `mk/test-hermes.mk` and the scripts in `tools/test-hermes/`:

| Variable | Default | Meaning |
|---|---|---|
| `TEST_HERMES_LOCAL` | `<repo>/.local` | Scratch root: the clone `hermes-agent/`, the test home `hh/`, `fakehome/`, pid and log files |
| `TEST_HERMES_VENV` | `$TEST_HERMES_LOCAL/venv314` | Venv holding the pinned Hermes (also used by the live tests) |
| `HERMES_REF` | `a4648c5f58` | Commit the clone in `$TEST_HERMES_LOCAL/hermes-agent` must be at |
| `TEST_LLM_PORT` | `18080` | Loopback port of the mock LLM |
| `TEST_GATEWAY_PORT` | `18700` | Loopback port of the test platform |
| `TEST_PROFILES` | `alpha beta` | Test profiles; the first is the default lane |

The mock LLM runs as `/usr/bin/python3` with a clean environment. It needs only the standard library (any Python
3.11 or newer), but the path is fixed in `mk/test-hermes.mk`; on a system without `/usr/bin/python3`, start it by hand
or adjust that line locally.

Example with a scratch root outside the repository and other ports:
`make test-hermes-up TEST_HERMES_LOCAL=/tmp/th TEST_HERMES_VENV=/tmp/th/venv TEST_LLM_PORT=18091 TEST_GATEWAY_PORT=18791`
(the clone must then be in `/tmp/th/hermes-agent`). Stop it with the same root and mock LLM port:
`make test-hermes-down TEST_HERMES_LOCAL=/tmp/th TEST_LLM_PORT=18091`. A plain `make test-hermes-down` looks for the
pid files in the default root, prints "nothing to stop" and leaves this gateway and mock LLM running.
