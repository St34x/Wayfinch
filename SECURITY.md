<!-- SPDX-License-Identifier: Apache-2.0 -->
# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately** through GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public issue, pull request or discussion for a
security problem.

A useful report says:

- which component and which commit you looked at;
- what an attacker needs (network position, an enrolled device, a local account on the server, ...);
- the steps to reproduce, and what happens compared with what should happen;
- the impact as you see it.

Please do not include real tokens, certificates, keys or personal data in the report. Redact them, or use throwaway
values.

This is a volunteer project with no bug bounty. Reports are handled on a best-effort basis. We will confirm receipt,
keep you informed while we work on a fix, and credit you in the advisory unless you ask us not to. Please give us a
reasonable time to release a fix before you disclose publicly.

## Supported versions

| Version | Supported |
|---|---|
| any | None yet. Wayfinch is **pre-alpha**, with no release and no deployable end-to-end system. Reports against `main` are welcome. |

Once releases exist, security fixes go into the latest release only.

## Scope

In scope: everything in this repository, in particular

- the Hermes gateway plugin `glasses` (`hermes-plugin/`): authentication, device registry, envelope handling, the
  outbox, approval and clarify cards, the phone bridge, and the admin CLI;
- the protocol and its schema (`protocol/`), e.g. limits that a valid frame can exceed, or rules that let a card's
  text misrepresent an action;
- the Android app (`android/`, once it exists): credential storage, enrolment, confirmations, exported components, and
  network trust;
- the documented deployment: reverse-proxy examples, enrolment and revocation procedures, and example configuration;
- the build and supply chain: dependency pinning, the secret scan, code generation, and release signing.

Out of scope (please report these upstream):

- vulnerabilities in Hermes Agent itself, the glasses firmware, vendor apps, or third-party STT / LLM / chat
  providers;
- findings that require an attacker who already controls the server account running the agent, or an unlocked,
  compromised phone, unless Wayfinch makes the impact worse than that access already allows;
- missing hardening on a deployment that ignores the documented requirements (for example a proxy that does not
  verify client certificates).

The intended security properties are described in [docs/architecture.md](docs/architecture.md#4-security-model). A way
to break any of them is in scope.
