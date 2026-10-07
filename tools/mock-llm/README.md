<!-- SPDX-License-Identifier: Apache-2.0 -->
# Mock LLM

## Purpose

A scripted, OpenAI-compatible LLM server for the isolated test Hermes (`tools/test-hermes/`), so end-to-end tests
of the glasses plugin run without a real model, network or API key.

## Public API

- `python3 tools/mock-llm/mock_llm.py [--port 18080] [--log-file PATH]` — serves `GET /v1/models` and
  `POST /v1/chat/completions` (streaming and non-streaming) on loopback; started by `make test-hermes-up`.
  `mock_llm.py` is the HTTP glue and wire format; `mock_script.py` (imported from the same directory) is the
  script language: `analyse` (directive sources), `plan_reply`, `delay_seconds`.
- Script directives in the last user message (or the system prompt on the first turn):
  `@@CALL <tool_name> {json arguments}@@` — one tool call per model call, in order;
  `<<DELAY:n>>` — sleep n seconds (max 120) before answering, only if the request offers tools, no `@@CALL` directive
  applies to the call (also not one from the system prompt) and no tool result followed it (ignored otherwise);
  `<<LONG>>` — a ~650-character answer; `<<TOOLS>>` — the log line lists every offered tool name.
- Without such a directive, `@@CALL` directives inside earlier tool results run once each (a worker reading its task
  body); a first user message `work kanban task <id>` makes one `kanban_show` call.
- Replies to requests with tools read `MOCK[<x>] <info>`; `<x>` is from `PROFILE-MARKER=<x>` in the system prompt
  (`?` if absent; proves which profile answered). Every request is logged to stderr as one JSON line.
- `make test-mock-llm` runs the unit tests in `tests/` (part of `make test`; one per directive, plus the HTTP glue).

## Invariants

- Stdlib only; binds loopback only. `mock_script.py` does no I/O.
- A tool call is emitted only when the request offers that tool; requests without tools (titles, compression)
  always get a short text reply.
- Directives inside JSON string arguments are not executed by the outer turn.
- The reply text depends only on the conversation (ids and timestamps aside), so tests can assert on it.
