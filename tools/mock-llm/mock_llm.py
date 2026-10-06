#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Scripted mock LLM for the isolated test Hermes (stdlib only, loopback only).

OpenAI-compatible: GET /v1/models, POST /v1/chat/completions (stream and non-stream).
Started as a throwaway spike mock and extended with scripted tool calls.

Script language (in the LAST user message of the conversation, or -- when that has none
and it is the first user turn -- in the system prompt):

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
the parent's turn. `<<LONG>>` in the last user message makes the final text answer ~700 chars
(exercises the long-answer hand-off).

Every request is logged to stderr as one JSON line.
Usage: python3 mock_llm.py [--port 18080] [--log-file PATH]
"""

import argparse
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mock-model"
_DIRECTIVE = "@@CALL "
_MARKER_RE = re.compile(r"PROFILE-MARKER=([A-Za-z0-9_-]+)")


LOG_FILE = None


def log(**kw):
    kw["ts"] = time.strftime("%H:%M:%S")
    kw["t"] = round(time.time(), 3)
    line = json.dumps(kw, ensure_ascii=False)
    print(line, file=sys.stderr, flush=True)
    if LOG_FILE:
        with open(LOG_FILE, "a") as fh:
            fh.write(line + "\n")


def text_of(content):
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def parse_directives(text):
    """Return [(name, args_dict)] for top-level @@CALL directives in text."""
    out, pos, dec = [], 0, json.JSONDecoder()
    while True:
        i = text.find(_DIRECTIVE, pos)
        if i < 0:
            return out
        j = i + len(_DIRECTIVE)
        m = re.match(r"([A-Za-z0-9_\-\.]+)\s*", text[j:])
        if not m:
            pos = j
            continue
        name = m.group(1)
        k = j + m.end()
        try:
            args, end = dec.raw_decode(text, k)
        except ValueError:
            args, end = {}, k
        out.append((name, args if isinstance(args, dict) else {}))
        pos = end


def _strings(obj):
    """All string leaves of a decoded JSON value (tool results are JSON-encoded)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _executed(messages):
    """(name, canonical-args) of every tool call the assistant already made in this conversation."""
    done = set()
    for m in messages:
        for tc in (m.get("tool_calls") or []) if m.get("role") == "assistant" else []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            done.add((fn.get("name"), json.dumps(args, sort_keys=True)))
    return done


_WORKER_RE = re.compile(r"work kanban task (t_[0-9a-zA-Z]+)")


def analyse(messages):
    """-> (directives, used, last_user, marker, last_tool_result).

    Directive sources, in order:
      1. the last user message (k-th call after it emits directive k);
      2. first user turn only: the system prompt;
      3. otherwise (worker / child agents whose prompt only points at the work): directives found in
         string leaves of earlier TOOL RESULTS anywhere in the conversation (e.g. a kanban task body
         returned by kanban_show); each runs once (dedup by name+args against calls already made);
      4. Kanban worker bootstrap: last user "work kanban task <id>" -> kanban_show(task_id) once.
    """
    system = " ".join(text_of(m.get("content")) for m in messages if m.get("role") == "system")
    marker_m = _MARKER_RE.search(system)
    marker = marker_m.group(1) if marker_m else "?"
    last_user_idx = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    last_user = text_of(messages[last_user_idx].get("content")) if last_user_idx >= 0 else ""
    after = messages[last_user_idx + 1 :] if last_user_idx >= 0 else messages
    last_tool = ""
    for m in reversed(after):
        if m.get("role") == "tool":
            last_tool = text_of(m.get("content"))
            break
    directives = parse_directives(last_user)
    n_user = sum(1 for m in messages if m.get("role") == "user")
    if not directives and n_user <= 1:
        directives = parse_directives(system)
    if directives:
        used = sum(1 for m in after if m.get("role") == "assistant" and m.get("tool_calls"))
        return directives, used, last_user, marker, last_tool
    # 3. directives carried inside earlier tool results, each executed once
    done = _executed(messages)
    found = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        raw = text_of(m.get("content"))
        try:
            leaves = list(_strings(json.loads(raw)))
        except ValueError:
            leaves = [raw]
        for leaf in leaves:
            found.extend(parse_directives(leaf))
    pending = [d for d in found if (d[0], json.dumps(d[1], sort_keys=True)) not in done]
    if pending:
        return pending, 0, last_user, marker, last_tool
    # 4. kanban worker bootstrap
    first_user = next((text_of(m.get("content")) for m in messages if m.get("role") == "user"), "")
    wm = _WORKER_RE.search(first_user)
    if wm and ("kanban_show", json.dumps({"task_id": wm.group(1)}, sort_keys=True)) not in done:
        return [("kanban_show", {"task_id": wm.group(1)})], 0, last_user, marker, last_tool
    return [], 0, last_user, marker, last_tool


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        log(method="GET", path=self.path)
        if self.path.rstrip("/").endswith("/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": MODEL, "object": "model", "created": 0, "owned_by": "mock", "context_length": 131072}
                    ],
                },
            )
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._json(400, {"error": {"message": "bad json"}})
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        messages = req.get("messages") or []
        tools = [t.get("function", {}).get("name") for t in (req.get("tools") or [])]
        directives, used, last_user, marker, last_tool = analyse(messages)
        tool_call = None
        if tools and used < len(directives):
            name, args = directives[used]
            if name in tools:
                tool_call = (name, args)
                reply = ""
            else:
                reply = "MOCK[%s] tool %s not offered (have %d tools)" % (marker, name, len(tools))
        elif tools:
            reply = "MOCK[%s] done" % marker
            if last_tool:
                reply += "; last tool result: " + last_tool[:300].replace("\n", " ")
            else:
                reply += "; you said: " + last_user[:120].replace("\n", " ")
            if "<<LONG>>" in last_user:
                reply += " | LONG ANSWER: " + " ".join(
                    "Line %d of a deliberately long mock answer that does not fit on the glasses." % i
                    for i in range(1, 9)
                )
        else:
            reply = "Mock title"  # auxiliary call (title generation etc.)
        stream = bool(req.get("stream"))
        delay_m = re.search(r"<<DELAY:(\d+)>>", last_user)
        if tools and not directives and delay_m and not last_tool:
            time.sleep(min(int(delay_m.group(1)), 120))  # simulate a slow child / worker
        log(
            method="POST",
            model=req.get("model"),
            stream=stream,
            n_messages=len(messages),
            n_tools=len(tools),
            marker=marker,
            tools_of_interest=sorted(
                t
                for t in tools
                if t in ("delegate_task", "kanban_create", "kanban_complete", "terminal", "clarify", "send_message")
                or "phone" in (t or "")
            )
            if tools
            else [],
            last_user=last_user[:300],
            directives=len(directives),
            used=used,
            all_tools=sorted(t for t in tools if t) if "<<TOOLS>>" in last_user else None,
            emit=("tool:" + tool_call[0]) if tool_call else ("text:" + reply[:80]),
        )
        cid = "chatcmpl-mock-%d" % int(time.time() * 1000)
        created = int(time.time())
        usage = {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}
        call_id = "call_%d_%d" % (int(time.time() * 1000), used)
        if not stream:
            if tool_call:
                msg = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": tool_call[0], "arguments": json.dumps(tool_call[1])},
                        }
                    ],
                }
                fin = "tool_calls"
            else:
                msg, fin = {"role": "assistant", "content": reply}, "stop"
            self._json(
                200,
                {
                    "id": cid,
                    "object": "chat.completion",
                    "created": created,
                    "model": MODEL,
                    "choices": [{"index": 0, "finish_reason": fin, "message": msg}],
                    "usage": usage,
                },
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(delta, finish=None, with_usage=False):
            obj = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if with_usage:
                obj["usage"] = usage
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        if tool_call:
            chunk({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"index": 0, "id": call_id, "type": "function", "function": {"name": tool_call[0], "arguments": ""}}
                ],
            })
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(tool_call[1])}}]})
            chunk({}, finish="tool_calls", with_usage=True)
        else:
            chunk({"role": "assistant", "content": ""})
            chunk({"content": reply})
            chunk({}, finish="stop", with_usage=True)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--log-file", help="also append every JSON log line to this file")
    a = ap.parse_args()
    global LOG_FILE
    LOG_FILE = a.log_file
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    log(event="listening", host="127.0.0.1", port=a.port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
