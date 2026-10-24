# SPDX-License-Identifier: Apache-2.0
"""A raw-socket WebSocket client for the connection tests: it can leave frames unread (a peer that does not answer)
and shows every byte the server wrote, so a test can assert what comes after a close frame."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

MASK = b"\x01\x02\x03\x04"
TEXT, CLOSE = 1, 8


def masked(opcode: int, payload: bytes) -> bytes:
    """One client frame (clients must mask)."""
    size = len(payload)
    length = bytes([0x80 | size]) if size < 126 else bytes([0x80 | 126]) + size.to_bytes(2, "big")
    return bytes([0x80 | opcode]) + length + MASK + bytes(byte ^ MASK[i % 4] for i, byte in enumerate(payload))


def parse_frames(data: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """The complete server frames in *data* as ``(opcode, payload)`` and the incomplete tail."""
    frames: list[tuple[int, bytes]] = []
    while len(data) >= 2:
        size, offset = data[1] & 0x7F, 2
        if size == 126 and len(data) >= 4:
            size, offset = int.from_bytes(data[2:4], "big"), 4
        elif size == 127 and len(data) >= 10:
            size, offset = int.from_bytes(data[2:10], "big"), 10
        elif size >= 126:
            break
        if len(data) < offset + size:
            break
        frames.append((data[0] & 0x0F, data[offset : offset + size]))
        data = data[offset + size :]
    return frames, data


class RawClient:
    def __init__(self, sock: socket.socket, pending: bytes) -> None:
        self.sock, self.pending = sock, pending

    @classmethod
    async def connect(cls, port: int, headers: dict[str, str], rcvbuf: int = 0) -> RawClient:
        loop = asyncio.get_running_loop()
        sock = socket.socket()
        sock.setblocking(False)
        if rcvbuf:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        await loop.sock_connect(sock, ("127.0.0.1", port))
        lines = ["GET /ws HTTP/1.1", f"Host: 127.0.0.1:{port}", *(f"{k}: {v}" for k, v in headers.items()), "", ""]
        await loop.sock_sendall(sock, "\r\n".join(lines).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await loop.sock_recv(sock, 4096)
            assert chunk, "the server closed during the handshake"
            head += chunk
        assert head.startswith(b"HTTP/1.1 101"), head[:40]
        return cls(sock, head.partition(b"\r\n\r\n")[2])

    async def send_json(self, frame: dict[str, Any]) -> None:
        await asyncio.get_running_loop().sock_sendall(self.sock, masked(TEXT, json.dumps(frame).encode()))

    async def recv_frames(self, count: int, timeout: float = 5.0) -> list[tuple[int, bytes]]:
        """Reads until *count* complete frames are in (or the stream ends)."""
        loop, data = asyncio.get_running_loop(), self.pending
        async with asyncio.timeout(timeout):
            while len(parse_frames(data)[0]) < count:
                chunk = await loop.sock_recv(self.sock, 1 << 20)
                if not chunk:
                    break
                data += chunk
        frames, self.pending = parse_frames(data)
        return frames

    async def recv_to_end(self, timeout: float = 5.0) -> bytes:
        """Everything the server still sends until it ends the connection (a reset ends it too)."""
        loop, chunks = asyncio.get_running_loop(), [self.pending]
        async with asyncio.timeout(timeout):
            try:
                while chunk := await loop.sock_recv(self.sock, 1 << 20):
                    chunks.append(chunk)
            except ConnectionResetError:
                pass
        self.pending = b""
        return b"".join(chunks)

    def close(self) -> None:
        self.sock.close()
