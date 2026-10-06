<!-- SPDX-License-Identifier: Apache-2.0 -->
# Phone-bridge MCP tools not visible to the agent

## Symptom
The agent's tool list has no `mcp__phone__*` tools, only `tool_search` / `tool_describe` / `tool_call`; phone skills
work only through extra model calls.

## Context
The phone bridge is configured as an MCP server (`mcp_servers.phone`) in a Hermes profile.

## Cause
Hermes defers MCP tools behind its tool-search bridge by default (`tools.tool_search.enabled: auto`).

## Fix
In the profile's config set `tools.tool_search.enabled: "off"` (or `tools.tool_search.defer: []`), then start a new
session (`/new`).

## Verified
Verified on an isolated test gateway, Hermes `a4648c5f58`, 2026-10-04: deferred by default; eager after the change.

## Related
`hermes-plugin-platform-has-no-tools.md`
