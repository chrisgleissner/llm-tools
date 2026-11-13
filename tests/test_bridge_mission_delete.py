"""`llm-bridge mission delete` (PRD-038): states and refusals, confirmation, completeness, files and the replicated tombstone."""

from __future__ import annotations

import io
import json
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge, bridge_commands as commands, bridge_missions, bridge_worker as worker
from llm_tools.bridge import main
from llm_tools.bridge_protocol import Message, ProtocolError, decode
from llm_tools.bridge_states import DELETABLE_MISSION_STATUSES
from llm_tools.bridge_store import BridgeStore, MissionLock


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    return directory


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _run(capsys: pytest.CaptureFixture[str], *argv: str, code: int = 0) -> str:
    assert main(list(argv)) == code, capsys.readouterr()
    captured = capsys.readouterr()
    return captured.out if code == 0 else captured.err


def _create(tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str = "work") -> str:
    folder = tmp_path / name
    folder.mkdir()
    out = _run(capsys, "mission", "create", "--prompt", f"{name} objective", "--name", name, "--cwd", str(folder), "--providers", "codex", "--json")
    return str(json.loads(out)["mission"]["id"])


def _set_status(state: Path, mission_id: str, status: str) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        with store._transaction():
            store.db.execute("UPDATE missions SET status=? WHERE id=?", (status, mission_id))


def _exists(state: Path, mission_id: str) -> bool:
    with BridgeStore(state / "bridge.sqlite3", readonly=True) as store:
        return store.mission_id_if_exists(mission_id) is not None


# ------------------------------------------------------------------ CLI: states, refusals, exit statuses


@pytest.mark.parametrize("status", ["CREATED", "COMPLETED", "FAILED", "CANCELLED", "STOPPED"])
def test_a_never_started_or_finished_mission_is_deleted(tmp_path, state, capsys, status) -> None:
    mission_id = _create(tmp_path, capsys)
    _set_status(state, mission_id, status)
    out = _run(capsys, "mission", "delete", mission_id[:8], "--yes")
    assert f'Deleted mission {mission_id[:8]} "work"' in out
    assert not _exists(state, mission_id)
    assert status in DELETABLE_MISSION_STATUSES


@pytest.mark.parametrize("status", ["RUNNING", "PAUSED", "STOPPING"])
def test_a_running_paused_or_stopping_mission_is_refused_with_status_4_and_what_to_do(tmp_path, state, capsys, status) -> None:
    mission_id = _create(tmp_path, capsys)
    _set_status(state, mission_id, status)
    err = _run(capsys, "mission", "delete", mission_id, "--yes", code=4)
    assert f"is {status.lower()}" in err and "cannot be deleted" in err
    assert f"llm-bridge stop --mission {mission_id[:8]}" in err and f"llm-bridge cancel --mission {mission_id[:8]}" in err
    assert _exists(state, mission_id)
    assert status not in DELETABLE_MISSION_STATUSES


def test_a_refusal_comes_before_the_question(tmp_path, state, capsys, monkeypatch) -> None:
    mission_id = _create(tmp_path, capsys)
    _set_status(state, mission_id, "RUNNING")

    def refuse(*_args: Any) -> str:
        raise AssertionError("the command asked for confirmation of a deletion it cannot do")

    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(sys, "stderr", _Tty())
    monkeypatch.setattr("builtins.input", refuse)
    assert main(["mission", "delete", mission_id]) == 4


def test_without_a_terminal_and_without_yes_it_is_a_usage_error_and_deletes_nothing(tmp_path, state, capsys) -> None:
    mission_id = _create(tmp_path, capsys)
    err = _run(capsys, "mission", "delete", mission_id, code=2)
    assert "This cannot be undone" in err and f"llm-bridge mission delete {mission_id[:8]} --yes" in err
    assert _exists(state, mission_id)


def test_on_a_terminal_it_asks_with_the_mission_name_and_keeps_the_mission_unless_confirmed(tmp_path, state, capsys, monkeypatch) -> None:
    mission_id = _create(tmp_path, capsys)
    monkeypatch.setattr(sys, "stdin", _Tty())
    errors = _Tty()
    monkeypatch.setattr(sys, "stderr", errors)
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert main(["mission", "delete", "--mission", mission_id]) == 2
    assert '"work"' in errors.getvalue() and "Kept mission" in errors.getvalue()
    assert _exists(state, mission_id)
    assert main(["mission", "delete", "--mission", mission_id]) == 0
    assert not _exists(state, mission_id)


def test_deleting_twice_reports_not_found_with_status_3(tmp_path, state, capsys) -> None:
    mission_id = _create(tmp_path, capsys)
    _run(capsys, "mission", "delete", mission_id, "--yes")
    err = _run(capsys, "mission", "delete", mission_id, "--yes", code=3)
    assert "no mission matches" in err
    assert "no mission matches" in _run(capsys, "mission", "delete", mission_id[:8], "--yes", code=3)


def test_an_ambiguous_prefix_is_not_found_status_3_and_deletes_nothing(tmp_path, state, capsys) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        for index in range(2):
            folder = tmp_path / f"f{index}"
            folder.mkdir()
            store.create_mission(f"m{index}", "objective", cwd=folder, mission_id=f"abc{index}" + "0" * 28)
    err = _run(capsys, "mission", "delete", "abc", "--yes", code=3)
    assert "ambiguous" in err
    with BridgeStore(state / "bridge.sqlite3", readonly=True) as store:
        assert len(store.list_missions()) == 2


def test_the_mission_must_be_named_and_the_current_folder_is_never_used(tmp_path, state, capsys, monkeypatch) -> None:
    mission_id = _create(tmp_path, capsys)
    monkeypatch.chdir(tmp_path / "work")  # the mission's own folder
    err = _run(capsys, "mission", "delete", "--yes", code=2)
    assert "name the mission to delete" in err
    assert _exists(state, mission_id)
    assert main(["mission", "delete", "--mission", mission_id[:8], "--yes"]) == 0  # --mission is the same as the positional form


def test_a_mission_with_a_live_controller_or_station_is_refused_and_nothing_changes(tmp_path, state, capsys, monkeypatch) -> None:
    mission_id = _create(tmp_path, capsys)
    lock = MissionLock(state / "locks", mission_id)
    lock.acquire()
    try:
        err = _run(capsys, "mission", "delete", mission_id, "--yes", code=4)
    finally:
        lock.release()
    assert "active controller" in err and _exists(state, mission_id)
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: True)
    err = _run(capsys, "mission", "delete", mission_id, "--yes", code=4)
    assert "live provider station" in err and "llm-bridge gc" in err and _exists(state, mission_id)


def test_a_managed_provider_session_may_not_delete_missions(tmp_path, state, capsys, monkeypatch) -> None:
    mission_id = _create(tmp_path, capsys)
    monkeypatch.setenv(bridge.MANAGED_ENV, "1")
    _run(capsys, "mission", "delete", mission_id, "--yes", code=2)
    assert _exists(state, mission_id)


def test_the_json_result_lists_what_was_removed(tmp_path, state, capsys) -> None:
    mission_id = _create(tmp_path, capsys)
    result = json.loads(_run(capsys, "mission", "delete", mission_id, "--yes", "--json"))
    assert result["status"] == "DELETED" and result["mission_id"] == mission_id and result["previous_status"] == "CREATED"
    assert result["removed"]["tasks"] == 1 and result["removed"]["events"] >= 1


def test_mission_help_lists_the_command(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["mission", "--help"])
    assert "delete" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["mission", "delete", "--help"])
    text = capsys.readouterr().out
    assert "--yes" in text and "cannot be undone" in text and "never inferred from the folder" in text


# ------------------------------------------------------------------ the store: nothing is left behind


def _populate(store: BridgeStore, folder: Path, name: str) -> str:
    folder.mkdir()
    mission = store.create_mission(name, f"{name} objective", cwd=folder, initial_prompt="first")
    mission_id = str(mission["id"])
    second = store.add_task(mission_id, "second", depends_on=[mission["initial_task_id"]])
    store.amend_task(second, prompt="second, amended")
    store.set_mission_status(mission_id, "RUNNING")
    operation = store.begin_operation(int(mission["initial_task_id"]), iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0, logs_path=str(folder / "unused.out"))
    store.set_mission_status(mission_id, "STOPPED")
    store.enqueue_command(mission_id, {"action": "pause"})
    with store._transaction():
        store.db.execute(
            "INSERT INTO managed_processes(id,mission_id,task_id,kind,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (f"mp-{name}", mission_id, mission["initial_task_id"], "verifier", "EXITED", "2026-11-08T00:00:00+00:00", "2026-11-08T00:00:00+00:00"),
        )
    return mission_id


def _tables(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    db = sqlite3.connect(path)
    try:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: [tuple(row) for row in db.execute(f"SELECT * FROM {name}")] for name in names}
    finally:
        db.close()


def test_the_store_leaves_no_row_of_any_table_that_mentions_the_mission(tmp_path, state) -> None:
    path = state / "bridge.sqlite3"
    with BridgeStore(path) as store:
        doomed = _populate(store, tmp_path / "doomed", "doomed")
        kept = _populate(store, tmp_path / "kept", "kept")
        task_ids = [int(row["id"]) for row in store.db.execute("SELECT id FROM tasks WHERE mission_id=?", (doomed,))]
    before = _tables(path)
    populated = {name for name, rows in before.items() if any(doomed in map(str, row) for row in rows)}
    assert {"missions", "tasks", "events", "operations", "commands", "managed_processes", "mission_task_counts"} <= populated
    assert any(row[0] in task_ids for row in before["task_dependencies"]) and any(row[1] in task_ids for row in before["amendments"])
    with BridgeStore(path) as store:
        result = store.delete_mission(doomed)
    assert result["tasks"] == 2 and result["events"] > 0 and result["operations"] == 1
    after = _tables(path)
    assert set(after) == set(before) | {"mission_tombstones"}
    holding = {name for name, rows in after.items() if any(doomed in map(str, row) for row in rows)}
    assert holding == {"mission_tombstones"}, f"tables that still mention the deleted mission: {holding}"
    marks = ",".join("?" * len(task_ids))
    db = sqlite3.connect(path)
    try:  # dependencies and amendments carry task IDs, not the mission ID
        assert db.execute(f"SELECT COUNT(*) FROM task_dependencies WHERE task_id IN ({marks}) OR depends_on IN ({marks})", task_ids * 2).fetchone()[0] == 0
        assert db.execute(f"SELECT COUNT(*) FROM amendments WHERE task_id IN ({marks})", task_ids).fetchone()[0] == 0
    finally:
        db.close()
    assert any(kept in map(str, row) for row in after["missions"])
    for name in ("tasks", "events", "operations", "commands", "managed_processes", "mission_task_counts"):
        assert len(after[name]) == len([row for row in before[name] if doomed not in map(str, row)]), name
    assert len(after["task_dependencies"]) == 1 and len(after["amendments"]) == 1, "the other mission keeps its dependency and amendment"
    assert [row[:3] for row in after["mission_tombstones"]] == [(doomed, "doomed", "STOPPED")]


def test_the_store_refuses_a_live_mission_without_changing_anything(tmp_path, state) -> None:
    path = state / "bridge.sqlite3"
    with BridgeStore(path) as store:
        mission_id = _populate(store, tmp_path / "live", "live")
        store.set_mission_status(mission_id, "RUNNING")
    before = _tables(path)
    with BridgeStore(path) as store:
        with pytest.raises(bridge.TransitionError, match="running, so it cannot be deleted"):
            store.delete_mission(mission_id)
    assert _tables(path) == before


def test_tombstones_are_bounded_and_the_table_is_created_by_the_first_deletion(tmp_path, state) -> None:
    path = state / "bridge.sqlite3"
    with BridgeStore(path) as store:
        assert store.pending_tombstones() == [] and "mission_tombstones" not in _tables(path)
        store.TOMBSTONE_KEEP_COUNT = 3
        ids = []
        for index in range(5):
            folder = tmp_path / f"b{index}"
            folder.mkdir()
            ids.append(str(store.create_mission(f"b{index}", "objective", cwd=folder)["id"]))
            store.delete_mission(ids[-1])
        remaining = [row[0] for row in _tables(path)["mission_tombstones"]]
        assert len(remaining) == 3 and set(remaining) <= set(ids)
        with store._transaction():
            store.db.execute("UPDATE mission_tombstones SET deleted_at='2020-01-01T00:00:00+00:00'")
        folder = tmp_path / "c"
        folder.mkdir()
        newest = str(store.create_mission("c", "objective", cwd=folder)["id"])
        store.delete_mission(newest)
        assert [row[0] for row in _tables(path)["mission_tombstones"]] == [newest], "tombstones past the retention age are dropped"


# ------------------------------------------------------------------ files


def test_files_of_the_mission_go_and_other_files_stay(tmp_path, state, capsys) -> None:
    path = state / "bridge.sqlite3"
    other_id = _create(tmp_path, capsys, "other")
    mission_id = _create(tmp_path, capsys, "gone")
    runtime = state / "runtime" / f"mission-{mission_id}"
    run = runtime / "ralph-logs" / "20261108-000000-codex"
    run.mkdir(parents=True)
    (run / "attempt-1.out").write_text("provider output")
    (runtime / "verifier").mkdir()
    (runtime / "verifier" / "attempt-1.log").write_text("verifier output")
    (runtime / "controller.log").write_text("controller")
    other_runtime = state / "runtime" / f"mission-{other_id}"
    (other_runtime / "verifier").mkdir(parents=True)
    (other_runtime / "verifier" / "keep.log").write_text("not this one")
    outside_run = tmp_path / "old-logs" / "20261108-000001-codex"
    outside_run.mkdir(parents=True)
    (outside_run / "attempt-1.out").write_text("an older log directory")
    unrelated = tmp_path / "notes" / "important.txt"
    unrelated.parent.mkdir()
    unrelated.write_text("not a run directory")
    _set_status(state, mission_id, "RUNNING")
    with BridgeStore(path) as store:
        task = store.list_missions()
        first = int(store.db.execute("SELECT id FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0])
        for number, logs in enumerate((run / "attempt-1.out", outside_run / "attempt-1.out", unrelated), start=1):
            operation = store.begin_operation(first, iteration=number, station="codex")
            store.finish_operation(operation, exit_code=1, logs_path=str(logs))
            with store._transaction():
                store.db.execute("UPDATE tasks SET status='QUEUED' WHERE id=?", (first,))
        assert task
    locks = state / "locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    (locks / f"mission-{mission_id}.start.lock").write_text("")
    (locks / f"mission-{mission_id}.lock").write_text("")
    _set_status(state, mission_id, "FAILED")

    result = json.loads(_run(capsys, "mission", "delete", mission_id, "--yes", "--json"))

    assert result["removed"]["files"] >= 3
    assert not runtime.exists() and not outside_run.exists()
    assert not list(locks.glob(f"mission-{mission_id}*"))
    assert unrelated.read_text() == "not a run directory", "a log path that is not in a run directory is never followed"
    assert (other_runtime / "verifier" / "keep.log").read_text() == "not this one" and _exists(state, other_id)


def test_a_dead_station_socket_is_removed_and_a_live_station_is_not_touched(tmp_path, state, capsys, monkeypatch) -> None:
    from llm_tools.bridge_sockets import mission_socket_path

    mission_id = _create(tmp_path, capsys)
    socket_path = mission_socket_path(mission_id)
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(socket_path))
    socket_path.chmod(0o600)  # as tmux creates it
    dead.close()  # tmux never unlinks its socket: the file stays, nothing listens
    assert socket_path.exists()
    _run(capsys, "mission", "delete", mission_id, "--yes")
    assert not socket_path.exists()

    second = _create(tmp_path, capsys, "second")
    live_path = mission_socket_path(second)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(live_path))
    live_path.chmod(0o600)
    listener.listen(1)
    try:
        monkeypatch.undo()  # use the real liveness probe: a socket that accepts connections is a live station
        monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
        assert main(["mission", "delete", second, "--yes"]) == 4
        assert live_path.exists() and _exists(state, second)
        capsys.readouterr()
    finally:
        listener.close()


# ------------------------------------------------------------------ the typed command


def test_the_operation_is_declared_and_needs_an_explicit_confirmation(tmp_path, state, capsys) -> None:
    entry = next(item for item in commands.operation_catalog() if item["operation"] == "mission.delete")
    assert entry["mutating"] and entry["needs_mission"] and entry["params"] == {"confirm": {"type": "bool", "required": False}, "force": {"type": "bool", "required": False}}
    mission_id = _create(tmp_path, capsys)
    refused = commands.execute(commands.CommandRequest("mission.delete", mission_id=mission_id, request_id="d1"))
    assert refused.outcome == "rejected" and refused.error is not None and refused.error.code == "confirmation-required"
    assert _exists(state, mission_id)
    applied = commands.execute(commands.CommandRequest("mission.delete", {"confirm": True}, mission_id=mission_id, request_id="d2"))
    assert applied.outcome == "applied" and applied.result is not None and applied.result["mission_id"] == mission_id
    assert not _exists(state, mission_id)
    again = commands.execute(commands.CommandRequest("mission.delete", {"confirm": True}, mission_id=mission_id, request_id="d3"))
    assert again.outcome == "rejected" and again.error is not None and again.error.code == "not-found"


def test_a_live_mission_through_the_envelope_is_a_conflict(tmp_path, state, capsys) -> None:
    mission_id = _create(tmp_path, capsys)
    _set_status(state, mission_id, "PAUSED")
    result = commands.execute(commands.CommandRequest("mission.delete", {"confirm": True}, mission_id=mission_id))
    assert result.outcome == "rejected" and result.error is not None and result.error.code == "conflict"
    assert "paused" in result.error.message and _exists(state, mission_id)


# ------------------------------------------------------------------ replication of the tombstone


def _session(path: Path) -> worker.WorkerSession:
    config = worker.WorkerConfig("worker-id", "ws-1", "wss://coordinator.example", "t" * 40, True)
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(path), software_version="9.9",
                                   save=lambda value: None, audit=lambda *args, **kwargs: None)
    session.clock = lambda: 1000.0
    welcome = Message("welcome", "coordinator", 1, {"generation": 1, "protocol_version": 1, "event_cursor": 0})
    assert session.handle(welcome) == []
    return session


def test_a_deleted_mission_is_replicated_as_a_tombstone_alone_and_marked_after_the_pong(tmp_path, state, capsys) -> None:
    path = state / "bridge.sqlite3"
    mission_id = _create(tmp_path, capsys)
    session = _session(path)
    assert any(m.payload["id"] == mission_id for m in [Message("x", "w", 0, s) for s in session.hello().payload["missions"]])
    _run(capsys, "mission", "delete", mission_id, "--yes")

    batch = session.next_events()
    assert batch is not None and batch.type == "events"
    cursor = session.cursor
    assert dict(batch.payload) == {"after": cursor, "through": cursor, "events": [], "deleted_missions": [mission_id]}
    assert decode(batch.encode()).payload["deleted_missions"] == [mission_id]
    assert session.next_events() is None, "one batch in flight"
    assert all(item["id"] != mission_id for item in session.hello().payload["missions"])

    session.handle(Message("pong", "coordinator", 1, {}))  # not the pong of this batch: nothing is marked yet
    with BridgeStore(path) as store:
        assert store.pending_tombstones() == [mission_id]
    ping = session.barrier_ping()
    assert ping.type == "ping"
    session.handle(Message("pong", "coordinator", 1, {}))
    session.handle(Message("pong", "coordinator", 1, {}))
    with BridgeStore(path) as store:
        assert store.pending_tombstones() == [] and store.tombstone(mission_id)["replicated_at"]
    assert session.next_events() is None


def test_a_tombstone_stays_pending_until_the_batch_is_confirmed(tmp_path, state, capsys) -> None:
    path = state / "bridge.sqlite3"
    mission_id = _create(tmp_path, capsys)
    _run(capsys, "mission", "delete", mission_id, "--yes")
    session = _session(path)
    first = session.next_events()
    assert first is not None
    session.barrier_ping()
    session.handle(Message("events.nack", "coordinator", 1, {"expected_after": 0}))  # no rewind: the batch starts at the cursor
    session.inflight, session.send_not_before = None, 0.0  # the connection dropped before the pong
    again = session.next_events()
    assert again is not None and again.payload["deleted_missions"] == [mission_id]


def test_a_coordinator_that_rejects_the_field_keeps_the_tombstone_and_stops_receiving_it(tmp_path, state, capsys) -> None:
    path = state / "bridge.sqlite3"
    mission_id = _create(tmp_path, capsys)
    _run(capsys, "mission", "delete", mission_id, "--yes")
    session = _session(path)
    batch = session.next_events()
    assert batch is not None
    session.barrier_ping()
    session.handle(Message("error", "coordinator", 1, {"code": "invalid-message", "message": "events: unknown ['deleted_missions']", "in_reply_to": batch.id}))
    session.handle(Message("pong", "coordinator", 1, {}))
    assert session.tombstones_unsupported
    with BridgeStore(path) as store:
        assert store.pending_tombstones() == [mission_id], "never marked as replicated"
    assert session.next_events() is None, "not offered again on this connection"
    session.handle(Message("welcome", "coordinator", 2, {"generation": 2, "protocol_version": 1, "event_cursor": 0}))
    assert not session.tombstones_unsupported and session.next_events() is not None


def test_the_events_message_validates_deleted_missions() -> None:
    def encoded(value: Any) -> str:
        return Message("events", "w", 1, {"after": 0, "through": 0, "events": [], "deleted_missions": value}).encode()

    assert decode(encoded(["abc", "d-e_f.1"])).payload["deleted_missions"] == ["abc", "d-e_f.1"]
    for bad in ("abc", [7], ["bad id"], [""], ["a" * 129], [f"m{n}" for n in range(101)]):
        with pytest.raises(ProtocolError) as raised:
            decode(encoded(bad))
        assert raised.value.code == "invalid-message"
