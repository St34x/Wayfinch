#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Scripted mock LLM for the isolated test Hermes (stdlib only, loopback only).

OpenAI-compatible: GET /v1/models, POST /v1/chat/completions (stream and non-stream).
Started as a throwaway spike mock and extended with scripted tool calls; the script language
(`@@CALL`, `<<DELAY:n>>`, `<<LONG>>`, `<<TOOLS>>`, PROFILE-MARKER) is in mock_script.py.

This file is the HTTP glue plus the wire format: `log_fields`, `completion_body` and
`stream_events` are pure; `Handler` reads the request, sleeps for `<<DELAY:n>>`, logs and writes.
Every request is logged to stderr as one JSON line.
Usage: python3 mock_llm.py [--port 18080] [--log-file PATH]
"""

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, NamedTuple

from mock_script import Analysis, Message, Plan, analyse, delay_seconds, plan_reply

MODEL = "mock-model"
_INTEREST = ("delegate_task", "kanban_create", "kanban_complete", "terminal", "clarify", "send_message")
_USAGE = {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}

LOG_FILE: str | None = None


class ChatRequest(NamedTuple):
    model: Any
    stream: bool
    messages: list[Message]
    tools: list[Any]  # offered tool names (None for a tool without a name)


class Ids(NamedTuple):
    cid: str
    created: int
    call_id: str


def log(**kw: Any) -> None:
    kw["ts"] = time.strftime("%H:%M:%S")
    kw["t"] = round(time.time(), 3)
    line = json.dumps(kw, ensure_ascii=False)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    if LOG_FILE:
        with open(LOG_FILE, "a") as fh:
            fh.write(line + "\n")


def parse_request(req: dict[str, Any]) -> ChatRequest:
    tools = [t.get("function", {}).get("name") for t in (req.get("tools") or [])]
    return ChatRequest(req.get("model"), bool(req.get("stream")), req.get("messages") or [], tools)


def log_fields(chat: ChatRequest, a: Analysis, plan: Plan) -> dict[str, Any]:
    tools = chat.tools
    interest = sorted(t for t in tools if t in _INTEREST or "phone" in (t or "")) if tools else []
    return {
        "method": "POST",
        "model": chat.model,
        "stream": chat.stream,
        "n_messages": len(chat.messages),
        "n_tools": len(tools),
        "marker": a.marker,
        "tools_of_interest": interest,
        "last_user": a.last_user[:300],
        "directives": len(a.directives),
        "used": a.used,
        "all_tools": sorted(t for t in tools if t) if "<<TOOLS>>" in a.last_user else None,
        "emit": ("tool:" + plan.tool_call[0]) if plan.tool_call else ("text:" + plan.reply[:80]),
    }


def make_ids(now: float, used: int) -> Ids:
    ms = int(now * 1000)
    return Ids(f"chatcmpl-mock-{ms}", int(now), f"call_{ms}_{used}")


def _envelope(ids: Ids, kind: str, choice: dict[str, Any]) -> dict[str, Any]:
    return {"id": ids.cid, "object": kind, "created": ids.created, "model": MODEL, "choices": [choice]}


def completion_body(ids: Ids, plan: Plan) -> dict[str, Any]:
    """Non-streaming chat.completion response."""
    msg: dict[str, Any] = {"role": "assistant", "content": plan.reply}
    fin = "stop"
    if plan.tool_call:
        name, args = plan.tool_call
        function = {"name": name, "arguments": json.dumps(args)}
        call = {"id": ids.call_id, "type": "function", "function": function}
        msg, fin = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
    body = _envelope(ids, "chat.completion", {"index": 0, "finish_reason": fin, "message": msg})
    body["usage"] = _USAGE
    return body


def _deltas(ids: Ids, plan: Plan) -> tuple[list[dict[str, Any]], str]:
    if not plan.tool_call:
        return [{"role": "assistant", "content": ""}, {"content": plan.reply}], "stop"
    name, args = plan.tool_call
    head = {"index": 0, "id": ids.call_id, "type": "function", "function": {"name": name, "arguments": ""}}
    first = {"role": "assistant", "content": None, "tool_calls": [head]}
    rest = {"tool_calls": [{"index": 0, "function": {"arguments": json.dumps(args)}}]}
    return [first, rest], "tool_calls"


def stream_events(ids: Ids, plan: Plan) -> list[bytes]:
    """Server-sent events of a streaming response, `data: [DONE]` last."""
    deltas, fin = _deltas(ids, plan)
    chunks = [_envelope(ids, "chat.completion.chunk", {"index": 0, "delta": d, "finish_reason": None}) for d in deltas]
    final = _envelope(ids, "chat.completion.chunk", {"index": 0, "delta": {}, "finish_reason": fin})
    final["usage"] = _USAGE
    events = [b"data: " + json.dumps(obj).encode() + b"\n\n" for obj in [*chunks, final]]
    return [*events, b"data: [DONE]\n\n"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        log(method="GET", path=self.path)
        if self.path.rstrip("/").endswith("/models"):
            model = {"id": MODEL, "object": "model", "created": 0, "owned_by": "mock", "context_length": 131072}
            self._json(200, {"object": "list", "data": [model]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._json(400, {"error": {"message": "bad json"}})
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json(404, {"error": {"message": "not found"}})
            return
        chat = parse_request(req)
        a = analyse(chat.messages)
        plan = plan_reply(a, chat.tools)
        delay = delay_seconds(a, chat.tools)
        if delay is not None:
            time.sleep(delay)
        log(**log_fields(chat, a, plan))
        ids = make_ids(time.time(), a.used)
        if chat.stream:
            self._stream(stream_events(ids, plan))
        else:
            self._json(200, completion_body(ids, plan))

    def _stream(self, events: list[bytes]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for event in events:
            self.wfile.write(event)
            self.wfile.flush()
        self.close_connection = True


def main() -> None:
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
