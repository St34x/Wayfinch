<!-- SPDX-License-Identifier: Apache-2.0 -->
# Agent on the glasses has no tools

## Symptom
On the glasses the agent cannot create tasks, ask clarifying questions or call phone skills; the model request shows
zero tools.

## Context
A Hermes profile serves a glasses lane through the `glasses` plugin platform.

## Cause
A plugin platform's default toolset is `hermes-<platform>`, which does not exist for `glasses`, so the platform gets
no tools unless the profile lists them.

## Fix
In **each** profile that serves a glasses lane, set `platform_toolsets.glasses: [...]` in that profile's own
`config.yaml` (the routed profile's list applies), e.g. the same toolsets the profile has on Telegram.

## Verified
Verified on an isolated test gateway, Hermes `a4648c5f58`, 2026-10-04: without the key `n_tools: 0`; with it the
configured tools appear (picked up without a restart).

## Related
`hermes-mcp-tools-deferred-tool-search.md`
