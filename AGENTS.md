<!-- SPDX-License-Identifier: Apache-2.0 -->
# Rules for coding agents and contributors

This file is the short form for anyone, human or coding agent, who changes this repository. The full rule set is
[ENGINEERING.md](ENGINEERING.md); the pull-request steps are in [CONTRIBUTING.md](CONTRIBUTING.md); vulnerabilities
are reported as [SECURITY.md](SECURITY.md) says. Where this file and ENGINEERING.md differ, ENGINEERING.md wins.

## Repository map

| Path | What it is |
|---|---|
| `android/` | Android app (Gradle root, pure Kotlin modules and Android modules); `android/third_party/` holds the vendored G2 driver |
| `hermes-plugin/glasses/` | Hermes gateway platform plugin `glasses`; tests in `hermes-plugin/tests/` |
| `protocol/` | envelope JSON Schema (the only definition of the wire format), specification, fixtures |
| `tools/` | code generators (`tools/codegen/`), quality gates (`tools/quality/`), test helpers |
| `mk/` | make fragments; each component wires its own target into `make test` (`mk/README.md`) |
| `docs/` | architecture, references, troubleshooting; the index is [docs/README.md](docs/README.md) |
| `deploy/examples/` | example configuration with `example.com` values only |

## Run the tests and gates

- `make venv` once: builds `.local/venv-dev` from the hash-pinned `requirements-dev.txt`.
- `make test`: the secret scan plus every live gate and test suite (size, lint, types, import boundaries, codegen
  freshness, hygiene, docs, coverage). It must pass at every commit, not only at the branch head.
- `make test-android` and `make test-android-jvm`: the Kotlin gates and tests; they need a JDK and the Android SDK and
  are not part of `make test`.
- Single gates while you work: `make check-size`, `make lint-py`, `make typecheck-py`, `make check-codegen`,
  `make check-hygiene`, `make check-docs`. `make help` lists the top-level targets.
- Generated files are never edited by hand: after a schema edit run `sh tools/codegen/regenerate.sh
  .local/venv-dev/bin/python` and commit the result with the source.

## What blocks a change, and what does not

The goal is a working, stable product first: ship the smallest thing that works, with security built in from the start
and polished along the way. A finding **blocks** a merge only when it is one of these:

- it lets an outsider or a compromised component gain access, or read or send data it must not;
- it destroys user data, or leaks a secret in the shipped product or in the repository (a guard that fails open on a
  path an attacker can reach counts);
- the change does not work or is unstable (a functional bug), or a gate in `make test` is red.

Everything else (naming, wording, missing edge-case tests, refactors, tooling ideas, theoretical bypasses) is an
**improvement**: write it down as a follow-up issue; it never holds a pull request. A dependency-direction (boundary)
violation counts as a blocker, a size or complexity overrun as a **should-fix** (ENGINEERING.md, preamble).

## Security rules

- No secrets in code, tests, fixtures, logs, commit messages or command-line arguments (a process list shows argv).
  Read them from a secret store or a file with tight permissions; logs carry ids and sizes, never tokens or text.
- No host-specific values: use `example.com`, `192.0.2.0/24` and `2001:db8::/32` in examples and tests.
- Validate every input at the boundary where it enters (frames, config, files, CLI); fail closed on anything unknown,
  expired or unreadable.
- Dependencies: standard library first; every new direct dependency is pinned by version and hash and justified in
  [docs/dependencies.md](docs/dependencies.md).
- Never weaken a gate to get green: no raised limit, new baseline or ratchet entry for new code, loosened rule, skipped
  test or suppression without a reason on the same line. Fix the code instead.

## Docs match the code

Documentation states what the code does today. Anything designed but not built is marked **planned** in the same
sentence; a gate is called live only when `make test` (or the named target) runs it. A change to a command, target,
config key, default, limit, path or visible behaviour updates every document that states it in the same commit.

## Naming and references

- No other company's or product's name in identifiers, paths, package, module or class names or config keys. Allowed:
  the device name `G2`, prose that states compatibility ("works with Even Realities G2 glasses"), licence and
  copyright text, `NOTICE`, `THIRD_PARTY_NOTICES.md` and the `UPSTREAM.md` of vendored code.
- Requirement and acceptance ids, decision-record numbers and review-report names stay out of code, tests and docs;
  name tests and files by the behaviour they check. Task ids such as `T1.23` are allowed (CONTRIBUTING.md, Task ids).
- A `TODO` or `FIXME` carries a task id: `TODO(T1.23): ...`.

## One task per change

One change = one behaviour, about 20 minutes of work, at most about 300 added lines of non-test, non-generated code
(deleted lines do not offset added ones). Write a failing test first. Every commit passes `make test`.

## Commits and sign-off

- Subject: a task id (`T1.06: reject lone surrogates in the codec`) or a lower-case prefix `fix:`, `feat:`, `docs:`,
  `test:`, `refactor:` or `chore:`, then the text. `python3 tools/quality/check_hygiene.py --commits <range>` checks a
  range; the full prefix list is `SUBJECT_PREFIXES` in that script.
- Body: a few plain lines on what changed and why.
- Sign off every commit with `git commit -s`, which adds the Developer Certificate of Origin trailer as the last line
  of the body: `Signed-off-by: Jane Doe <jane@example.com>`. The author and the sign-off use the same name and
  address; maintainers use their GitHub noreply address for both. No gate checks the trailer yet (planned).
