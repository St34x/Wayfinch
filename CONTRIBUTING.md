<!-- SPDX-License-Identifier: Apache-2.0 -->
# Contributing

Thanks for your interest. Wayfinch is pre-alpha, so the code moves quickly. Before you start on anything larger than a
small fix, please open an issue to discuss it.

Everyone taking part in the project follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Standards

[ENGINEERING.md](ENGINEERING.md) is the rule set: ports and adapters with a pure core, size and complexity limits,
typing, tests, error handling, dependencies and documentation. Reviews use it as the checklist. Please read it once.
The gates enforce most of it.

## Before you open a pull request

1. Build the test venv once with `make venv` (Python 3.14; see the [README](README.md#quick-start-for-contributors)).
2. **`make test` must pass.** It runs the secret scan and every gate and test suite. Every commit should pass it,
   not only the last one.
3. Write a failing test first, then the change. Keep one behaviour per change (roughly ≤300 changed lines of non-test,
   non-generated code).
4. Update the docs in the same change if behaviour that users, deployers or contributors can see changes. That covers
   the protocol, config keys, CLI, defaults, error messages and the security model. If you solved a problem others may
   hit, add an entry to [docs/troubleshooting/](docs/troubleshooting/INDEX.md).
5. Generated files are never edited by hand. After a schema change, regenerate and commit:
   `.local/venv-dev/bin/python tools/codegen/gen_protocol.py` (protocol schema) or
   `.local/venv-dev/bin/python tools/codegen/gen_config_docs.py` (plugin config schema).
6. A new direct dependency needs a row in [docs/dependencies.md](docs/dependencies.md) and hash pins.

Also:

- **SPDX header** in every new source file (`.py`, `.kt`, `.kts`, `.sh`, `.mk`, `Makefile`, `.gradle`), in its first
  ten lines: `SPDX-License-Identifier: Apache-2.0`.
- **No secrets and no host-specific values.** No hostnames, IPs, paths, account or profile names of a real
  deployment. Use `example.com`, `glasses.example.com` and the documentation address ranges (`192.0.2.0/24`,
  `2001:db8::/32`).
- **`TODO` / `FIXME`** only with a task id, e.g. `TODO(T1.23): ...`.
- **No references to private notes:** no decision-record numbers (`ADR-<n>`, in any letter case and also with `_`,
  `/`, `#` or a space before the number), paths into the maintainers' private team-notes folder or QA/review report
  names, in file contents or file names. Review-process tags fail too: QA wave, finding or round ids, review, QA or
  fix round numbers, wave numbers, `round <n>` fixes, findings or xfails, lists or decisions of the team lead, and
  file names ending in `_qa` or `-qa<n>` before the extension; name tests and comments by behaviour instead. Cite
  `ENGINEERING.md`, `docs/` or `protocol/README.md`, or explain the reason inline (checked by `make check-hygiene`;
  exact rule in ENGINEERING.md §9).
- **Python 3.11 compatible.** The dev venv is Python 3.14, but the plugin runs in the Hermes venv and supports the
  range Hermes declares (3.11 to 3.14), so all Python code must stay 3.11-compatible (checked by `make lint-py` and
  `make typecheck-py`). The Hermes commit pinned for the live tests itself needs Python 3.14; an installed Hermes may
  run on 3.11 or newer.

## Task ids

Comments, ratchet baselines (`mypy.ini`, `ruff.toml`, `tools/quality/size-baseline.txt`, `.coveragerc`) and commit
subjects carry task ids such as `T1.111`, `P.13` or `S02`. They refer to the maintainers' task tracker, which is not
published: `T<n>.<n>` (optionally with a letter, e.g. `T1.06a`) is an implementation task, `P.<n>` a publishing task
and `S<n>` a feasibility spike. A baseline entry tagged with an id stays until the task with that id lands and
removes it. Open work will move to GitHub issues later; until then, ask in an issue when an id matters for your
change.

## Commit messages

The subject line starts with a task id or a lower-case prefix, then `: ` and the text:

- a task id for work on a task (see [Task ids](#task-ids)): `T<n>.<n>` with an optional letter, `P.<n>` or `S<n>`
  with an optional letter, e.g. `T1.06: reject lone surrogates in the codec`;
- otherwise one of `fix:`, `feat:`, `docs:`, `test:`, `refactor:` or `chore:`.

`tools/quality/check_hygiene.py --commits <range>` checks a range of commits locally. It applies one allowlist
(`SUBJECT_PREFIXES` in that script) to every commit, whoever wrote it. Besides the forms above it accepts these
prefixes, which the project history uses for planning and process commits (most of them change the maintainers'
unpublished notes):

- `merge:` a merge commit that brings a task branch into `main`;
- `design:` and `adr:` design documents and decision records;
- `plan:`, `tasks:` and `status:` plans, task lists and status notes;
- `night:` logs of scheduled unattended work runs;
- `research:` and `spike:` research notes and throwaway feasibility experiments;
- `mockups:` UI mockups;
- `qa:` and `review:` test reports and code reviews.

A contribution normally needs only a task id or one of `fix:`, `feat:`, `docs:`, `test:`, `refactor:` and `chore:`.

## Licence and sign-off

Wayfinch is licensed under the [Apache License 2.0](LICENSE). Contributions are accepted under the same licence
(inbound = outbound, as in section 5 of the licence). There is no CLA.

Please sign off every commit (`git commit -s`). The sign-off certifies the
[Developer Certificate of Origin](https://developercertificate.org/): you wrote the change, or otherwise have the right
to submit it under the project's licence.

Code ported from other projects keeps its original licence notice and a header naming the source, the upstream commit
and what was changed. Only licences compatible with Apache-2.0 are accepted.
