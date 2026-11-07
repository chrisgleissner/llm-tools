"""The standard-library WebSocket client against a scripted loopback server."""

from __future__ import annotations

import shutil
import socket
import ssl
import struct
import subprocess
import threading

import pytest

from llm_tools import bridge_ws as ws
from llm_tools.bridge_ws import WebSocketClient, WebSocketClosed, WebSocketError


class Server:
    """Accept one connection, answer the handshake, then run ``script(conn)``."""

    def __init__(self, script, *, status="101 Switching Protocols", accept=True, context=None):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.script, self.status, self.accept, self.context = script, status, accept, context
        self.request = b""
        self.received = []
        self.error = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.listener.accept()
            if self.context is not None:
                conn = self.context.wrap_socket(conn, server_side=True)
            with conn:
                while b"\r\n\r\n" not in self.request:
                    chunk = conn.recv(1024)
                    if not chunk:
                        return
                    self.request += chunk
                key = [line.split(b":", 1)[1].strip().decode() for line in self.request.split(b"\r\n") if line.lower().startswith(b"sec-websocket-key")][0]
                lines = [f"HTTP/1.1 {self.status}", "Upgrade: websocket", "Connection: Upgrade"]
                if self.accept:
                    lines.append(f"Sec-WebSocket-Accept: {ws.accept_key(key)}")
                conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
                self.script(self, conn)
        except Exception as exc:  # noqa: BLE001 - surfaced by the test
            self.error = exc
        finally:
            self.listener.close()

    def read(self, conn):
        frame = ws.read_frame(conn, max_payload=1 << 20, expect_masked=True)
        self.received.append(frame)
        return frame

    def join(self):
        self.thread.join(5)
        assert not self.thread.is_alive()


def _connect(server, **kwargs):
    return WebSocketClient.connect(f"ws://127.0.0.1:{server.port}/api/v1/worker/connect?x=1", headers={"Authorization": "Bearer abc"}, **kwargs)


def test_text_round_trip_ping_fragments_and_close():
    def script(server, conn):
        assert server.read(conn).payload == b"hello"
        conn.sendall(ws.encode_frame(ws.OP_PING, b"p", mask=False))
        conn.sendall(ws.encode_frame(ws.OP_PONG, b"", mask=False))
        conn.sendall(ws.encode_frame(ws.OP_TEXT, b"wor", mask=False, fin=False))
        conn.sendall(ws.encode_frame(ws.OP_CONT, b"ld" + b"!" * 70000, mask=False))
        assert server.read(conn).opcode == ws.OP_PONG
        assert server.read(conn).opcode == ws.OP_PING
        conn.sendall(ws.encode_frame(ws.OP_CLOSE, struct.pack("!H", 1001) + b"bye", mask=False))
        server.read(conn)

    server = Server(script)
    client = _connect(server)
    client.send_text("hello")
    assert client.receive_text(timeout=5) == "world" + "!" * 70000
    client.ping()
    with pytest.raises(WebSocketClosed) as closed:
        client.receive_text(timeout=5)
    assert (closed.value.code, closed.value.reason) == (1001, "bye")
    client.close()  # idempotent
    with pytest.raises(WebSocketClosed):
        client.send_text("late")
    server.join()
    assert server.error is None
    assert b"GET /api/v1/worker/connect?x=1 HTTP/1.1" in server.request and b"Authorization: Bearer abc" in server.request
    assert server.received[-1].opcode == ws.OP_CLOSE


def test_receive_timeout_returns_none():
    done = threading.Event()
    server = Server(lambda server, conn: done.wait(5))
    client = _connect(server)
    assert client.receive_text(timeout=0.05) is None
    done.set()
    client.close()
    server.join()


def test_send_does_not_inherit_the_short_receive_timeout():
    """A write that blocks longer than the receive poll interval must not fail.

    The worker polls with receive_text(timeout=0.5). Before the fix the socket
    kept that timeout, so sendall raised TimeoutError whenever a write blocked
    for more than 0.5 s, and the worker reconnected every few seconds on a
    slow link.
    """

    class SlowSocket:
        def __init__(self):
            self.timeout = None
            self.sent = b""

        def settimeout(self, value):
            self.timeout = value

        def gettimeout(self):
            return self.timeout

        def sendall(self, data):
            # A blocking write that takes longer than the receive timeout.
            if self.timeout is not None and self.timeout < 1.0:
                raise TimeoutError("The write operation timed out")
            self.sent += data

    sock = SlowSocket()
    client = WebSocketClient(sock, max_message=1024)  # type: ignore[arg-type]
    sock.settimeout(0.5)  # what receive_text(timeout=0.5) leaves behind
    client.send_text("hello")
    assert sock.sent
    assert sock.timeout == 0.5  # the receive timeout is restored afterwards


@pytest.mark.parametrize(
    ("frames", "message"),
    [
        ([ws.encode_frame(ws.OP_TEXT, b"x", mask=True)], "masking"),
        ([bytes([0xC1, 0x00])], "reserved bits"),
        ([bytes([0x83, 0x00])], "unknown opcode"),
        ([ws.encode_frame(ws.OP_PING, b"x" * 126, mask=False)], "control frames"),
        ([ws.encode_frame(ws.OP_TEXT, b"x" * 2000, mask=False)], "exceeds"),
        ([ws.encode_frame(ws.OP_CONT, b"x", mask=False)], "continuation"),
        ([ws.encode_frame(ws.OP_TEXT, b"a", mask=False, fin=False), ws.encode_frame(ws.OP_TEXT, b"b", mask=False)], "previous one"),
        ([ws.encode_frame(ws.OP_BINARY, b"a", mask=False)], "binary"),
        ([ws.encode_frame(ws.OP_TEXT, b"\xff", mask=False)], "UTF-8"),
        ([ws.encode_frame(ws.OP_TEXT, b"a" * 600, mask=False, fin=False), ws.encode_frame(ws.OP_CONT, b"a" * 600, mask=False)], "size limit"),
        ([ws.encode_frame(ws.OP_TEXT, b"a", mask=False, fin=False)], "fragmented"),
        ([b"\x81"], "connection lost"),
    ],
)
def test_protocol_violations_are_errors(frames, message):
    done = threading.Event()

    def script(server, conn):
        for frame in frames:
            conn.sendall(frame)
        if message != "connection lost":
            done.wait(5)

    server = Server(script)
    client = _connect(server, max_message=1024)
    with pytest.raises(WebSocketError, match=message):
        client.receive_text(timeout=0.3)
    done.set()
    client.close()
    server.join()


@pytest.mark.parametrize(("status", "accept", "message"), [("403 Forbidden", False, "handshake refused"), ("101 Switching Protocols", False, "accept key")])
def test_handshake_failures(status, accept, message):
    server = Server(lambda server, conn: None, status=status, accept=accept)
    with pytest.raises(WebSocketError, match=message):
        _connect(server)
    server.join()


def test_handshake_input_validation():
    with pytest.raises(WebSocketError, match="URL"):
        WebSocketClient.connect("https://example")
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    with pytest.raises(WebSocketError, match="line breaks"):
        WebSocketClient.connect(f"ws://127.0.0.1:{port}/", headers={"X": "a\r\nInjected: 1"})
    listener.close()


def test_handshake_rejects_oversized_or_truncated_responses():
    for payload, message in [(b"HTTP/1.1 101 OK\r\n" + b"X: y\r\n" * 4000, "too large"), (b"HTTP/1.1 101", "during handshake")]:
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        def serve(listener=listener, payload=payload):
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(payload)
            listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with pytest.raises(WebSocketError, match=message):
            WebSocketClient.connect(f"ws://127.0.0.1:{port}/")
        thread.join(5)


def _openssl_can_make_a_ca() -> bool:
    """OpenSSL 3 (not the LibreSSL that macOS ships, whose `-addext` handling differs) creates the test CA."""
    if shutil.which("openssl") is None:
        return False
    version = subprocess.run(["openssl", "version"], capture_output=True, text=True, check=False).stdout
    return version.startswith("OpenSSL ")


@pytest.mark.skipif(not _openssl_can_make_a_ca(), reason="needs the OpenSSL 3 command line tool to create a test CA (macOS ships LibreSSL)")
def test_wss_verifies_the_server_against_the_given_ca(tmp_path):
    def run(*args):
        subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=tmp_path)

    run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-subj", "/CN=test ca",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-addext", "subjectKeyIdentifier=hash")
    run("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=localhost")
    (tmp_path / "ext").write_text(
        "subjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n"
    )
    run("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.pem", "-days", "2", "-extfile", "ext")
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_context.load_cert_chain(tmp_path / "srv.pem", tmp_path / "srv.key")

    def script(server, conn):
        server.read(conn)
        conn.sendall(ws.encode_frame(ws.OP_TEXT, b"secure", mask=False))
        server.read(conn)

    server = Server(script, context=server_context)
    strict = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
    strict.verify_flags |= ssl.VERIFY_X509_STRICT
    client = WebSocketClient.connect(f"wss://localhost:{server.port}/", context=strict)
    client.send_text("hi")
    assert client.receive_text(timeout=5) == "secure"
    client.close()
    server.join()
    assert server.error is None

    untrusted = Server(lambda server, conn: None, context=server_context)
    with pytest.raises(ssl.SSLCertVerificationError):
        WebSocketClient.connect(f"wss://localhost:{untrusted.port}/", context=ssl.create_default_context())
    untrusted.join()
