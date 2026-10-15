<!-- SPDX-License-Identifier: Apache-2.0 -->
# Documentation index

Every public document of this repository is linked from here or from a document linked here; `make check-docs`
fails when one is not (or when a link or `#anchor` does not resolve). Start with the [README](../README.md).

## Project

- [README](../README.md) — what the project is, its status today, quick start.
- [Architecture](architecture.md) — target architecture: what runs where, data flows, security.
- [Glossary](glossary.md) — one name per term.
- [Security policy](../SECURITY.md) — reporting a vulnerability, supported versions, scope.
- [Third-party notices](../THIRD_PARTY_NOTICES.md) and [code of conduct](../CODE_OF_CONDUCT.md).

## Deployers and operators

- [Configuration reference](reference/config.md) — every plugin config key (generated from the schema).
- [Admin CLI](admin-cli.md) — the `glasses-admin` commands that exist today, their options and exit codes.
- [Dependencies](dependencies.md) — every direct dependency, why it is here and the alternative rejected.
- [Troubleshooting](troubleshooting/INDEX.md) — solved problems, one file each, searchable by error text.

## Contributors

- [Contributing](../CONTRIBUTING.md) — standards, checks before a pull request, task ids, commit messages, sign-off.
- [Engineering standards](../ENGINEERING.md) — limits, boundaries, typing, tests, docs rules and the gate table.
- [Integration test instance](test-hermes.md) — the pinned Hermes Agent used for integration tests.
- [Make-target reference](reference/make-targets.md) — every make target and what it does (generated from the `#:`
  lines of the `Makefile` and `mk/*.mk`).

## Module READMEs

Each module states its purpose, public API and invariants:

- [Hermes plugin](../hermes-plugin/glasses/README.md) — `hermes-plugin/glasses/`
- [Protocol](../protocol/README.md) — `protocol/`, the wire format (envelope schema, limits, delivery rules)
- [Make fragments](../mk/README.md) — `mk/`, how `make test` is wired
- [Developer tools](../tools/README.md) — `tools/`
- [Code generators](../tools/codegen/README.md) — `tools/codegen/`
- [Mock LLM](../tools/mock-llm/README.md) — `tools/mock-llm/`
- [Quality gates](../tools/quality/README.md) — `tools/quality/`
