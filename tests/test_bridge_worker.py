"""Worker service: enrolment, protocol session, reconnect loop and CLI."""

from __future__ import annotations

import dataclasses
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


def test_worker_status_reports_connection_and_service_state_without_token(monkeypatch, capsys):
    config = worker.WorkerConfig("worker-id", "build-1", "wss://coordinator.example/api/v1/worker/connect", TOKEN, False)
    monkeypatch.setattr(worker, "load_config", lambda _env=None: config)
    monkeypatch.setattr(worker, "read_audit", lambda _limit=50, _env=None: [
        {"ts": "2026-11-06T14:00:00Z", "action": "connection.accepted", "generation": 2},
    ])
    status = worker.status_public(service_state="active")
    assert status["connection_state"] == "connected"
    assert status["last_connected_at"] == "2026-11-06T14:00:00Z"
    assert status["service_state"] == "active"
    assert "token" not in status and TOKEN not in json.dumps(status)
    monkeypatch.setattr(worker, "status_public", lambda: status)
    assert main(["worker", "status"]) == 0
    output = capsys.readouterr().out
    assert "Worker build-1 · connected to the coordinator" in output
    assert "Worker service: active" in output


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


@pytest.mark.parametrize(
    ("cert", "key", "message"),
    [
        ("-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n", None, "supplied together"),
        ("not a certificate", "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n", "not a PEM certificate"),
        ("-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n", "not a private key", "not a PEM private key"),
    ],
)
def test_worker_enrollment_rejects_incomplete_or_malformed_mtls_identity(state, cert, key, message):
    with pytest.raises(worker.WorkerConfigError, match=message):
        worker.enroll("ws", "wss://c", TOKEN, ca_pem=None, allow_control=False,
                      client_cert_pem=cert, client_key_pem=key)


def test_worker_tls_context_rejects_missing_enrolled_client_identity(state):
    directory, _ = state
    cert = "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
    key = "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n"
    worker.enroll("ws", "wss://c", TOKEN, ca_pem=None, allow_control=False,
                  client_cert_pem=cert, client_key_pem=key)
    (directory / "worker" / worker.CLIENT_KEY_FILE).unlink()
    with pytest.raises(worker.WorkerConfigError, match="client certificate or key is missing"):
        worker.tls_context()


def test_worker_tls_context_loads_the_enrolled_client_identity(state, monkeypatch):
    directory, _ = state
    cert = "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
    key = "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n"
    worker.enroll("ws", "wss://c", TOKEN, ca_pem=None, allow_control=False,
                  client_cert_pem=cert, client_key_pem=key)

    class TlsContext:
        minimum_version = None
        loaded_identity = None

        def load_cert_chain(self, *, certfile, keyfile):
            self.loaded_identity = (certfile, keyfile)

    context = TlsContext()
    monkeypatch.setattr(worker.ssl, "create_default_context", lambda cafile=None: context)

    assert worker.tls_context() is context
    assert context.minimum_version == worker.ssl.TLSVersion.TLSv1_2
    assert context.loaded_identity == (
        str(directory / "worker" / worker.CLIENT_CERT_FILE),
        str(directory / "worker" / worker.CLIENT_KEY_FILE),
    )


def test_worker_token_rotation_preserves_opt_ins_and_client_identity(state, monkeypatch):
    directory, _ = state
    cert = "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
    key = "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n"
    enrolled = worker.enroll("ws", "wss://c", TOKEN, ca_pem=None, allow_control=True,
                             allow_create=True, allow_roots=[str(directory)], allow_power=True,
                             allow_restore=True, client_cert_pem=cert, client_key_pem=key)
    rotated = worker.rotate_token("n" * 40)
    assert rotated.token == "n" * 40 and rotated.worker_id == enrolled.worker_id
    assert rotated.client_certificate and rotated.allow_control and rotated.allow_create
    assert rotated.allow_power and rotated.allow_restore and rotated.allow_roots == enrolled.allow_roots
    assert stat.S_IMODE((directory / "worker" / worker.CLIENT_CERT_FILE).stat().st_mode) == 0o600
    assert stat.S_IMODE((directory / "worker" / worker.CLIENT_KEY_FILE).stat().st_mode) == 0o600
    assert "token" not in rotated.public()
    assert TOKEN not in json.dumps(rotated.public())
    with pytest.raises(worker.WorkerConfigError, match="32-256 URL-safe"):
        worker.rotate_token("contains spaces")


def test_worker_opt_ins_default_off_and_roots_are_canonical(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    root = tmp_path / "workspace"
    root.mkdir()
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(root, target_is_directory=True)
    config = worker.enroll("ws", "wss://c", TOKEN, ca_pem=None, allow_control=True, allow_roots=[str(alias), str(root)])
    assert config.allow_create is False and config.allow_power is False and config.allow_restore is False
    assert config.allow_roots == (str(root.resolve()),)
    assert worker.load_config().allow_roots == (str(root.resolve()),)
    assert config.public()["allow_root"] == [str(root.resolve())]
    with pytest.raises(worker.WorkerConfigError, match="cannot be resolved"):
        worker.enroll("ws", "wss://c", TOKEN, ca_pem=None, allow_control=True, allow_roots=[str(tmp_path / "missing")])
    with pytest.raises(worker.WorkerConfigError, match="requires at least one"):
        worker.enroll("ws", "wss://c", TOKEN, ca_pem=None, allow_control=True, allow_create=True)


def test_hello_reports_inventory_and_only_permitted_operations(state, monkeypatch):
    directory, mission_id = state
    session, _ = _session(directory, allow_control=False)
    monkeypatch.setattr(worker.platform, "node", lambda: "test-host.example")
    hello = decode(session.hello().encode())
    assert hello.payload["missions"][0]["id"] == mission_id
    summary = hello.payload["missions"][0]
    assert summary["task_counts"]["QUEUED"] == 1
    assert summary["station"] == "unknown" and summary["model"] == "unknown"
    assert "cwd" not in summary and "config" not in summary
    assert hello.payload["host_name"] == "test-host.example"
    assert hello.payload["event_cursor"] > 0
    operations = {spec["operation"] for spec in hello.payload["capabilities"]}
    assert "mission.status" in operations and not any(spec["mutating"] for spec in hello.payload["capabilities"])
    assert "token" not in session.hello().encode()


def test_hello_caps_optional_needs_you_summary_fields(state, monkeypatch):
    directory, _ = state
    monkeypatch.setattr("llm_tools.bridge_monitor.project_snapshot", lambda *_a, **_k: {
        "task_counts": {"QUEUED": 1},
        "current_task": {"ordinal": 7, "title": "t" * 300},
        "station_context": {"last_launched_station": "codex"},
        "station_observation": {"station": None, "model": "m" * 200},
        "needs_you": {
            "kind": "approval", "message": "m" * 5000, "command": "c" * 2000,
            "tasks": list(range(1, 1001)), "hold": True,
        },
    })
    session, _ = _session(directory, allow_control=False)
    summary = decode(session.hello().encode()).payload["missions"][0]
    assert summary["current_task"] == {"ordinal": 7, "title": "t" * 256}
    assert summary["station"] == "codex" and summary["model"] == "m" * 128
    assert len(summary["needs_you"]["message"]) == 2048
    assert len(summary["needs_you"]["command"]) == 512
    assert len(summary["needs_you"]["tasks"]) == 32
    assert summary["needs_you"]["hold"] is True


def test_mission_summary_falls_back_when_projection_fails(state, monkeypatch):
    directory, mission_id = state

    def fail_projection(*_args, **_kwargs):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr("llm_tools.bridge_monitor.project_snapshot", fail_projection)
    session, _ = _session(directory, allow_control=False)
    summary = decode(session.hello().encode()).payload["missions"][0]
    assert summary["id"] == mission_id
    assert summary["task_counts"] == {"QUEUED": 1}


@pytest.mark.parametrize(("hostname", "expected"), [("Pi-Lab.Example", "pi-lab"), ("host_name", "host-name"), ("!!!", "worker")])
def test_default_worker_name_uses_short_safe_hostname(monkeypatch, hostname, expected):
    monkeypatch.setattr(worker.platform, "node", lambda: hostname)
    assert worker.default_worker_name() == expected


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


def test_repeated_redelivery_on_one_connection_applies_a_task_add_once(state):
    """FIND-003: the coordinator re-sends an unanswered command every few seconds on the same connection.

    Every delivery must be acknowledged, must return the first outcome and must not add a second task."""
    directory, mission_id = state
    session, _ = _session(directory)
    _welcome(session)
    with BridgeStore(directory / "bridge.sqlite3") as store:
        before = store.db.execute("SELECT COUNT(*) FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0]
    request = CommandRequest("task.add", mission_id=mission_id, params={"title": "once", "prompt": "do it once"}, request_id="remote-add-1")
    ack, first = _command(session, request)
    assert (ack.payload["status"], first.payload["outcome"]) == ("received", "applied")
    for _ in range(3):
        ack, again = _command(session, request)
        assert ack.payload["status"] == "duplicate"
        assert again.payload == first.payload
    with BridgeStore(directory / "bridge.sqlite3") as store:
        assert store.db.execute("SELECT COUNT(*) FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0] == before + 1


def test_read_only_worker_refuses_mutations(state):
    directory, mission_id = state
    session, _ = _session(directory, allow_control=False)
    _welcome(session)
    _, result = _command(session, CommandRequest("mission.cancel", mission_id=mission_id))
    assert (result.payload["outcome"], result.payload["error"]["code"]) == ("rejected", "forbidden")
    with BridgeStore(directory / "bridge.sqlite3") as store:
        assert store.get_mission(mission_id)["status"] == "CREATED"


def test_admin_commands_cannot_bypass_worker_opt_ins(state, tmp_path):
    directory, _ = state
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_escape = allowed / "escape"
    try:
        symlink_escape.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        symlink_escape = None
    disabled = worker.enroll("ws-1", "wss://coord.home.arpa:8443", TOKEN, ca_pem=None, allow_control=True,
                             allow_roots=[str(allowed)])
    disabled_session = worker.WorkerSession(disabled, open_store=lambda: BridgeStore(directory / "bridge.sqlite3"), software_version="9.9")
    _welcome(disabled_session)
    _, denied_create = _command(disabled_session, CommandRequest("mission.create", {"cwd": str(allowed)}))
    assert denied_create.payload["outcome"] == "rejected"
    assert denied_create.payload["error"]["code"] == "forbidden"

    config = worker.enroll("ws-1", "wss://coord.home.arpa:8443", TOKEN, ca_pem=None, allow_control=True,
                           allow_create=True, allow_roots=[str(allowed)])
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(directory / "bridge.sqlite3"), software_version="9.9")
    _welcome(session, generation=2)
    requests = [
        CommandRequest("mission.create", {"cwd": str(outside)}),
        CommandRequest("machine.poweroff"),
        CommandRequest("worker.restore", {"input": str(allowed / "backup.db")}),
    ]
    if symlink_escape is not None:
        requests.append(CommandRequest("mission.create", {"cwd": str(symlink_escape), "trust_folder": True}))
    for request in requests:
        _, result = _command(session, request)
        assert result.payload["outcome"] == "rejected"
        assert result.payload["error"]["code"] == "forbidden"


def test_allowed_root_recheck_and_worker_status_expose_opt_ins(state, tmp_path, monkeypatch):
    directory, _ = state
    root = tmp_path / "root"
    root.mkdir()
    config = worker.enroll("ws-1", "wss://coord.home.arpa:8443", TOKEN, ca_pem=None, allow_control=False,
                           allow_create=True, allow_roots=[str(root)], allow_power=True, allow_restore=True)
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(directory / "bridge.sqlite3"), software_version="9.9")
    request = CommandRequest("mission.create", {"cwd": str(root)})
    assert session._policy_error(request, None) is None
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = root / "escape"
    try:
        escape.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        escape = None
    if escape is not None:
        assert session._policy_error(CommandRequest("mission.create", {"cwd": str(escape)}), None) is not None
    moved = tmp_path / "moved"
    moved.mkdir()
    root.rename(tmp_path / "old-root")
    try:
        root.symlink_to(moved, target_is_directory=True)
    except (NotImplementedError, OSError):
        pass
    else:
        assert session._policy_error(request, None) is not None
    status = worker.status_public(service_state="inactive")
    assert status["allow_create"] and status["allow_power"] and status["allow_restore"]
    assert status["allow_root"] == [str(root)]


def test_worker_typed_create_power_and_restore_require_local_opt_ins(state, tmp_path, monkeypatch):
    directory, _ = state
    root = tmp_path / "approved"
    root.mkdir()
    (root / "backup.db").write_bytes(b"placeholder")
    config = worker.enroll("ws-1", "wss://coord.home.arpa:8443", TOKEN, ca_pem=None, allow_control=True, allow_roots=[str(root)])
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(directory / "bridge.sqlite3"), software_version="9.9")
    hello = session.hello()
    operations = {item["operation"] for item in hello.payload["capabilities"]}
    assert "mission.create" not in operations and "mission.suspend_until_credits" not in operations and "worker.restore" not in operations
    _welcome(session)
    _, denied = _command(session, CommandRequest("mission.create", {"prompt": "new work", "cwd": str(root)}))
    assert denied.payload["error"]["code"] == "forbidden"
    _, denied = _command(session, CommandRequest("mission.suspend_until_credits", mission_id="x"))
    assert denied.payload["error"]["code"] == "forbidden"
    _, denied = _command(session, CommandRequest("worker.restore", {"input": str(root / "backup.db")}))
    assert denied.payload["error"]["code"] == "forbidden"

    config = worker.enroll("ws-1", "wss://coord.home.arpa:8443", TOKEN, ca_pem=None, allow_control=False,
                           allow_create=True, allow_roots=[str(root)], allow_power=True, allow_restore=True)
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(directory / "bridge.sqlite3"), software_version="9.9")
    operations = {item["operation"] for item in session.hello().payload["capabilities"]}
    assert {"mission.create", "mission.suspend_until_credits", "worker.restore"} <= operations
    _welcome(session, generation=2)
    _, created = _command(session, CommandRequest("mission.create", {"prompt": "new work", "cwd": str(root), "providers": "codex", "trust_folder": True}))
    assert created.payload["outcome"] == "applied"
    assert created.payload["result"]["mission"]["cwd"] == str(root)
    assert created.payload["result"]["mission"]["config"]["trust_folder"] is True

    _, unsupported = _command(session, CommandRequest("mission.create", {"prompt": "new work", "cwd": str(root), "verifier_argv": ["true"]}))
    assert unsupported.payload["error"]["code"] == "unsupported-remote-option"

    power_calls = []
    monkeypatch.setattr("llm_tools.bridge_power.suspend_until_credits", lambda mission_id: power_calls.append(mission_id) or {"ok": True})
    _, suspended = _command(session, CommandRequest("mission.suspend_until_credits", mission_id=created.payload["result"]["mission"]["id"]))
    assert suspended.payload["outcome"] == "applied" and power_calls

    restore_calls = []
    restore_details = {
        "restored_from": str(root / "backup.db"),
        "previous_database": str(directory / "bridge.sqlite3.before-restore-secret"),
        "missions": 1,
        "schema_version": 10,
        "last_event_id": 42,
        "event_sequence": 43,
    }
    monkeypatch.setattr("llm_tools.bridge_backup.restore", lambda source, **kwargs: restore_calls.append(str(source)) or restore_details)
    _, restored = _command(session, CommandRequest("worker.restore", {"input": str(root / "backup.db")}))
    assert restored.payload["outcome"] == "applied" and restore_calls == [str(root / "backup.db")]
    assert restored.payload["result"] == {"missions": 1, "schema_version": 10, "last_event_id": 42, "event_sequence": 43}
    serialized_result = json.dumps(restored.payload["result"])
    assert str(root) not in serialized_result and str(directory) not in serialized_result
    _, escaped = _command(session, CommandRequest("worker.restore", {"input": str(tmp_path / "outside.db")}))
    assert escaped.payload["error"]["code"] == "forbidden"


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
    assert session.next_events() is None  # one batch in flight
    session.handle(Message("events.nack", "c", 1, {"expected_after": 0}))  # names the batch's own start: stale
    assert session.next_events() is None and session.inflight is not None
    session.barrier_ping()
    session.handle(Message("pong", "c", 1, {}))
    assert session.cursor == through and session.inflight is None


def test_event_batch_carries_current_mission_inventory_after_hello(state, tmp_path):
    directory, first_id = state
    session, _ = _session(directory)
    hello = session.hello()
    _welcome(session, cursor=hello.payload["event_cursor"])
    assert hello.payload["missions"][0]["id"] == first_id

    # The mission is created after hello while this same worker session stays online.
    second_cwd = tmp_path / "second"
    second_cwd.mkdir()
    assert main(["mission", "create", "--prompt", "Second objective", "--cwd", str(second_cwd), "--providers", "codex", "--json"]) == 0
    batch = session.next_events()
    assert batch is not None
    summaries = {mission["id"]: mission for mission in batch.payload["missions"]}
    assert first_id not in summaries and len(summaries) == 1
    assert batch.payload["mission_cursor"] == batch.payload["through"]
    assert all(set(mission) == {"id", "name", "status", "folder", "updated_at", "task_counts", "current_task", "station", "model", "needs_kind", "needs_tasks"} for mission in summaries.values())
    assert all(mission["task_counts"] == {"QUEUED": 1} for mission in summaries.values())
    assert all({"id", "name", "status", "folder", "updated_at", "task_counts", "station", "model"} <= set(mission) for mission in summaries.values())


def test_worker_replicates_new_provider_question_without_a_database_event(state, monkeypatch):
    directory, mission_id = state
    with BridgeStore(directory / "bridge.sqlite3") as store:
        mission = store.get_mission(mission_id)
        config = {**mission["config"], "native_tui": True}
        store.db.execute("UPDATE missions SET status='RUNNING',config_json=? WHERE id=?", (json.dumps(config), mission_id))
    question = {"kind": "provider", "command": f"llm-bridge attach --mission {mission_id[:8]}",
                "message": "Codex asks: Should I reconnect? · Choices: 1. Yes; 2. No · take control of the terminal to answer"}
    observed = {"needs": None}
    monkeypatch.setattr("llm_tools.bridge_monitor.project_snapshot", lambda *_args, **_kwargs: {"needs_you": observed["needs"]})
    session, _ = _session(directory)
    hello = session.hello()
    _welcome(session, cursor=hello.payload["event_cursor"])
    assert hello.payload["missions"][0]["needs_you"] is None

    # request_user_input can be pending without a Bridge SQLite event.
    observed["needs"] = question
    session.last_question_refresh = 0.0
    batch = session.next_events()

    assert batch is not None
    assert batch.payload["events"] == []
    assert batch.payload["after"] == batch.payload["through"]
    assert len(batch.payload["missions"]) == 1
    summary = batch.payload["missions"][0]
    assert summary["id"] == mission_id and summary["status"] == "RUNNING"
    assert summary["needs_you"] == question
    decode(batch.encode())


def test_periodic_question_scan_rotates_beyond_its_bounded_batch(monkeypatch):
    monkeypatch.setattr(worker, "MAX_QUESTION_MISSIONS_PER_REFRESH", 3)
    session = object.__new__(worker.WorkerSession)
    session.last_question_mission_id = None
    rows = [{"id": f"mission-{index:02d}"} for index in range(7)]

    first = session._question_refresh_batch(rows)
    second = session._question_refresh_batch(rows)
    third = session._question_refresh_batch(rows)

    assert [row["id"] for row in first] == ["mission-00", "mission-01", "mission-02"]
    assert [row["id"] for row in second] == ["mission-03", "mission-04", "mission-05"]
    assert [row["id"] for row in third] == ["mission-06", "mission-00", "mission-01"]


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
    monkeypatch.setattr(worker.platform, "node", lambda: "Test-Host.Example")
    assert main(["worker", "status"]) == 2
    assert "not enrolled" in capsys.readouterr().err
    assert main(["worker", "enroll", "--coordinator", "wss://c:8443", "--token-file", str(token_file)]) == 0
    enrolled = capsys.readouterr().out
    assert "Enrolled worker test-host" in enrolled and "remote control read-only" in enrolled
    assert main(["worker", "status", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["name"] == "test-host" and TOKEN not in out
    assert main(["worker", "status"]) == 0 and "Remote access: read-only" in capsys.readouterr().out
    started = []
    monkeypatch.setattr(worker, "run", lambda session, connect, stop, log: started.append(session.config.name))
    assert main(["worker", "run"]) == 0 and started == ["test-host"]
    capsys.readouterr()
    assert main(["worker", "unenroll"]) == 0 and "removed" in capsys.readouterr().out
    assert main(["worker", "unenroll"]) == 0 and "not enrolled" in capsys.readouterr().out


def test_worker_cli_enrollment_opt_in_flags_and_status(state, tmp_path, capsys):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    root1, root2 = tmp_path / "one", tmp_path / "two"
    root1.mkdir()
    root2.mkdir()
    assert main(["worker", "enroll", "--name", "ws", "--coordinator", "wss://c:1", "--token-file", str(token_file),
                 "--allow-create", "--allow-root", str(root1), "--allow-root", str(root2), "--allow-power", "--allow-restore", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["allow_create"] and output["allow_power"] and output["allow_restore"]
    assert output["allow_root"] == [str(root1.resolve()), str(root2.resolve())]
    assert main(["worker", "status"]) == 0
    rendered = capsys.readouterr().out
    assert "Remote creation: allowed" in rendered and str(root1.resolve()) in rendered and "Remote power: allowed" in rendered


def test_worker_cli_installs_a_staged_token_without_reenrolling(state, tmp_path, capsys):
    directory, _ = state
    worker.enroll("ws", "wss://c", TOKEN, ca_pem=None, allow_control=True)
    staged = tmp_path / "staged-token"
    staged.write_text("n" * 40 + "\n")

    assert main(["worker", "rotate-token", "--token-file", str(staged)]) == 0

    updated = worker.load_config()
    assert updated.token == "n" * 40
    assert updated.worker_id
    assert "Worker token updated for ws" in capsys.readouterr().out


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


def test_errors_are_never_answered_and_stale_welcome_closes_the_connection(state):
    """Review M3: a stale welcome must not leave a connected but unusable worker exchanging errors."""
    directory, _ = state
    session, _ = _session(directory)
    error = Message("error", "c", 0, {"code": "stale-generation", "message": "x"})
    assert session.handle(error) == []  # before welcome
    _welcome(session, generation=4)
    assert session.handle(error) == []  # after welcome
    stale, _ = _session(directory)
    assert _welcome(stale, generation=2)[0].payload["code"] == "stale-generation" and stale.close_reason
    stop = threading.Event()
    connections = []

    class Link:
        def __init__(self, generation):
            self.sent, self.closed = [], False
            self.inbox = [Message("welcome", "c", generation, {"generation": generation, "protocol_version": 1, "event_cursor": 0}).encode()]

        def send_text(self, text):
            self.sent.append(decode(text))

        def receive_text(self, timeout=None):
            if self.inbox:
                return self.inbox.pop(0)
            stop.set()
            return None

        def close(self):
            self.closed = True

    def connect(config):
        connections.append(Link(3 if not connections else 9))
        return connections[-1]

    reconnecting, _ = _session(directory)
    worker.run(reconnecting, connect, stop=stop, idle=0.01, max_backoff=0.01)
    first, second = connections
    assert first.closed and [m.type for m in first.sent] == ["hello", "error"]
    assert reconnecting.config.max_generation == 9 and second.sent[0].type == "hello"
    assert "connection.refused" in [entry["action"] for entry in worker.read_audit(50)]


def test_a_new_token_resets_the_generation_high_water_mark(state):
    first = worker.enroll("ws-1", "wss://c:1", TOKEN, ca_pem=None, allow_control=False)
    worker._save(dataclasses.replace(first, max_generation=7))
    assert worker.enroll("ws-1", "wss://c:1", TOKEN, ca_pem=None, allow_control=False).max_generation == 7
    renewed = worker.enroll("ws-1", "wss://c:1", "u" * 40, ca_pem=None, allow_control=False)
    assert (renewed.max_generation, renewed.worker_id) == (0, first.worker_id)


def test_read_only_worker_shares_terminals_only_with_the_view_opt_in(state, tmp_path, capsys):
    directory, mission_id = state
    opened = []

    class Launcher:
        def open(self, session, session_id, mission, mode, *, allow_write):
            opened.append((session_id, mode, allow_write))

    def open_terminal(config, session_id):
        session = worker.WorkerSession(config, open_store=lambda: BridgeStore(directory / "bridge.sqlite3"), software_version="t",
                                       save=lambda value: None, terminal_launcher=Launcher())
        _welcome(session)
        return session.handle(Message("terminal.open", "c", 1, {"session_id": session_id, "mission_id": mission_id, "mode": "view", "cols": 80, "rows": 24}))

    plain = worker.enroll("ws-1", "wss://c:1", TOKEN, ca_pem=None, allow_control=False)
    refusal = open_terminal(plain, "t1")[0].payload["reason"]
    assert "--allow-terminal-view" in refusal and len(refusal.encode("utf-8")) <= 64 and opened == []
    viewer = worker.enroll("ws-1", "wss://c:1", TOKEN, ca_pem=None, allow_control=False, allow_terminal_view=True)
    assert open_terminal(viewer, "t2") == [] and opened == [("t2", "view", False)]
    assert worker.load_config().public()["allow_terminal_view"] is True
    token = tmp_path / "token"
    token.write_text(TOKEN)
    assert main(["worker", "enroll", "--name", "ws", "--coordinator", "wss://c:1", "--token-file", str(token), "--allow-terminal-view", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["allow_terminal_view"] is True


def test_hello_offers_every_supported_version_and_a_welcome_must_pick_one_of_them(state, monkeypatch):
    from llm_tools import bridge_protocol

    directory, _ = state
    monkeypatch.setattr(worker, "SUPPORTED_VERSIONS", (1, 2))
    monkeypatch.setattr(bridge_protocol, "SUPPORTED_VERSIONS", (1, 2))
    session, _saved = _session(directory)
    hello = decode(session.hello().encode())
    assert hello.payload["protocol_versions"] == [1, 2]
    # A coordinator one version newer or older than the worker's newest answers with a version the worker offered.
    for selected in (1, 2):
        assert decode(Message("welcome", "c", 1, {"generation": 1, "protocol_version": selected, "event_cursor": 0}).encode()).payload["protocol_version"] == selected
    with pytest.raises(bridge_protocol.ProtocolError, match="protocol version 3 is not supported") as caught:
        decode(Message("welcome", "c", 1, {"generation": 1, "protocol_version": 3, "event_cursor": 0}).encode())
    assert caught.value.code == "unsupported-version"


def _session_with_root(tmp_path, directory):
    root = tmp_path / "approved"
    root.mkdir(exist_ok=True)
    config = worker.enroll("ws-1", "wss://coord.home.arpa:8443", TOKEN, ca_pem=None, allow_control=True,
                           allow_create=True, allow_roots=[str(root)])
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(directory / "bridge.sqlite3"), software_version="9.9")
    session.hello()
    _welcome(session)
    return session, root


def _write_config(tmp_path, monkeypatch, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(path))


def test_remote_create_resolves_a_verifier_declared_in_the_worker_config(state, tmp_path, monkeypatch):
    directory, _ = state
    session, root = _session_with_root(tmp_path, directory)
    (root / "check.sh").write_text("#!/bin/sh\nexit 0\n")
    _write_config(tmp_path, monkeypatch, '[bridge.verifiers.unit]\nargv = ["sh", "check.sh"]\nfiles = ["check.sh"]\ntimeout_seconds = 90\n')
    _, created = _command(session, CommandRequest("mission.create", {"prompt": "new work", "cwd": str(root), "providers": "codex", "verifier_name": "unit"}))
    assert created.payload["outcome"] == "applied", created.payload
    mission_config = created.payload["result"]["mission"]["config"]
    verifier = mission_config["verifier"]
    assert verifier["argv"] == ["sh", "check.sh"] and verifier["timeout_seconds"] == 90.0
    assert "check.sh" in verifier["fingerprints"]  # the verifier guard hashes the declared files as for a local mission
    assert "frozen_tool_config" not in mission_config
    _, overridden = _command(session, CommandRequest("mission.create", {"prompt": "again", "cwd": str(root), "providers": "codex", "verifier_name": "unit", "verifier_timeout": 30}))
    assert overridden.payload["result"]["mission"]["config"]["verifier"]["timeout_seconds"] == 30.0


def test_remote_create_refuses_unknown_and_malformed_verifier_names(state, tmp_path, monkeypatch):
    directory, _ = state
    session, root = _session_with_root(tmp_path, directory)
    _write_config(tmp_path, monkeypatch, '[bridge.verifiers.unit]\nargv = ["true"]\n')
    for name in ("missing", "../etc/passwd", "Bad Name"):
        _, refused = _command(session, CommandRequest("mission.create", {"prompt": "x", "cwd": str(root), "providers": "codex", "verifier_name": name}))
        assert refused.payload["error"]["code"] == "unknown-verifier", name
    _, too_long = _command(session, CommandRequest("mission.create", {"prompt": "x", "cwd": str(root), "providers": "codex", "verifier_name": "unit", "verifier_timeout": 99999}))
    assert too_long.payload["error"]["code"] == "invalid-argument"


@pytest.mark.parametrize("option", ["command_template", "verifier_argv", "verifier_files", "verifier", "verifier_command"])
def test_remote_create_still_refuses_commands_and_files(state, tmp_path, option):
    directory, _ = state
    session, root = _session_with_root(tmp_path, directory)
    value = ["true"] if option.endswith(("argv", "files")) else "true"
    _, refused = _command(session, CommandRequest("mission.create", {"prompt": "x", "cwd": str(root), option: value}))
    assert refused.payload["error"]["code"] == "unsupported-remote-option"


def test_remote_create_accepts_the_retention_and_continue_options(state, tmp_path):
    directory, _ = state
    session, root = _session_with_root(tmp_path, directory)
    _, created = _command(session, CommandRequest("mission.create", {
        "prompt": "x", "cwd": str(root), "providers": "codex", "auto_continue": True, "include_config": True,
        "log_max_bytes": 2048, "log_rotations": 1, "log_retained_runs": 5, "capacity_refresh_seconds": 30}))
    config = created.payload["result"]["mission"]["config"]
    assert (config["auto_continue"], config["log_max_bytes"], config["log_rotations"], config["log_retained_runs"], config["capacity_refresh_seconds"]) == (True, 2048, 1, 5, 30)
    assert "frozen_tool_config" in config
    _, bad = _command(session, CommandRequest("mission.create", {"prompt": "x", "cwd": str(root), "providers": "codex", "log_rotations": 99}))
    assert bad.payload["error"]["code"] == "invalid-argument"


def test_worker_config_validates_verifier_declarations(tmp_path, monkeypatch):
    from llm_tools import config as toolconfig

    for text in ('[bridge.verifiers.Bad]\nargv = ["x"]\n', '[bridge.verifiers.ok]\nargv = []\n', '[bridge.verifiers.ok]\nargv = ["x"]\nrun = 1\n',
                 '[bridge.verifiers.ok]\nargv = ["x"]\ntimeout_seconds = 0\n', '[bridge.verifiers.ok]\nargv = ["x"]\nfiles = [1]\n'):
        _write_config(tmp_path, monkeypatch, text)
        with pytest.raises(SystemExit):
            toolconfig.load_config()


def test_an_explicit_verifier_timeout_wins_even_when_it_equals_the_default(state, tmp_path, monkeypatch):
    directory, _ = state
    session, root = _session_with_root(tmp_path, directory)
    _write_config(tmp_path, monkeypatch, '[bridge.verifiers.unit]\nargv = ["true"]\ntimeout_seconds = 90\n')
    _, created = _command(session, CommandRequest("mission.create", {"prompt": "x", "cwd": str(root), "providers": "codex", "verifier_name": "unit", "verifier_timeout": 300}))
    assert created.payload["result"]["mission"]["config"]["verifier"]["timeout_seconds"] == 300.0


def _create_session(directory, root):
    config = worker.enroll("ws-1", "wss://coord.home.arpa:8443", TOKEN, ca_pem=None, allow_control=True,
                           allow_create=True, allow_roots=[str(root)])
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(directory / "bridge.sqlite3"), software_version="9.9")
    _welcome(session, generation=2)
    return session


def _mission_count(directory):
    with BridgeStore(directory / "bridge.sqlite3") as store:
        return store.db.execute("SELECT COUNT(*) FROM missions").fetchone()[0]


def test_duplicate_mission_create_returns_the_first_result_and_creates_one_mission(state, tmp_path):
    """FIND-003: the coordinator re-sends a command whose result was lost; mission.create has no id of its own to deduplicate on."""
    directory, _ = state
    session = _create_session(directory, tmp_path)
    before = _mission_count(directory)
    request = CommandRequest("mission.create", {"prompt": "new work", "cwd": str(tmp_path), "providers": "codex"}, request_id="create-1")
    _, first = _command(session, request)
    ack, again = _command(session, request)
    assert first.payload["outcome"] == "applied"
    assert ack.payload["status"] == "duplicate" and again.payload == first.payload
    assert _mission_count(directory) == before + 1


def test_duplicate_mission_delete_returns_the_first_result_not_not_found(state, tmp_path):
    directory, mission_id = state
    session = _create_session(directory, tmp_path)
    request = CommandRequest("mission.delete", {"confirm": True}, mission_id=mission_id, request_id="delete-1")
    _, first = _command(session, request)
    _, again = _command(session, request)
    assert first.payload["outcome"] == "applied", first.payload
    assert again.payload == first.payload
    other = CommandRequest("mission.delete", {"confirm": True}, mission_id=mission_id, request_id="delete-2")
    assert _command(session, other)[1].payload["outcome"] != "applied"  # a different request id really is a new attempt


@pytest.mark.parametrize("operation", ["proposal.create", "proposal.accept", "mission.finish", "worker.restore", "mission.settings.patch",
                                       "mission.rotation.put", "worker.gc", "mission.start", "mission.suspend_until_credits"])
def test_every_mutating_operation_runs_once_per_request_id(state, tmp_path, monkeypatch, operation):
    directory, mission_id = state
    session = _create_session(directory, tmp_path)
    runs = []

    def slow_execute(request):
        runs.append(request.request_id)  # stands for an operation that takes longer than the coordinator's re-send interval
        return worker.commands.CommandResult(request.request_id, request.operation, "applied", result={"run": len(runs)})

    monkeypatch.setattr(worker.commands, "execute", slow_execute)
    monkeypatch.setattr(session, "_policy_error", lambda request, spec: None)
    spec = worker.commands.OPERATIONS.get(operation)
    assert spec is not None and spec.mutating, operation
    request = CommandRequest(operation, mission_id=mission_id, request_id=f"once-{operation}")
    first = _command(session, request)[1]
    for _ in range(2):  # re-sent frames queued behind the running copy are handled after it, one at a time
        assert _command(session, request)[1].payload == first.payload
    assert runs == [f"once-{operation}"]


def test_outcome_ledger_is_private_bounded_and_pruned(tmp_path, monkeypatch):
    now = [1_000_000.0]
    ledger = worker.OutcomeLedger(tmp_path / "worker" / "outcomes.sqlite3", clock=lambda: now[0])
    monkeypatch.setattr(worker, "OUTCOMES_KEEP", 5)
    for index in range(8):
        now[0] += 1
        ledger.record(f"r{index}", {"outcome": "applied", "n": index})
    assert ledger.get("r0") is None and ledger.get("r2") is None and ledger.get("r7") == {"outcome": "applied", "n": 7}
    assert (ledger.path.stat().st_mode & 0o777) == 0o600
    now[0] += worker.OUTCOMES_KEEP_SECONDS + 1
    ledger.record("late", {"outcome": "applied"})
    assert ledger.get("r7") is None and ledger.get("late") is not None
