"""Worker service: enrolment, protocol session, reconnect loop and CLI."""

from __future__ import annotations

import json
import stat
import threading

import pytest

from llm_tools import bridge_worker as worker
from llm_tools.bridge import main
from llm_tools.bridge_commands import CommandRequest
from llm_tools.bridge_protocol import Message, decode
from llm_tools.bridge_store import BridgeStore

TOKEN = "t" * 40


@pytest.fixture
def state(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    return directory, mission_id


def _session(directory, *, allow_control=True):
    config = worker.enroll("ws-1", "wss://coord.home.arpa:8443", TOKEN, ca_pem=None, allow_control=allow_control)
    saved = []
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(directory / "bridge.sqlite3"), software_version="9.9",
                                   save=lambda value: saved.append(value) or worker._save(value))
    return session, saved


def _welcome(session, generation=1, cursor=0):
    return session.handle(Message("welcome", "coordinator", generation, {"generation": generation, "protocol_version": 1, "event_cursor": cursor}))


def _command(session, request, generation=None):
    return session.handle(Message("command", session.config.worker_id, session.generation if generation is None else generation, request.to_json()))


def test_enrolment_is_private_validated_and_hides_the_token(state, capsys):
    directory, _ = state
    for args, message in [
        (("Bad Name", "wss://c", TOKEN), "worker name"),
        (("ws", "https://c", TOKEN), "wss://"),
        (("ws", "wss://c", "short"), "worker token"),
    ]:
        with pytest.raises(worker.WorkerConfigError, match=message):
            worker.enroll(*args, ca_pem=None, allow_control=False)
    with pytest.raises(worker.WorkerConfigError, match="PEM"):
        worker.enroll("ws", "wss://c", TOKEN, ca_pem="nope", allow_control=False)
    with pytest.raises(worker.WorkerConfigError, match="not enrolled"):
        worker.load_config()
    first = worker.enroll("ws", "wss://c", TOKEN, ca_pem="-----BEGIN CERTIFICATE-----\nx\n", allow_control=False)
    second = worker.enroll("ws2", "wss://c", TOKEN, ca_pem=None, allow_control=True)
    assert first.worker_id == second.worker_id
    files = [directory / "worker" / worker.WORKER_FILE, directory / "worker" / worker.CA_FILE]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    assert stat.S_IMODE((directory / "worker").stat().st_mode) == 0o700
    assert TOKEN not in json.dumps(second.public())
    (directory / "worker" / worker.WORKER_FILE).write_text("{")
    with pytest.raises(worker.WorkerConfigError, match="unreadable"):
        worker.load_config()
    assert worker.unenroll() is True and worker.unenroll() is False


def test_hello_reports_inventory_and_only_permitted_operations(state):
    directory, mission_id = state
    session, _ = _session(directory, allow_control=False)
    hello = decode(session.hello().encode())
    assert hello.payload["missions"][0]["id"] == mission_id
    assert hello.payload["event_cursor"] > 0
    operations = {spec["operation"] for spec in hello.payload["capabilities"]}
    assert "mission.status" in operations and not any(spec["mutating"] for spec in hello.payload["capabilities"])
    assert "token" not in session.hello().encode()


def test_generation_fencing(state):
    directory, mission_id = state
    session, saved = _session(directory)
    request = CommandRequest("mission.status", mission_id=mission_id)
    assert _command(session, request, generation=1)[0].payload["code"] == "not-welcomed"
    assert _welcome(session, generation=2) == []
    assert saved[-1].max_generation == 2 and worker.load_config().max_generation == 2
    assert _command(session, request, generation=1)[0].payload["code"] == "stale-generation"
    restarted, _ = _session(directory)
    assert _welcome(restarted, generation=2)[0].payload["code"] == "stale-generation"
    assert _welcome(restarted, generation=3) == []


def test_commands_ack_then_result_and_duplicates_do_not_repeat(state):
    directory, mission_id = state
    session, _ = _session(directory)
    _welcome(session)
    ack, result = _command(session, CommandRequest("mission.status", mission_id=mission_id, target="ws-1"))
    assert ack.type == "command.ack" and result.payload["outcome"] == "ok"
    cancel = CommandRequest("mission.cancel", mission_id=mission_id, request_id="remote-cancel-1")
    ack, first = _command(session, cancel)
    assert (ack.payload["status"], first.payload["outcome"]) == ("received", "applied")
    ack, again = _command(session, cancel)
    assert ack.payload["status"] == "duplicate" and again.payload == first.payload
    with BridgeStore(directory / "bridge.sqlite3") as store:
        assert store.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1


def test_read_only_worker_refuses_mutations(state):
    directory, mission_id = state
    session, _ = _session(directory, allow_control=False)
    _welcome(session)
    _, result = _command(session, CommandRequest("mission.cancel", mission_id=mission_id))
    assert (result.payload["outcome"], result.payload["error"]["code"]) == ("rejected", "forbidden")
    with BridgeStore(directory / "bridge.sqlite3") as store:
        assert store.get_mission(mission_id)["status"] == "CREATED"


def test_control_messages_and_event_replication(state):
    directory, _ = state
    session, _ = _session(directory)
    assert session.next_events() is None  # no cursor before welcome
    _welcome(session, cursor=0)
    assert session.handle(Message("ping", "c", 1, {}))[0].type == "pong"
    assert session.handle(Message("pong", "c", 1, {})) == []
    assert session.handle(Message("error", "c", 1, {"code": "x", "message": "y"})) == []
    assert session.handle(Message("events", "c", 1, {"after": 0, "through": 0, "events": []}))[0].payload["code"] == "unsupported-message"
    batch = session.next_events()
    assert batch is not None and batch.payload["after"] == 0
    decode(batch.encode())  # a valid protocol message
    through = batch.payload["through"]
    assert session.next_events() is None
    session.handle(Message("events.nack", "c", 1, {"expected_after": 0}))
    assert session.next_events().payload["through"] == through


class FakeConnection:
    def __init__(self, incoming, stop, *, fail_after=False):
        self.incoming = list(incoming)
        self.sent = []
        self.stop = stop
        self.fail_after = fail_after
        self.closed = False

    def send_text(self, text):
        self.sent.append(decode(text))

    def receive_text(self, timeout=None):
        if self.incoming:
            return self.incoming.pop(0)
        if self.fail_after:
            raise ConnectionResetError("gone")
        self.stop.set()
        return None

    def close(self):
        self.closed = True


def test_run_reconnects_and_says_hello_on_every_connection(state, monkeypatch):
    directory, mission_id = state
    session, _ = _session(directory)
    stop = threading.Event()
    welcome = Message("welcome", "c", 1, {"generation": 1, "protocol_version": 1, "event_cursor": 0}).encode()
    welcome2 = Message("welcome", "c", 2, {"generation": 2, "protocol_version": 1, "event_cursor": 0}).encode()
    command = Message("command", session.config.worker_id, 2, CommandRequest("mission.status", mission_id=mission_id).to_json()).encode()
    connections = [FakeConnection([welcome], stop, fail_after=True), None, FakeConnection([welcome2, "not json", command], stop)]
    logs = []

    def connect(config):
        connection = connections.pop(0)
        if connection is None:
            raise OSError("refused")
        return connection

    monkeypatch.setattr(worker.random, "uniform", lambda a, b: 0.0)
    worker.run(session, connect, stop=stop, log=logs.append)
    assert connections == [] and len(logs) == 2 and "ConnectionResetError" in logs[0] and "refused" in logs[1]


def test_run_traffic_on_final_connection(state, monkeypatch):
    directory, mission_id = state
    session, _ = _session(directory)
    stop = threading.Event()
    welcome = Message("welcome", "c", 1, {"generation": 1, "protocol_version": 1, "event_cursor": 0}).encode()
    command = Message("command", session.config.worker_id, 1, CommandRequest("mission.status", mission_id=mission_id).to_json()).encode()
    connection = FakeConnection([welcome, "not json", command], stop)
    clock = iter([0.0, 0.0, 30.0, 31.0, 32.0, 33.0, 34.0, 35.0])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock, 36.0))
    worker.run(session, lambda config: connection, stop=stop)
    kinds = [message.type for message in connection.sent]
    assert kinds[0] == "hello" and "events" in kinds and "ping" in kinds
    assert [message.payload.get("code") for message in connection.sent if message.type == "error"] == ["invalid-message"]
    assert kinds.index("command.ack") < kinds.index("command.result")
    assert connection.closed


def test_run_drops_a_silent_coordinator(state, monkeypatch):
    directory, _ = state
    session, _ = _session(directory)
    stop = threading.Event()
    logs = []
    clock = iter([0.0, 100.0])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock, 200.0))

    class Silent(FakeConnection):
        def receive_text(self, timeout=None):
            return None

    worker.run(session, lambda config: Silent([], stop), stop=stop, log=lambda text: (logs.append(text), stop.set()))
    assert "coordinator silent" in logs[0]


def test_worker_cli(state, tmp_path, capsys, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    assert main(["worker", "status"]) == 2
    assert "not enrolled" in capsys.readouterr().err
    assert main(["worker", "enroll", "--name", "ws", "--coordinator", "wss://c:8443", "--token-file", str(token_file)]) == 0
    assert "remote control read-only" in capsys.readouterr().out
    assert main(["worker", "status", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["name"] == "ws" and TOKEN not in out
    assert main(["worker", "status"]) == 0 and "allow_control: False" in capsys.readouterr().out
    started = []
    monkeypatch.setattr(worker, "run", lambda session, connect, stop, log: started.append(session.config.name))
    assert main(["worker", "run"]) == 0 and started == ["ws"]
    capsys.readouterr()
    assert main(["worker", "unenroll"]) == 0 and "removed" in capsys.readouterr().out
    assert main(["worker", "unenroll"]) == 0 and "not enrolled" in capsys.readouterr().out


def test_audit_records_security_actions_without_secrets(state, capsys, monkeypatch):
    """SEC-007: enrolment, connections, mutations, refusals and terminal sessions are audited; tokens and parameters are not."""
    directory, mission_id = state
    session, _ = _session(directory, allow_control=False)
    _welcome(session, generation=3)
    _welcome(session, generation=2)  # stale
    _command(session, CommandRequest("mission.status", mission_id=mission_id))  # successful reads are not audited
    _command(session, CommandRequest("task.add", {"prompt": "SECRET-PROMPT"}, mission_id=mission_id, request_id="r-1"))
    _command(session, CommandRequest("mission.status", mission_id=mission_id), generation=1)
    session.handle(Message("terminal.open", "c", 3, {"session_id": "t1", "mission_id": mission_id, "mode": "write", "cols": 80, "rows": 24}))
    entries = worker.read_audit(100)
    actions = [entry["action"] for entry in entries]
    assert actions == ["enroll", "connection.accepted", "connection.refused", "command", "command.refused", "terminal.requested", "terminal.refused"]
    command = entries[3]
    assert (command["operation"], command["outcome"], command["error"], command["request_id"]) == ("task.add", "rejected", "forbidden", "r-1")
    text = (directory / "worker" / worker.AUDIT_FILE).read_text()
    assert TOKEN not in text and "SECRET-PROMPT" not in text
    assert stat.S_IMODE((directory / "worker" / worker.AUDIT_FILE).stat().st_mode) == 0o600
    assert main(["worker", "audit", "--limit", "2"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 and "terminal.refused" in out[1] and "reason=terminal access is not available" in out[1]
    assert main(["worker", "audit", "--json", "--limit", "0"]) == 0 and json.loads(capsys.readouterr().out) == {"entries": []}
    worker.unenroll()
    assert worker.read_audit(1)[0]["action"] == "unenroll"


def test_audit_rotates_and_survives_bad_lines_and_write_failures(state, monkeypatch, capsys):
    directory, _ = state
    monkeypatch.setattr(worker, "AUDIT_MAX_BYTES", 200)
    for index in range(10):
        worker.audit("probe", index=index, text="x \x1b" * 100)
    path = directory / "worker" / worker.AUDIT_FILE
    assert path.with_name(worker.AUDIT_FILE + ".1").exists()
    assert len(json.loads(path.read_text().splitlines()[-1])["text"]) == 200
    with path.open("a") as handle:
        handle.write("not json\n[1]\n")
    assert worker.read_audit(100)[-1]["index"] == 9
    path.unlink()
    path.with_name(worker.AUDIT_FILE + ".1").unlink()
    assert main(["worker", "audit"]) == 0 and "No worker audit entries" in capsys.readouterr().out
    monkeypatch.setattr(worker.os, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("ro")))
    worker.audit("ignored")  # must not raise


def test_run_audits_start_end_and_stop(state, monkeypatch):
    directory, _ = state
    session, _ = _session(directory)
    stop = threading.Event()

    class Once:
        def __init__(self):
            self.sent = []
            self.replies = [Message("welcome", "c", 1, {"generation": 5, "protocol_version": 1, "event_cursor": 0}).encode()]

        def send_text(self, text):
            self.sent.append(text)

        def receive_text(self, timeout=None):
            if self.replies:
                return self.replies.pop(0)
            stop.set()
            raise ConnectionResetError("gone")

        def close(self):
            pass

    worker.run(session, lambda config: Once(), stop=stop, idle=0.01)
    actions = [entry["action"] for entry in worker.read_audit(10)]
    assert actions[-4:] == ["worker.start", "connection.accepted", "connection.ended", "worker.stop"]


def test_service_unit_install_and_uninstall(state, tmp_path, capsys, monkeypatch):
    directory, _ = state
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    assert main(["worker", "install-service"]) == 2  # not enrolled: no unit that cannot start
    assert "not enrolled" in capsys.readouterr().err and not worker.service_unit_path().exists()
    worker.enroll("ws", "wss://c:1", TOKEN, ca_pem=None, allow_control=False)
    assert main(["worker", "install-service", "--print"]) == 0
    text = capsys.readouterr().out
    assert "-m llm_tools.bridge worker run" in text and f"LLM_BRIDGE_STATE_DIR={directory}" in text and TOKEN not in text
    assert "UMask=0077" in text and "Restart=on-failure" in text and "WorkingDirectory=" + str(home) in text
    calls = []
    monkeypatch.setattr(__import__("shutil"), "which", lambda name, path=None: "/usr/bin/" + name)
    unit, steps = worker.install_service(run=lambda argv: calls.append(argv) or 0)
    assert unit == home / ".config/systemd/user/llm-bridge-worker.service" and "worker run" in unit.read_text()
    assert calls[-1] == ["systemctl", "--user", "enable", "--now", "llm-bridge-worker.service"] and "enable-linger" in steps[0]
    assert "could not enable" in worker.install_service(run=lambda argv: 1)[1][0]
    assert worker.uninstall_service(run=lambda argv: calls.append(argv) or 0) and not unit.exists()
    assert calls[-2][:3] == ["systemctl", "--user", "disable"] and not worker.uninstall_service(run=lambda argv: 0)
    monkeypatch.setattr(__import__("shutil"), "which", lambda name, path=None: None)
    assert "not available" in worker.install_service()[1][0]
    actions = [entry["action"] for entry in worker.read_audit(20)]
    assert actions.count("service.install") == 3 and "service.uninstall" in actions
    monkeypatch.setattr(worker, "install_service", lambda: (unit, ["do this"]))
    assert main(["worker", "install-service"]) == 0 and "Do this." in capsys.readouterr().out
    monkeypatch.setattr(worker, "uninstall_service", lambda: False)
    assert main(["worker", "uninstall-service"]) == 0 and "No worker service" in capsys.readouterr().out
