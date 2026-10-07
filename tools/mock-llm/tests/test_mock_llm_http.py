# SPDX-License-Identifier: Apache-2.0
"""Unit tests of tools/mock-llm/mock_llm.py: the request log, the `<<TOOLS>>` directive and the HTTP glue, served by
the real Handler on a free loopback port."""

import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_llm
import mock_script


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def fields(text: str, tools: list[Any]) -> dict[str, Any]:
    chat = mock_llm.ChatRequest("m", False, [user(text)], tools)
    a = mock_script.analyse(chat.messages)
    return mock_llm.log_fields(chat, a, mock_script.plan_reply(a, chat.tools))


def test_tools_directive_logs_every_offered_tool_name() -> None:
    logged = fields("<<TOOLS>>", ["web_search", None, "clarify"])
    assert logged["all_tools"] == ["clarify", "web_search"]
    assert logged["tools_of_interest"] == ["clarify"]
    assert fields("hi", ["phone_notify", "web_search"])["all_tools"] is None


@pytest.fixture
def port() -> Iterator[int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mock_llm.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_port
    srv.shutdown()
    srv.server_close()


def request(port: int, method: str, path: str, body: bytes = b"") -> tuple[int, str, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, resp.getheader("Content-Type") or "", data


def chat(port: int, payload: dict[str, Any]) -> tuple[int, str, bytes]:
    return request(port, "POST", "/v1/chat/completions", json.dumps(payload).encode())


def offered(*names: str) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": n}} for n in names]


def test_http_non_streaming_tool_call_response(port: int) -> None:
    payload = {"messages": [user('@@CALL terminal {"command": "ls"}@@')], "tools": offered("terminal")}
    status, ctype, data = chat(port, payload)
    body = json.loads(data)
    assert (status, ctype, body["object"], body["model"]) == (200, "application/json", "chat.completion", "mock-model")
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"] == {"name": "terminal", "arguments": '{"command": "ls"}'}
    assert call["id"].startswith("call_") and call["id"].endswith("_0")
    assert body["usage"] == {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}


def test_http_non_streaming_text_response(port: int) -> None:
    body = json.loads(chat(port, {"messages": [user("hi")]})[2])
    assert body["choices"] == [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Mock title"}}
    ]


def test_http_streaming_text_response_ends_with_done(port: int) -> None:
    payload = {"messages": [user("<<DELAY:0>> hi")], "tools": offered("terminal"), "stream": True}
    status, ctype, data = chat(port, payload)
    events = data.decode().split("\n\n")
    assert (status, ctype) == (200, "text/event-stream")
    assert events[-2:] == ["data: [DONE]", ""]
    chunks = [json.loads(e.removeprefix("data: ")) for e in events[:-2]]
    assert [c["choices"][0]["delta"] for c in chunks] == [
        {"role": "assistant", "content": ""},
        {"content": "MOCK[?] done; you said: <<DELAY:0>> hi"},
        {},
    ]
    assert [c["choices"][0]["finish_reason"] for c in chunks] == [None, None, "stop"]
    assert "usage" in chunks[-1] and "usage" not in chunks[0]


def test_http_streaming_tool_call_sends_arguments_in_a_second_chunk(port: int) -> None:
    payload = {"messages": [user('@@CALL terminal {"command": "s"}@@')], "tools": offered("terminal"), "stream": True}
    events = chat(port, payload)[2].decode().split("\n\n")
    deltas = [json.loads(e.removeprefix("data: "))["choices"][0] for e in events[:3]]
    assert deltas[0]["delta"]["tool_calls"][0]["function"] == {"name": "terminal", "arguments": ""}
    assert deltas[1]["delta"] == {"tool_calls": [{"index": 0, "function": {"arguments": '{"command": "s"}'}}]}
    assert deltas[2]["finish_reason"] == "tool_calls"


def test_http_models_list_and_unknown_paths(port: int) -> None:
    status, _, data = request(port, "GET", "/v1/models/")
    assert status == 200 and json.loads(data)["data"][0]["id"] == "mock-model"
    assert request(port, "GET", "/v1/other")[0] == 404
    assert request(port, "POST", "/v1/other", b"{}")[0] == 404


def test_http_bad_json_body_is_a_400(port: int) -> None:
    status, _, data = request(port, "POST", "/v1/chat/completions", b"{nope")
    assert (status, json.loads(data)) == (400, {"error": {"message": "bad json"}})


def test_http_non_utf8_body_is_a_400(port: int) -> None:
    status, _, data = request(port, "POST", "/v1/chat/completions", b'\xff\xfe{"messages": [}')
    assert (status, json.loads(data)) == (400, {"error": {"message": "bad json"}})


def test_http_request_without_content_length_is_answered_as_an_empty_request(port: int) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", "/v1/chat/completions")
    conn.putheader("Connection", "close")
    conn.endheaders()
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 200 and body["choices"][0]["message"]["content"] == "Mock title"


def test_http_delay_directive_sleeps_before_answering(port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    chat(port, {"messages": [user("slow <<DELAY:3>>")], "tools": offered("terminal")})
    chat(port, {"messages": [user("slow <<DELAY:3>>")]})  # no tools: an auxiliary call never sleeps
    assert slept == [3]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def test_mock_llm_runs_as_a_script_from_another_directory_with_port_and_log_file(tmp_path: Path) -> None:
    script = Path(mock_llm.__file__).resolve()
    port, log_file = free_port(), tmp_path / "mock.jsonl"
    argv = [sys.executable, str(script), "--port", str(port), "--log-file", str(log_file)]
    proc = subprocess.Popen(argv, cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stderr is not None
        listening = json.loads(proc.stderr.readline())
        assert (listening["event"], listening["host"], listening["port"]) == ("listening", "127.0.0.1", port)
        status, _, data = request(port, "GET", "/v1/models")
        assert status == 200 and json.loads(data)["data"][0]["id"] == "mock-model"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    logged = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert [(e.get("event"), e.get("method")) for e in logged] == [("listening", None), (None, "GET")]


def test_every_request_is_logged_as_one_json_line(
    port: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = tmp_path / "mock.jsonl"
    monkeypatch.setattr(mock_llm, "LOG_FILE", str(log_file))
    chat(port, {"model": "m1", "messages": [user("hi")], "tools": offered("terminal", "phone_x")})
    line = json.loads(log_file.read_text().splitlines()[-1])
    assert line["emit"] == "text:MOCK[?] done; you said: hi"
    assert (line["model"], line["n_tools"], line["tools_of_interest"]) == ("m1", 2, ["phone_x", "terminal"])
    assert capsys.readouterr().err.splitlines()[-1] == log_file.read_text().splitlines()[-1]
