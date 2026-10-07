# SPDX-License-Identifier: Apache-2.0
"""Script language of the mock LLM (pure: no I/O, no clock) -- imported by mock_llm.py, stdlib only.

Directives (in the LAST user message of the conversation, or -- when that has none and it is
the first user turn -- in the system prompt):

    @@CALL <tool_name> {json arguments}@@

Directives run in order, one per model call: the k-th call after the last user message
emits directive k as a tool call (only when the request offers that tool; otherwise a
text reply names the missing tool). When all directives are used, the mock answers with
text. JSON arguments are parsed with raw_decode, so a directive nested inside a JSON
string (e.g. a kanban task body) is NOT executed by the outer agent -- it is executed by
whatever agent later receives that body as its own prompt.

Reply text: "MOCK[<marker>] <info>" where <marker> is the value of PROFILE-MARKER=<x>
found in the system prompt (we put it into each test profile's SOUL.md), so the reply
itself proves which profile answered.

Auxiliary calls (title generation, compression, ...) carry no `tools`; they always get a
short text reply and never a tool call.

`<<DELAY:n>>` in a last user message WITHOUT directives makes the mock sleep n seconds (max
120) before answering -- used to make a delegated child / Kanban worker finish later than
the parent's turn. `<<LONG>>` in the last user message makes the final text answer ~650 chars
(exercises the long-answer hand-off). `<<TOOLS>>` logs every offered tool name.
"""

import json
import re
from collections.abc import Iterator
from typing import Any, NamedTuple

_DIRECTIVE = "@@CALL "
_MARKER_RE = re.compile(r"PROFILE-MARKER=([A-Za-z0-9_-]+)")
_NAME_RE = re.compile(r"([A-Za-z0-9_\-\.]+)\s*")
_WORKER_RE = re.compile(r"work kanban task (t_[0-9a-zA-Z]+)")
_DELAY_RE = re.compile(r"<<DELAY:(\d+)>>")
_MAX_DELAY = 120
_LONG_TEXT = " ".join(
    f"Line {i} of a deliberately long mock answer that does not fit on the glasses." for i in range(1, 9)
)

Message = dict[str, Any]
Directive = tuple[str, dict[str, Any]]
Call = tuple[Any, str]  # (tool name as sent, canonical JSON arguments)


class Analysis(NamedTuple):
    """What a conversation asks of the next model call."""

    directives: list[Directive]
    used: int  # directives already emitted since the last user message
    last_user: str
    marker: str
    last_tool: str  # last tool result after the last user message ("" if none)


class Plan(NamedTuple):
    """The answer to one model call: a tool call, or a text reply."""

    tool_call: Directive | None
    reply: str


def text_of(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def parse_directives(text: str) -> list[Directive]:
    """Return [(name, args_dict)] for top-level @@CALL directives in text."""
    out: list[Directive] = []
    pos, dec = 0, json.JSONDecoder()
    while (i := text.find(_DIRECTIVE, pos)) >= 0:
        j = i + len(_DIRECTIVE)
        m = _NAME_RE.match(text[j:])
        if not m:
            pos = j
            continue
        k = j + m.end()
        try:
            args, end = dec.raw_decode(text, k)
        except ValueError:
            args, end = {}, k
        out.append((m.group(1), args if isinstance(args, dict) else {}))
        pos = end
    return out


def _strings(obj: Any) -> Iterator[str]:
    """All string leaves of a decoded JSON value (tool results are JSON-encoded)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _call(name: Any, args: Any) -> Call:
    return (name, json.dumps(args, sort_keys=True))


def _executed(messages: list[Message]) -> set[Call]:
    """(name, canonical-args) of every tool call the assistant already made in this conversation."""
    done: set[Call] = set()
    for m in messages:
        for tc in (m.get("tool_calls") or []) if m.get("role") == "assistant" else []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            done.add(_call(fn.get("name"), args))
    return done


def _texts(messages: list[Message], role: str) -> list[str]:
    return [text_of(m.get("content")) for m in messages if m.get("role") == role]


def _tool_result_directives(messages: list[Message]) -> list[Directive]:
    """Directives in the string leaves of every tool result (in its raw text if it is not JSON)."""
    found: list[Directive] = []
    for raw in _texts(messages, "tool"):
        try:
            leaves = list(_strings(json.loads(raw)))
        except ValueError:
            leaves = [raw]
        for leaf in leaves:
            found.extend(parse_directives(leaf))
    return found


def _carried_directives(messages: list[Message]) -> list[Directive]:
    """Sources 3 and 4 of `analyse`: tool-result directives not run yet, else the worker bootstrap."""
    done = _executed(messages)
    pending = [d for d in _tool_result_directives(messages) if _call(*d) not in done]
    if pending:
        return pending
    first_user = next((text_of(m.get("content")) for m in messages if m.get("role") == "user"), "")
    wm = _WORKER_RE.search(first_user)
    if wm and _call("kanban_show", {"task_id": wm.group(1)}) not in done:
        return [("kanban_show", {"task_id": wm.group(1)})]
    return []


def analyse(messages: list[Message]) -> Analysis:
    """Directive sources, in order:

    1. the last user message (k-th call after it emits directive k);
    2. first user turn only: the system prompt;
    3. otherwise (worker / child agents whose prompt only points at the work): directives found in
       string leaves of earlier TOOL RESULTS anywhere in the conversation (e.g. a kanban task body
       returned by kanban_show); each runs once (dedup by name+args against calls already made);
    4. Kanban worker bootstrap: first user "work kanban task <id>" -> kanban_show(task_id) once.
    """
    system = " ".join(_texts(messages, "system"))
    marker_m = _MARKER_RE.search(system)
    marker = marker_m.group(1) if marker_m else "?"
    last_user_idx = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    last_user = text_of(messages[last_user_idx].get("content")) if last_user_idx >= 0 else ""
    after = messages[last_user_idx + 1 :] if last_user_idx >= 0 else messages
    last_tool = next((text_of(m.get("content")) for m in reversed(after) if m.get("role") == "tool"), "")
    directives = parse_directives(last_user)
    n_user = sum(1 for m in messages if m.get("role") == "user")
    if not directives and n_user <= 1:
        directives = parse_directives(system)
    if directives:
        used = sum(1 for m in after if m.get("role") == "assistant" and m.get("tool_calls"))
        return Analysis(directives, used, last_user, marker, last_tool)
    return Analysis(_carried_directives(messages), 0, last_user, marker, last_tool)


def _done_text(a: Analysis) -> str:
    reply = f"MOCK[{a.marker}] done"
    if a.last_tool:
        reply += "; last tool result: " + a.last_tool[:300].replace("\n", " ")
    else:
        reply += "; you said: " + a.last_user[:120].replace("\n", " ")
    if "<<LONG>>" in a.last_user:
        reply += " | LONG ANSWER: " + _LONG_TEXT
    return reply


def plan_reply(a: Analysis, tools: list[Any]) -> Plan:
    """The next directive as a tool call if it is offered, else a text reply; no tools = auxiliary call."""
    if not tools:
        return Plan(None, "Mock title")
    if a.used < len(a.directives):
        name, args = a.directives[a.used]
        if name in tools:
            return Plan((name, args), "")
        return Plan(None, f"MOCK[{a.marker}] tool {name} not offered (have {len(tools)} tools)")
    return Plan(None, _done_text(a))


def delay_seconds(a: Analysis, tools: list[Any]) -> int | None:
    """Seconds to sleep for `<<DELAY:n>>` (simulates a slow child / worker), None for no sleep."""
    delay_m = _DELAY_RE.search(a.last_user)
    if tools and not a.directives and delay_m and not a.last_tool:
        return min(int(delay_m.group(1)), _MAX_DELAY)
    return None
