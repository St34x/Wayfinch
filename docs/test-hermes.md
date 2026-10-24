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
  `test_config_check_live_output_independent_of_env.py`, `test_config_check_names_and_masks.py`,
  `test_config_check_merge.py` and `test_config_check_env_overlay.py`
  (`config check --live` against real Hermes code; `live_readonly` and `live_home_exact` also prove the scratch home
  is unchanged by a run, `allow_all` that `allow_all_users` of a config fails the check, `env_secrets`,
  `live_secret_edges`, `live_timeouts_and_names_not_echoed` and `names_and_masks` what a `.env` value may do to the
  output, `live_output_independent_of_env` that the output is byte-identical with and without an environment value,
  `live_names_exact` that names and arguments are never echoed, `merge` that the glasses platform is merged
  as the gateway merges it, `env_overlay` that a `.env` makes the checker run no code). Without the venv below they
  are skipped, and the fakes in the same file still run.
- `hermes-plugin/tests/test_gate_served_profiles_real.py` runs the allow-all gate and the adapter's `gate_ok_async()` in the
  real Hermes venv: parked and standalone profiles are not read, a change of a served `.env` or of the served set
  shows within a second, and a switch in the administrator-managed `.env` is refused within a second and at every later
  expiry (a clean verdict is never renewed). It is skipped without the venv below.
- `hermes-plugin/tests/test_gate_active_scope_real.py` installs a secret scope that holds an allow-all switch in a copied
  context of the real Hermes venv and checks that the adapter refuses that context inside the second in which a clean
  context was accepted (the verdict key holds the caller's active scope). It is skipped without the venv below.
- `hermes-plugin/tests/test_inbound_pinned_hermes.py` runs the real adapter's inbound path in the real Hermes venv: a
  `msg.in` becomes the pinned `MessageEvent(TEXT)` with the device id as `user_id` and the profile of
  `gateway.profile_routes` on its source, a lane routed to a profile that is not served gets `lane_unrouted`, and
  the gateway's own user check admits the source with `GLASSES_ALLOWED_USERS=<deviceId>` and refuses it with another
  id or with the list empty or unset. It also checks that the clone is at `HERMES_REF`. It is skipped without the venv
  below.
- `hermes-plugin/tests/test_inbound_gateway_control_pinned.py` sends `/approve always`, `/yolo`, `yes`, `always`,
  `session` and `approve` as `msg.in` text through the real adapter and the pinned base `handle_message` while the
  session is busy and a dangerous-command approval is pending: none is a gateway command or resolves the approval (the
  same event with `allow_gateway_control=True` does, as a control). It is skipped without the venv below.
- `hermes-plugin/tests/test_inbound_gateway_control_strings_pinned.py` sends more texts as `msg.in` (`restart gateway`,
  `/stop`, `/new`, `/update`, `/model --global ...`, odd spellings of `/approve`) through the real adapter and the
  pinned base `handle_message` on an idle session: the message handler gets the text unchanged and no command, while
  the same event with `allow_gateway_control=True` is rewritten or parsed as a command. It is skipped without the venv
  below.

`make test-hermes-up` / `make test-hermes-down` are not part of `make test`: they need the pinned venv and run a real
gateway. The live tests are part of `make test`: it runs the whole plugin suite (`test-plugin`), so they run when `.local/venv314`
exists in the checkout (or in the main checkout of a worktree) and are skipped otherwise. Both stay away from any
Hermes install you use: every `hermes` call runs with a clean environment (`env -i`), `HOME` and `HERMES_HOME` point
into the scratch directory, and all ports are on `127.0.0.1`.

What `config check --live` does with environment references: it does not evaluate them. A field of `config.yaml`
that holds `${VAR}` or `${env:VAR}` is detected from the raw text of the file, never from the value of the variable;
the check prints one fixed line for it (`<field>: uses an environment reference, not checked`, a warning) and skips
every check that depends on the field, so stdout, stderr and the exit status are the same for any value of the
variable. Otherwise a reader who can edit `config.yaml` could learn whether a secret equals a text of their choice
(a tool set compared with another, a route `chat_id` with a lane name). The fields, per routed profile:
`platform_toolsets` (with the other keys that decide a tool set: `toolsets`, `agent.disabled_toolsets`,
`context.engine`, `known_*_toolsets`, `mcp_servers.<name>.enabled`), `stt.provider`, `stt.language`
(`stt.language`, `stt.<provider>.language`, `language_code`), `agent.clarify_timeout` and `approvals.timeout`; for the
gateway: `gateway.profile_routes` and `gateway.multiplex_profiles`, whose reference leaves no lane and no profile
checked. A reference in `allow_all_users` is an error line, not a skip: a grant to every sender cannot be ruled out.
Nothing is masked, and a route to a profile this gateway does not serve is shown as one fixed placeholder (the line
still names the lane; the exit status is 1). A served profile is named, a tool name only when it is in Hermes' toolset
table (any other is counted), a gate line only in its known shape. Not references, so still read as Hermes reads them:
`HERMES_LOCAL_STT_LANGUAGE` and the allow-all variables of the environment and of the `.env` files, whose effect (a
language hint of `en` or not, a gate that passes or fails) is what the check reports. A reference inside the managed
overlay of Hermes (pinned by an administrator, not written in the `config.yaml` being checked) is still expanded.

## One-time setup

Requirements: git, network access, and **Python 3.14** for the Hermes venv (every core dependency of the pinned Hermes
is marked `python_version >= '3.14'`; on an older Python the install succeeds but nothing runs, see
[troubleshooting](troubleshooting/test-hermes-venv-no-module-ruamel.md)). From the repository root:

```sh
C=$PWD/tools/test-hermes/constraints-d923a5e.txt   # absolute: the commands below run in .local (or another root)
mkdir -p .local && cd .local
git init -q hermes-agent
git -C hermes-agent fetch --depth 1 --no-tags https://github.com/NousResearch/hermes-agent.git d923a5eb487a17c0a68801bba9ef0f56c81040a8
git -C hermes-agent checkout -q FETCH_HEAD
python3.14 -m venv venv314
venv314/bin/python -m pip install -c "$C" -e "./hermes-agent[messaging,mcp]"
venv314/bin/python -m pip install -c "$C" PyYAML==6.0.3
cd ..
```

- The commit is the Hermes version this repository is tested against. `setup_home.sh` refuses a clone at another
  commit (`HERMES_REF`, default `d923a5eb48`, Hermes 0.22.2). Moving to a newer Hermes is its own change (see
  [Moving the pin](#moving-the-pin)).
- `-c` gets an absolute path (`$C`), so the recipe works from any root: to build in a scratch root outside the repository
  (`TEST_HERMES_LOCAL`, see Settings) replace `.local` by that directory and leave `$C` as it is.
- `[messaging]` provides `aiohttp` (the test platform's WebSocket server); `[mcp]` provides `jsonschema` (plugin config
  validation). `PyYAML` is needed by `config check --live` and comes with neither extra
  ([troubleshooting](troubleshooting/test-hermes-venv-pyyaml-missing.md)).
- `tools/test-hermes/constraints-d923a5e.txt` fixes every package version of the venv (a `pip freeze` of the
  reference venv at this pin, without the editable Hermes), so the venv is the same on every host. Without `-c`, pip
  resolves the transitive dependencies on the install date (fastapi 0.143.3 on one host, 0.148.0 on another).
  Versions only, no hashes: it is a test fixture, not shipped, and not the dev venv (`make venv`, which is hash-pinned).
  No make target builds this venv; the recipe above is the only place that installs it.

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
| `HERMES_REF` | `d923a5eb48` | Commit the clone in `$TEST_HERMES_LOCAL/hermes-agent` must be at |
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

## Moving the pin

The pin is bumped by its own change (last: `a4648c5f58`, 0.21.4, to `d923a5eb48`, 0.22.2, T1.217). Steps:

1. Build the new clone and venv next to the old ones (`hermes-agent-<short>`, `venv314-<short>`: the recipe above with these
   directory names and the new commit), but without `-c` (the old constraints file may conflict with the new Hermes); read the
   new `pyproject.toml` first
   (`requires-python`, extras). Keep the old pair until the suite is green. Then regenerate the constraints file from the new venv:
   `venv314-<short>/bin/python -m pip freeze --exclude-editable --disable-pip-version-check`, saved as
   `tools/test-hermes/constraints-<short>.txt` (keep the header comments, `git rm` the old file) and point the recipe above at it.
2. Move the commit everywhere: `HERMES_REF` in `tools/test-hermes/setup_home.sh`, the fetch recipe and the table here, and every
   `pinned <hash>` comment (`git grep -n <old short hash>` outside `docs/troubleshooting/`). The "Verified on ... `<old>`"
   lines of the troubleshooting entries are dated facts and stay.
3. Re-check every `file:line` citation of the Hermes source in `hermes-plugin/` against the new tree (the lines move even when the
   code does not) and the field sets that mirror a Hermes table (`PLATFORM_ENTRY_FIELDS`, `HERMES_TOP_LEVEL`).
4. Run the live tests against the new pair first (`TEST_HERMES_LOCAL=<root>` with the clone at `<root>/hermes-agent` and
   `TEST_HERMES_VENV`, or a worktree whose `.local/hermes-agent` and `.local/venv314` are symlinks to the new pair):
   `hermes-plugin/tests/test_config_check_live.py`, `make test-hermes-harness`, then the full `make test`. Each drift
   found is a finding.
5. At merge time, do on every machine that runs the tests three things, in this order:
   1. Build the new pair next to the old one, with the recipe above and the committed constraints file, under its final
      names `.local/hermes-agent-<new>` and `.local/venv314-<new>` (the venv holds absolute paths of its own directory and of
      the editable clone, shebangs and `direct_url.json`, so it cannot be renamed afterwards).
   2. Swap with the script, not by hand: `sh tools/test-hermes/swap-pin.sh swap <new> <old>`, while holding every lock that
      serialises the test runs of the host (all slots of a runner that has several: a run in the other slot would read a
      half-swapped pair) and with no run in progress. The script works in one directory (`TEST_HERMES_LOCAL`, default
      `.local`) and changes `hermes-agent` and `venv314` into links to the new pair, each with an atomic rename. It checks
      first, and changes nothing when the new pair is missing or incomplete, when the two names are not both plain directories
      or both links of the same pin, or when a name is odd. The first time on a host both names are plain directories and
      `<old>` is the short name they are kept under (`hermes-agent-<old>`, `venv314-<old>`); later they are links, and the next
      swap is the same command without `<old>`. `sh tools/test-hermes/swap-pin.sh -n swap ...` only prints the plan. It prints what it did and the command to undo it.
   3. Undo, if needed, with `sh tools/test-hermes/swap-pin.sh rollback` under the same locks: the links go back to the pair
      the last swap replaced (a second rollback swaps again). Nothing may be run from a venv that the links do not point at:
      the editable install of the old venv names `.local/hermes-agent`, which after the swap is the new clone.

   A worktree whose `.local/hermes-agent` and `.local/venv314` are symlinks to the checkout's follows the swap. After the
   swap no host can run a commit from before the bump (the old venv is not usable and the old `setup_home.sh` refuses the new
   clone): every open branch merges `main` before its next full run.
