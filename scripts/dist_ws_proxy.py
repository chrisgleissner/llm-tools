"""A TLS-terminating WebSocket proxy that can delay or drop whole frames (PRD-008).

``tests/fault_proxy.py`` forwards TLS bytes untouched, so it can cut, stall and delay a link but
cannot lose one WebSocket message: removing bytes from a TLS stream only breaks the stream. This
proxy ends TLS on both sides instead, so it sees the WebSocket frames of the worker protocol:

* the worker trusts the proxy's own throw-away certificate (made with the ``openssl`` command line
  tool, valid for 127.0.0.1 only) and connects to the proxy;
* the proxy connects to the coordinator and verifies the coordinator's CA like a normal worker;
* after the HTTP upgrade, each side's frames are read, optionally delayed or dropped, and written
  to the other side again (masked towards the coordinator, unmasked towards the worker).

Dropped frames model a lossy middlebox, not a normal TCP failure. They are used to show that the
protocol never reports a lost message as success: a lost ``command`` stays unfinished, a lost
``events`` batch is detected by the cursor check, and everything converges after a reconnect.

Standard library plus the ``openssl`` binary; binds 127.0.0.1 only.
"""

from __future__ import annotations

import json
import random
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm_tools import bridge_ws  # noqa: E402

UP = "up"  # worker to coordinator
DOWN = "down"  # coordinator to worker
MAX_PAYLOAD = 2 * 1024 * 1024
HANDSHAKE_LIMIT = 16 * 1024


class OpenSSLUnavailable(RuntimeError):
    """The ``openssl`` binary needed to mint the proxy certificate is not installed."""


def make_proxy_certificate(directory: Path) -> tuple[Path, Path]:
    """Create a self-signed certificate for 127.0.0.1 and return ``(certificate, key)`` paths."""
    openssl = shutil.which("openssl")
    if openssl is None:
        raise OpenSSLUnavailable("the openssl command line tool is required to build the WebSocket frame proxy certificate")
    cert, key = directory / "proxy-cert.pem", directory / "proxy-key.pem"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", str(key), "-out", str(cert),
         "-days", "2", "-subj", "/CN=bridge-chaos-proxy", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,digitalSignature,keyCertSign",
         "-addext", "extendedKeyUsage=serverAuth"],
        check=True, capture_output=True, timeout=30,
    )
    key.chmod(0o600)
    return cert, key


@dataclass
class _Link:
    client: socket.socket
    upstream: socket.socket
    closed: threading.Event = field(default_factory=threading.Event)  # set once the link is being torn down
    released: threading.Event = field(default_factory=threading.Event)  # set once the owning thread has closed both sockets
    reset: bool = False  # close with a TCP reset instead of a normal FIN


@dataclass
class _DropRule:
    direction: str
    message_type: str | None  # JSON "type" of a text frame, or None for any data frame
    remaining: int
    dropped: int = 0


def frame_message_type(payload: bytes) -> str | None:
    """The protocol message ``type`` of a text frame payload, or ``None`` when it is not a JSON message."""
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    return value.get("type") if isinstance(value, dict) and isinstance(value.get("type"), str) else None


class WsFrameProxy:
    """Listens on a loopback port; every accepted worker connection is relayed frame by frame."""

    def __init__(self, upstream_port: int, upstream_ca: Path, directory: Path, *, seed: int = 0, host: str = "127.0.0.1") -> None:
        self.upstream = (host, upstream_port)
        self.certificate, key = make_proxy_certificate(directory)
        self._server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._server_context.load_cert_chain(str(self.certificate), str(key))
        self._upstream_context = ssl.create_default_context(cafile=str(upstream_ca))
        self.random = random.Random(seed)
        self._lock = threading.Lock()
        self._links: list[_Link] = []
        self._delay = {UP: 0.0, DOWN: 0.0}
        self._rules: list[_DropRule] = []
        self._drop_probability = {UP: 0.0, DOWN: 0.0}
        self._refusing = False
        self._stop = threading.Event()
        self.accepted = 0
        self.refused = 0
        self.resets = 0
        self.forwarded: dict[tuple[str, str], int] = {}
        self.dropped: dict[tuple[str, str], int] = {}
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, 0))
        self._listener.listen(64)
        self._listener.settimeout(0.1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="ws-frame-proxy")
        self._thread.start()

    # ---- fault controls
    def set_delay(self, direction: str, seconds: float) -> None:
        """Hold every data frame in ``direction`` for ``seconds`` (frame order is kept)."""
        self._delay[direction] = seconds

    def clear_delay(self) -> None:
        self._delay = {UP: 0.0, DOWN: 0.0}

    def drop_next(self, direction: str, message_type: str | None = None, count: int = 1) -> _DropRule:
        """Swallow the next ``count`` text frames of ``direction`` whose message ``type`` matches (any type when ``None``)."""
        rule = _DropRule(direction, message_type, count)
        with self._lock:
            self._rules.append(rule)
        return rule

    def set_drop_probability(self, direction: str, probability: float) -> None:
        """Swallow each data frame of ``direction`` with this probability (seeded)."""
        self._drop_probability[direction] = probability

    def clear_drops(self) -> None:
        with self._lock:
            self._rules.clear()
        self._drop_probability = {UP: 0.0, DOWN: 0.0}

    def refuse(self) -> None:
        self._refusing = True

    def allow(self) -> None:
        self._refusing = False

    def reset_all(self) -> int:
        with self._lock:
            links, self._links = self._links, []
        for link in links:
            self._close(link, reset=True)
        # Return only after every link's sockets are closed (with a reset), so the caller can rely on the cut.
        for link in links:
            link.released.wait(10)
        self.resets += len(links)
        return len(links)

    def cut(self) -> None:
        self.refuse()
        self.reset_all()

    def restore(self) -> None:
        self.allow()
        self.clear_delay()
        self.clear_drops()

    def active(self) -> int:
        with self._lock:
            return sum(1 for link in self._links if not link.closed.is_set())

    def count(self, table: dict[tuple[str, str], int], direction: str, message_type: str) -> int:
        return table.get((direction, message_type), 0)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5)
        self._listener.close()
        self.reset_all()

    # ---- internals
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            if self._refusing:
                self.refused += 1
                self._abort(client)
                continue
            threading.Thread(target=self._serve, args=(client,), daemon=True, name="ws-frame-proxy-link").start()

    @staticmethod
    def _abort(sock: socket.socket) -> None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _close(self, link: _Link, *, reset: bool) -> None:
        """Start tearing the link down from any thread: wake every thread blocked on its sockets.

        The sockets are not closed here. The thread that owns them (``_serve``) closes them after both
        pump threads have returned. Closing a descriptor while another thread is still inside
        ``recv``/``send`` on it lets the kernel hand the same descriptor number to the next
        connection while the old thread (and its TLS object) still writes to that number, which injects
        stray bytes into an unrelated connection.
        """
        with self._lock:
            if link.closed.is_set():
                return
            link.reset = reset
            link.closed.set()
        for sock in (link.client, link.upstream):
            try:
                if reset:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                # The socket-level call: SSLSocket.shutdown would drop the TLS object under a running reader.
                socket.socket.shutdown(sock, socket.SHUT_RD if reset else socket.SHUT_RDWR)
            except OSError:
                pass

    @staticmethod
    def _release(link: _Link) -> None:
        for sock in (link.client, link.upstream):
            try:
                sock.close()
            except OSError:
                pass
        link.released.set()

    @staticmethod
    def _read_head(sock: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            if len(data) > HANDSHAKE_LIMIT:
                raise OSError("handshake headers too large")
            chunk = sock.recv(1)
            if not chunk:
                raise OSError("closed during handshake")
            data += chunk
        return bytes(data)

    def _serve(self, raw_client: socket.socket) -> None:
        link: _Link | None = None
        pump: threading.Thread | None = None
        opened: list[socket.socket] = [raw_client]
        try:
            raw_client.settimeout(10)
            client = self._server_context.wrap_socket(raw_client, server_side=True)
            opened.append(client)
            raw_upstream = socket.create_connection(self.upstream, timeout=10)
            opened.append(raw_upstream)
            upstream = self._upstream_context.wrap_socket(raw_upstream, server_hostname=self.upstream[0])
            opened.append(upstream)
            for sock in (client, upstream):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            link = _Link(client, upstream)
            request = self._read_head(client)
            upstream.sendall(request)
            response = self._read_head(upstream)
            client.sendall(response)
            if b" 101 " not in response.split(b"\r\n", 1)[0]:
                return
            client.settimeout(None)
            upstream.settimeout(None)
            with self._lock:
                self._links.append(link)
            self.accepted += 1
            pump = threading.Thread(target=self._pump, args=(link, client, upstream, UP), daemon=True, name="ws-frame-proxy-pump")
            pump.start()
            self._pump(link, upstream, client, DOWN)
        except (OSError, ssl.SSLError, bridge_ws.WebSocketError):
            pass
        finally:
            if link is not None:
                with self._lock:
                    if link in self._links:
                        self._links.remove(link)
                self._close(link, reset=False)
                if pump is not None:
                    pump.join()  # the sockets were shut down, so the pump thread returns promptly
                self._release(link)
            else:
                for sock in opened:
                    try:
                        sock.close()
                    except OSError:
                        pass

    def _should_drop(self, direction: str, kind: str | None) -> bool:
        with self._lock:
            for rule in self._rules:
                if rule.direction == direction and rule.remaining > 0 and (rule.message_type is None or rule.message_type == kind):
                    rule.remaining -= 1
                    rule.dropped += 1
                    return True
            probability = self._drop_probability[direction]
            return bool(probability) and self.random.random() < probability

    def _pump(self, link: _Link, source: ssl.SSLSocket, sink: ssl.SSLSocket, direction: str) -> None:
        # Frames from the worker are masked; frames from the coordinator are not. The proxy re-masks towards the coordinator.
        masked_in = direction == UP
        try:
            while not link.closed.is_set() and not self._stop.is_set():
                frame = bridge_ws.read_frame(source, max_payload=MAX_PAYLOAD, expect_masked=masked_in)
                data_frame = frame.opcode in (bridge_ws.OP_TEXT, bridge_ws.OP_BINARY, bridge_ws.OP_CONT)
                kind = frame_message_type(frame.payload) if frame.opcode == bridge_ws.OP_TEXT and frame.fin else None
                label = kind or f"opcode-{frame.opcode}"
                if data_frame:
                    if self._should_drop(direction, kind):
                        self.dropped[(direction, label)] = self.dropped.get((direction, label), 0) + 1
                        continue
                    delay = self._delay[direction]
                    if delay:
                        link.closed.wait(delay)  # a torn-down link ends the wait early
                if link.closed.is_set():
                    break
                # Counted before the send: a reader that has received the frame must already see it counted.
                self.forwarded[(direction, label)] = self.forwarded.get((direction, label), 0) + 1
                sink.sendall(bridge_ws.encode_frame(frame.opcode, frame.payload, mask=direction == UP, fin=frame.fin))
                if frame.opcode == bridge_ws.OP_CLOSE:
                    break
        except (OSError, ssl.SSLError, bridge_ws.WebSocketError):
            pass
        finally:
            self._close(link, reset=False)


def temp_directory() -> Path:
    return Path(tempfile.mkdtemp(prefix="wsproxy-"))
