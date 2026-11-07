"""Minimal RFC 6455 WebSocket client over TLS, standard library only.

The worker service needs one outbound, authenticated, bidirectional
connection to the coordinator (WORK-002, SEC-002). This module implements only
what that needs: the client handshake, masked text/close/ping/pong frames, and
size limits on everything read from the network. Fragmented messages are
reassembled up to ``max_message`` bytes. Extensions and subprotocols are not
negotiated.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from dataclasses import dataclass
from urllib.parse import urlsplit

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_HEADER_BYTES = 16 * 1024


class WebSocketError(Exception):
    """Handshake failure, protocol violation or closed connection."""


class WebSocketClosed(WebSocketError):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"connection closed ({code}) {reason}".strip())
        self.code = code
        self.reason = reason


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")


def encode_frame(opcode: int, payload: bytes, *, mask: bool, fin: bool = True, mask_key: bytes | None = None) -> bytes:
    """Encode one frame; clients must mask, servers must not."""
    header = bytearray([(0x80 if fin else 0) | opcode])
    length = len(payload)
    mask_bit = 0x80 if mask else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 1 << 16:
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)
    if not mask:
        return bytes(header) + payload
    key = mask_key if mask_key is not None else os.urandom(4)
    masked = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + key + masked


def _read_exact(stream: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = stream.recv(count - len(data))
        if not chunk:
            raise WebSocketClosed(1006, "connection lost")
        data += chunk
    return bytes(data)


@dataclass(frozen=True)
class Frame:
    fin: bool
    opcode: int
    payload: bytes


def read_frame(stream: socket.socket, *, max_payload: int, expect_masked: bool) -> Frame:
    """Read one frame, refusing reserved bits, wrong masking and oversized payloads."""
    first, second = _read_exact(stream, 2)
    if first & 0x70:
        raise WebSocketError("reserved bits set without a negotiated extension")
    opcode = first & 0x0F
    if opcode not in {OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG}:
        raise WebSocketError(f"unknown opcode {opcode}")
    masked = bool(second & 0x80)
    if masked != expect_masked:
        raise WebSocketError("frame masking does not match the peer role")
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(stream, 8))[0]
    if opcode >= OP_CLOSE and (length > 125 or not first & 0x80):
        raise WebSocketError("control frames must be final and at most 125 bytes")
    if length > max_payload:
        raise WebSocketError(f"frame of {length} bytes exceeds the {max_payload}-byte limit")
    key = _read_exact(stream, 4) if masked else b""
    payload = _read_exact(stream, length)
    if masked:
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return Frame(bool(first & 0x80), opcode, payload)


# How long one outgoing frame may block. Independent of the receive timeout:
# the worker polls with a short receive timeout, and a send that inherited it
# failed on any link where a write blocks briefly (weak Wi-Fi, full TCP window).
SEND_TIMEOUT_SECONDS = 30.0


class WebSocketClient:
    """A blocking client connection. Not thread-safe; one reader and one writer at most."""

    def __init__(self, sock: socket.socket, *, max_message: int, send_timeout: float = SEND_TIMEOUT_SECONDS) -> None:
        self.sock = sock
        self.max_message = max_message
        self.send_timeout = send_timeout
        self.closed = False

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        context: ssl.SSLContext | None = None,
        timeout: float = 10.0,
        max_message: int = 1024 * 1024,
    ) -> "WebSocketClient":
        parts = urlsplit(url)
        if parts.scheme not in {"wss", "ws"} or not parts.hostname:
            raise WebSocketError("URL must be wss://host[:port]/path")
        secure = parts.scheme == "wss"
        port = parts.port or (443 if secure else 80)
        raw = socket.create_connection((parts.hostname, port), timeout=timeout)
        try:
            sock: socket.socket = raw
            if secure:
                tls = context or ssl.create_default_context()
                tls.minimum_version = max(tls.minimum_version, ssl.TLSVersion.TLSv1_2)
                sock = tls.wrap_socket(raw, server_hostname=parts.hostname)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            host = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
            lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
                     f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            for name, value in (headers or {}).items():
                if any(character in name + value for character in "\r\n"):
                    raise WebSocketError("header names and values must not contain line breaks")
                lines.append(f"{name}: {value}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            response = bytearray()
            while b"\r\n\r\n" not in response:
                if len(response) > MAX_HEADER_BYTES:
                    raise WebSocketError("handshake response headers are too large")
                chunk = sock.recv(1)
                if not chunk:
                    raise WebSocketError("connection closed during handshake")
                response += chunk
            head = response.decode("latin-1").split("\r\n")
            status = head[0].split(" ", 2)
            if len(status) < 2 or status[1] != "101":
                raise WebSocketError(f"handshake refused: {head[0][:200]}")
            fields = {}
            for line in head[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    fields[name.strip().lower()] = value.strip()
            if fields.get("sec-websocket-accept") != accept_key(key):
                raise WebSocketError("handshake accept key mismatch")
            sock.settimeout(None)
            return cls(sock, max_message=max_message)
        except BaseException:
            raw.close()
            raise

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode("utf-8"))

    def ping(self, payload: bytes = b"") -> None:
        self._send(OP_PING, payload)

    def _send(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise WebSocketClosed(1006, "connection already closed")
        previous = self.sock.gettimeout()
        self.sock.settimeout(self.send_timeout)
        try:
            self.sock.sendall(encode_frame(opcode, payload, mask=True))
        finally:
            self.sock.settimeout(previous)

    def receive_text(self, timeout: float | None = None) -> str | None:
        """Return the next text message, ``None`` on timeout; answer pings on the way."""
        self.sock.settimeout(timeout)
        parts = bytearray()
        opcode_seen: int | None = None
        try:
            while True:
                frame = read_frame(self.sock, max_payload=self.max_message, expect_masked=False)
                if frame.opcode == OP_PING:
                    self._send(OP_PONG, frame.payload)
                    continue
                if frame.opcode == OP_PONG:
                    continue
                if frame.opcode == OP_CLOSE:
                    code = struct.unpack("!H", frame.payload[:2])[0] if len(frame.payload) >= 2 else 1005
                    reason = frame.payload[2:].decode("utf-8", "replace")
                    self.close(code if code not in {1005, 1006} else 1000)
                    raise WebSocketClosed(code, reason)
                if frame.opcode == OP_CONT and opcode_seen is None:
                    raise WebSocketError("continuation frame without a message")
                if frame.opcode in {OP_TEXT, OP_BINARY} and opcode_seen is not None:
                    raise WebSocketError("new message before the previous one finished")
                opcode_seen = opcode_seen if frame.opcode == OP_CONT else frame.opcode
                parts += frame.payload
                if len(parts) > self.max_message:
                    raise WebSocketError("message exceeds the size limit")
                if frame.fin:
                    if opcode_seen != OP_TEXT:
                        raise WebSocketError("binary messages are not used by this protocol")
                    return parts.decode("utf-8")
        except (socket.timeout, TimeoutError):
            if parts:
                raise WebSocketError("timed out inside a fragmented message") from None
            return None
        except UnicodeDecodeError as exc:
            raise WebSocketError("text message is not valid UTF-8") from exc

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        try:
            self.sock.sendall(encode_frame(OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120], mask=True))
        except OSError:
            pass
        finally:
            self.closed = True
            try:
                self.sock.close()
            except OSError:
                pass
