<!-- SPDX-License-Identifier: Apache-2.0 -->
# Third-party notices

**The source tree bundles no third-party code yet.** Every file in this repository is Wayfinch's own work under the
Apache License 2.0 ([`LICENSE`](LICENSE), [`NOTICE`](NOTICE)), except the one third-party text listed below.

## Contributor Covenant (`CODE_OF_CONDUCT.md`)

- Component: Contributor Covenant Code of Conduct, version 2.1, created by Coraline Ada Ehmke.
- Source: <https://www.contributor-covenant.org/version/2/1/code_of_conduct/> (text as published there). The page
  as published in 2022 states that the Contributor Covenant is released under the CC BY 4.0 licence (archived copy:
  <https://web.archive.org/web/20221214154300/https://www.contributor-covenant.org/version/2/1/code_of_conduct/>).
- Licence: Creative Commons Attribution 4.0 International (`CC-BY-4.0`),
  <https://creativecommons.org/licenses/by/4.0/>. It is not under the project's Apache-2.0 licence.
- Changes: the `[INSERT CONTACT METHOD]` placeholder is replaced by this project's reporting channel, and a
  comment with the SPDX id, the licence link and this change note is added at the top. The rest of the text,
  including its Attribution section, is unchanged.

## Dependencies

Dependencies are not bundled. They are installed separately and keep their own licences:

- **Runtime** (the Hermes plugin): only libraries of the Hermes venv; the plugin installs nothing. A base Hermes
  install provides `cryptography`; `aiohttp` comes with Hermes' messaging extras, `jsonschema` with its `mcp` extra
  and `PyYAML` (operator CLI only) only through extras that pull it in, so a Hermes installed without them lacks
  those libraries.
- **Development** (tests and quality gates): pinned with SHA-256 hashes in `requirements-dev.txt`; the pinned
  gitleaks binary is downloaded and checksum-verified by `tools/secret-scan.sh`.

Each direct dependency, with its version and SPDX licence identifier, is listed in
[`docs/dependencies.md`](docs/dependencies.md).

## How notices will be recorded

- **Vendored or ported source** (planned: the open-source G2 driver layer under `android/third_party/<name>/`): the
  upstream licence and notice files stay unchanged next to the code; an `UPSTREAM.md` there records the upstream URL,
  the pinned commit, the licence and our changes; ported files keep the upstream copyright lines and an SPDX
  expression naming both licences (for example `Apache-2.0 AND MIT`); and this file gets a section with the
  component, version, licence, copyright notice and source URL.
- **Binary releases** (the Android app): this file will be generated from the build's licence inventory, checked
  against a licence allowlist, shipped with each release and shown in the app.
- A change that adds third-party code updates this file in the same commit.
