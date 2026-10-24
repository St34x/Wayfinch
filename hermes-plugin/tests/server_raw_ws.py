# SPDX-License-Identifier: Apache-2.0
"""A raw-socket WebSocket client for the WS server tests: the upgrade, masked frames with a chosen FIN bit and opcode
(so a message can be fragmented or a frame left unmasked), and a frame reader. Blocking: run it in an executor."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import socket
import struct
from collections.abc import Callable
from typing import Any

from server_helpers import HELLO, Wiring, app_for, serving

TEXT, BINARY, CONTINUATION, CLOSE = 0x1, 0x2, 0x0, 0x8


def frame_bytes(opcode: int, payload: bytes, *, fin: bool = True, masked: bool = True) -> bytes:
    head = bytes([(0x80 if fin else 0) | opcode])
    length = len(payload)
    flag = 0x80 if masked else 0
    if length < 126:
        head += bytes([flag | length])
    elif length < 65536:
        head += bytes([flag | 126]) + struct.pack(">H", length)
    else:
        head += bytes([flag | 127]) + struct.pack(">Q", length)
    if not masked:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def _connect(port: int, timeout: float, rcvbuf: int | None) -> socket.socket:
    """A TCP connection to the test server; *rcvbuf* shrinks the CLIENT's receive buffer before it connects (the
    server's socket is never touched), which makes a client that does not read stall the server's writes early."""
    if rcvbuf is None:
        return socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    sock.settimeout(timeout)
    sock.connect(("127.0.0.1", port))
    return sock


class RawWs:
    """One connection: ``open`` does the HTTP upgrade, ``send`` writes bytes, ``recv`` reads one server frame."""

    def __init__(self, port: int, headers: dict[str, str], timeout: float = 5.0, rcvbuf: int | None = None) -> None:
        self.sock = _connect(port, timeout, rcvbuf)
        nonce = base64.b64encode(os.urandom(16)).decode()
        lines = {
            "Host": "x",
            "Connection": "Upgrade",
            "Upgrade": "websocket",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": nonce,
            **headers,
        }
        request = "GET /ws HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in lines.items()) + "\r\n"
        self.sock.sendall(request.encode())
        self.upgrade_reply = self._read_head()
        self._buffer = b""

    def _read_head(self) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(1)
            if not chunk:
                break
            data += chunk
        return data

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def send_message(self, opcode: int, payload: bytes, pieces: int = 1) -> None:
        """One message as *pieces* fragments (FIN only on the last)."""
        size = -(-len(payload) // pieces)
        chunks = [payload[i : i + size] for i in range(0, len(payload), size)] or [b""]
        for index, chunk in enumerate(chunks):
            first = index == 0
            self.send(frame_bytes(opcode if first else CONTINUATION, chunk, fin=index == len(chunks) - 1))

    def _exact(self, count: int) -> bytes | None:
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self._buffer += chunk
        taken, self._buffer = self._buffer[:count], self._buffer[count:]
        return taken

    def recv(self) -> tuple[int, bytes] | None:
        """The next server frame as ``(opcode, payload)``; ``None`` when the server closed the TCP connection."""
        try:
            head = self._exact(2)
            if head is None:
                return None
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._exact(2) or b"\0\0")[0]
            elif length == 127:
                length = struct.unpack(">Q", self._exact(8) or bytes(8))[0]
            payload = self._exact(length)
        except (TimeoutError, ConnectionResetError):
            return None
        return None if payload is None else (head[0] & 0x0F, payload)

    def close_code(self) -> int | None:
        """Read frames until the server's close frame; its status code, or ``None`` if none came."""
        while (frame := self.recv()) is not None:
            if frame[0] == CLOSE:
                return struct.unpack(">H", frame[1][:2])[0] if len(frame[1]) >= 2 else 1005
        return None

    def shut(self) -> None:
        self.sock.close()


def hello_bytes(**body: Any) -> bytes:
    frame = copy.deepcopy(HELLO)
    frame["body"].update(body)
    return json.dumps(frame).encode()


def session(w: Wiring, scenario: Callable[[RawWs], Any], **changes: Any) -> Any:
    async def main() -> Any:
        async with serving(app_for(w, **changes)) as port:

            def work() -> Any:
                ws = RawWs(port, w.headers())
                try:
                    return scenario(ws)
                finally:
                    ws.shut()

            return await asyncio.get_running_loop().run_in_executor(None, work)

    return asyncio.run(asyncio.wait_for(main(), 30))


def say_hello(ws: RawWs) -> dict[str, Any]:
    ws.send(frame_bytes(TEXT, hello_bytes()))
    return reply(ws)


def reply(ws: RawWs) -> dict[str, Any]:
    got = ws.recv()
    assert got is not None and got[0] == TEXT, got
    decoded: dict[str, Any] = json.loads(got[1])
    return decoded
