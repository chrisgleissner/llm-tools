"""Remote terminal sessions: control-mode parsing, single writer, worker wiring and a real tmux station."""

from __future__ import annotations

import base64
import json
import shutil
import sys
import threading

import pytest

from llm_tools import bridge_terminal_stream as stream
from llm_tools import bridge_worker as worker
from llm_tools.bridge import main
from llm_tools.bridge_protocol import Message, ProtocolError, decode
from llm_tools.bridge_runtime import mission_socket_path
from llm_tools.bridge_store import BridgeStore
from llm_tools.terminal import TmuxTerminalBackend
from tests.conftest import scaled, wait_for


def test_control_mode_decoding():
    assert stream.decode_control_output(b"a\\033[1mb\\134c\\015\\012") == b"a\x1b[1mb\\c\r\n"
    assert stream.decode_control_output(b"tail\\01") == b"tail\\01"  # incomplete escape stays literal
    assert stream.decode_control_output(b"\\9xy") == b"\\9xy"
    assert stream.parse_control_line(b"%output %3 hi\\015\n", "%3") == b"hi\r"
    assert stream.parse_control_line(b"%output %4 hi\n", "%3") is None
    assert stream.parse_control_line(b"%begin 1 2 3\n", "%3") is None
    assert stream.parse_control_line(b"%extended-output %3 12 : a : b\\015\n", "%3") == b"a : b\r"


def test_writer_registry_allows_one_writer_per_mission():
    registry = stream.WriterRegistry()
    assert registry.acquire("m", "a") and registry.acquire("m", "a")
    assert not registry.acquire("m", "b") and registry.acquire("n", "b")
    registry.release("m", "b")  # not the holder: no effect
    assert registry.holder("m") == "a"
    registry.release("m", "a")
    assert registry.holder("m") is None and registry.acquire("m", "b")


class FakeSource:
    def __init__(self, outputs=(), local=0):
        self.outputs = list(outputs)
        self.local = local
        self.inputs: list[bytes] = []
        self.resized: list[tuple[int, int]] = []
        self.closed = False
        self.resync = False

    def snapshot(self):
        return b"SCREEN"

    def size(self):
        return (80, 24)

    def local_clients(self):
        return self.local

    def send_input(self, data):
        self.inputs.append(data)

    def resize(self, columns, rows):
        self.resized.append((columns, rows))

    def read_output(self, timeout):
        if self.outputs:
            item = self.outputs.pop(0)
            if item == "PAUSE":
                self.resync = True
                return b""
            return item
        return None

    def take_resync(self):
        value, self.resync = self.resync, False
        return value

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent: list[dict] = []
        self.closed = False

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def receive_text(self, timeout=None):
        return self.incoming.pop(0) if self.incoming else None

    def close(self, code=1000, reason=""):
        self.closed = True


def _frame(kind, **fields):
    return json.dumps({"t": kind, **fields})


def _input(data: bytes) -> str:
    return _frame("i", d=base64.b64encode(data).decode())


def test_writer_session_streams_output_and_accepts_bounded_input():
    registry = stream.WriterRegistry()
    source = FakeSource([b"one", b"", "PAUSE", b"two", b"three", b"four", b"five"])
    incoming = [_input(b"ls\r"), "not json", "[]", _frame("i", d="***"), _input(b"x" * (stream.MAX_INPUT + 1)), _frame("r", cols=100, rows=30),
                _frame("r", cols=True, rows=30), _frame("r", cols=5000, rows=30)]
    connection = FakeConnection(incoming)
    session = stream.TerminalSession("s1", "m", "write", source, connection, writers=registry)
    assert session.run() == "station ended"
    kinds = [frame["t"] for frame in connection.sent]
    assert kinds[0] == "s" and connection.sent[0]["mode"] == "write" and kinds[-1] == "x"
    outputs = [base64.b64decode(frame["d"]) for frame in connection.sent if frame["t"] == "o"]
    assert outputs[0] == stream.CLEAR_SCREEN + b"SCREEN" and outputs[1] == b"one"
    assert stream.CLEAR_SCREEN + b"SCREEN" in outputs[2:]  # resync after a tmux pause
    assert source.inputs == [b"ls\r"] and source.resized == [(100, 30)]
    assert source.closed and connection.closed and registry.holder("m") is None


def test_second_writer_and_local_client_get_view_only():
    registry = stream.WriterRegistry()
    registry.acquire("m", "other")
    connection = FakeConnection([_input(b"rm -rf /\r")])
    source = FakeSource([b"x"])
    stream.TerminalSession("s2", "m", "write", source, connection, writers=registry).run()
    assert connection.sent[0]["mode"] == "view" and "another session" in connection.sent[0]["reason"]
    assert source.inputs == []
    local = FakeSource([b"x"], local=1)
    connection = FakeConnection([_input(b"x")])
    stream.TerminalSession("s3", "n", "write", local, connection, writers=stream.WriterRegistry()).run()
    assert "local terminal" in connection.sent[0]["reason"] and local.inputs == []
    readonly = FakeConnection()
    stream.TerminalSession("s4", "p", "write", FakeSource([]), readonly, writers=stream.WriterRegistry(), allow_write=False).run()
    assert readonly.sent[0]["mode"] == "view" and "read-only" in readonly.sent[0]["reason"]


def test_writer_role_is_dropped_when_a_local_client_attaches():
    registry = stream.WriterRegistry()
    source = FakeSource([b"a", b"b", b"c"])
    clock = iter([0.0, 0.5, 2.0, 2.5, 4.0, 5.0])

    class Attaching(FakeConnection):
        def receive_text(self, timeout=None):
            if len(self.sent) >= 3:
                source.local = 1
            return None

    connection = Attaching()
    stream.TerminalSession("s5", "m", "write", source, connection, writers=registry, clock=lambda: next(clock, 9.0)).run()
    statuses = [frame for frame in connection.sent if frame["t"] == "s"]
    assert [status["mode"] for status in statuses] == ["write", "view"]


def test_session_failure_is_contained():
    class Broken(FakeSource):
        def read_output(self, timeout):
            raise OSError("pipe")

    connection = FakeConnection()
    assert stream.TerminalSession("s6", "m", "view", Broken(), connection).run() == "OSError"
    assert connection.sent[-1] == {"t": "x", "reason": "OSError"}


@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        ({"session_id": "t1", "mission_id": "m", "mode": "write", "cols": 80, "rows": 24}, True),
        ({"session_id": "t1", "mission_id": "m", "mode": "admin", "cols": 80, "rows": 24}, False),
        ({"session_id": "../t", "mission_id": "m", "mode": "view", "cols": 80, "rows": 24}, False),
        ({"session_id": "t1", "mission_id": "m", "mode": "view", "cols": 1001, "rows": 24}, False),
    ],
)
def test_terminal_open_validation(payload, ok):
    text = Message("terminal.open", "c", 1, payload).encode()
    if ok:
        assert decode(text).payload == payload
    else:
        with pytest.raises(ProtocolError):
            decode(text)
    with pytest.raises(ProtocolError):
        decode(Message("terminal.status", "w", 1, {"session_id": "t", "state": "weird", "mode": "view", "reason": ""}).encode())


@pytest.fixture
def enrolled(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    config = worker.enroll("ws", "wss://c:1", "t" * 40, ca_pem=None, allow_control=True)
    return tmp_path, mission_id, config


def _worker_session(tmp_path, config, launcher):
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(tmp_path / "state" / "bridge.sqlite3"), software_version="t",
                                   save=lambda value: None, terminal_launcher=launcher)
    session.handle(Message("welcome", "c", 1, {"generation": 1, "protocol_version": 1, "event_cursor": 0}))
    return session


def _open(session, mission_id, session_id="t1", mode="write"):
    return session.handle(Message("terminal.open", "c", 1, {"session_id": session_id, "mission_id": mission_id, "mode": mode, "cols": 80, "rows": 24}))


def _statuses(session, count):
    found = []
    wait_for(lambda: (found.extend(_drain(session)) or len(found) >= count), timeout=scaled(10), interval=0.02, message="no terminal status")
    return [message.payload for message in found]


def _drain(session):
    items = []
    while not session.outbox.empty():
        items.append(session.outbox.get_nowait())
    return items


def test_worker_routes_terminal_open_and_close(enrolled):
    tmp_path, mission_id, config = enrolled
    no_terminal = _worker_session(tmp_path, config, None)
    assert _open(no_terminal, mission_id)[0].payload["state"] == "refused"
    connections = []

    class Blocking(FakeConnection):
        def receive_text(self, timeout=None):
            return None

    def source_factory(mission):
        return FakeSource([b""] * 100000)

    launcher = worker.TerminalLauncher(lambda cfg, sid: connections.append(Blocking()) or connections[-1], source_factory)
    session = _worker_session(tmp_path, config, launcher)
    assert _open(session, "f" * 32)[0].payload["reason"] == "unknown mission"
    assert _open(session, mission_id) == []
    assert _statuses(session, 1)[0]["state"] == "opened"
    _open(session, mission_id)  # duplicate session id while running
    assert _statuses(session, 1)[0]["state"] == "refused"
    session.handle(Message("terminal.close", "c", 1, {"session_id": "t1"}))
    closed = _statuses(session, 1)[0]
    assert closed["state"] == "closed"

    def failing_source(mission):
        raise RuntimeError("no live terminal station")

    session = _worker_session(tmp_path, config, worker.TerminalLauncher(lambda cfg, sid: FakeConnection(), failing_source))
    _open(session, mission_id, "t2")
    assert "no live terminal station" in _statuses(session, 1)[0]["reason"]

    def refuse_connect(cfg, sid):
        raise OSError("refused")

    fake_source = FakeSource()
    session = _worker_session(tmp_path, config, worker.TerminalLauncher(refuse_connect, lambda mission: fake_source))
    _open(session, mission_id, "t3")
    assert "terminal connection failed" in _statuses(session, 1)[0]["reason"] and fake_source.closed


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_real_station_streams_output_and_types_exact_bytes(enrolled):
    tmp_path, mission_id, _ = enrolled
    backend = TmuxTerminalBackend()
    provider = [sys.executable, "-c", "import sys\nprint('ready', flush=True)\nfor line in sys.stdin: print('echo:' + line.strip(), flush=True)"]
    handle = backend.start(provider_argv=provider, bridge_argv=[sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path,
                           socket_path=mission_socket_path(mission_id), session_name=f"mission-{mission_id}")
    source = None
    try:
        wait_for(lambda: "ready" in backend.capture(handle, pane="provider"), timeout=scaled(10), interval=0.05, message="provider did not start")
        source = stream.TmuxPaneSource(mission_id)
        assert b"ready" in source.snapshot()
        assert source.size()[0] > 0 and source.local_clients() == 0
        source.send_input(b"hello \\ world\r")
        seen = bytearray()

        def echoed():
            chunk = source.read_output(0.1)
            seen.extend(chunk or b"")
            return b"echo:hello \\ world" in seen

        wait_for(echoed, timeout=scaled(10), interval=0.01, message=f"no echo in {bytes(seen)!r}")
        assert source.take_resync() is False
        source.resize(100, 30)
        assert source.size()[0] == 100
    finally:
        if source is not None:
            source.close()
        backend.stop(handle)
