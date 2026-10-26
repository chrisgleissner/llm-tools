"""Public operator workflows through real SQLite dispatch."""
import io
import json

from llm_tools.bridge import dispatch_console_command, main, status_snapshot
from llm_tools.bridge_store import BridgeStore


def create(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    assert main(["mission", "create", "--prompt", "Original objective\nsecond line\n",
                 "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    return state, result["mission"]["id"], result["task"]["id"]


def test_cross_mission_writes_fail_without_partial_effect(tmp_path, monkeypatch, capsys):
    state, mission, first = create(tmp_path, monkeypatch, capsys)
    other = tmp_path / "other"
    other.mkdir()
    assert main(["mission", "create", "--prompt", "other", "--cwd", str(other), "--json"]) == 0
    foreign = json.loads(capsys.readouterr().out)["task"]["id"]
    assert main(["task", "cancel", "--mission", mission, "--task", str(foreign), "--json"]) == 2
    capsys.readouterr()
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_task(foreign)["status"] == "QUEUED"


def test_cycle_rejection_is_reported_as_failure(tmp_path, monkeypatch, capsys):
    state, mission, first = create(tmp_path, monkeypatch, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        second = store.add_task(mission, "dependent", depends_on=[first])
    assert main(["task", "dependencies", "--mission", mission, "--task", str(first),
                 "--add", str(second), "--json"]) == 2
    capsys.readouterr()
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_task(first)["depends_on"] == []


def test_stdin_multiline_and_dependency_submission(tmp_path, monkeypatch, capsys):
    state, mission, first = create(tmp_path, monkeypatch, capsys)
    monkeypatch.setattr("sys.stdin", io.StringIO("New task\nverbatim second line\n"))
    assert main(["task", "add", "--mission", mission, "--file", "-", "--priority", "9",
                 "--depends-on", str(first), "--json"]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["original_prompt"] == "New task\nverbatim second line\n"
    assert added["depends_on"] == [first]


def test_task_priority_reorder_pause_resume_and_amend(tmp_path, monkeypatch, capsys):
    state, mission, first = create(tmp_path, monkeypatch, capsys)
    for action, extra in [("priority", ["--value", "12"]), ("reorder", ["--value", "1"]),
                          ("pause", []), ("resume", [])]:
        assert main(["task", action, "--mission", mission, "--task", str(first), *extra, "--json"]) == 0
        capsys.readouterr()
    amendment = tmp_path / "amendment with spaces.md"
    amendment.write_text("Revised instruction\n", encoding="utf-8")
    assert main(["task", "amend", "--mission", mission, "--task", str(first),
                 "--file", str(amendment), "--json"]) == 0
    capsys.readouterr()
    with BridgeStore(state / "bridge.sqlite3") as store:
        task = store.get_task(first)
        assert task["priority"] == 12 and task["ordering"] == 1
        assert task["pending"]["prompt"] == "Revised instruction\n"
        assert task["original_prompt"] == "Original objective\nsecond line\n"


def test_event_paging_is_ordered_and_bounded(tmp_path, monkeypatch, capsys):
    state, mission, first = create(tmp_path, monkeypatch, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        for index in range(5):
            store.add_task(mission, f"task {index}")
    assert main(["events", "--mission", mission, "--limit", "2", "--json"]) == 0
    events = json.loads(capsys.readouterr().out)["events"]
    assert len(events) == 2
    last = events[-1]["id"]
    assert main(["events", "--mission", mission, "--after", str(last), "--limit", "2", "--json"]) == 0
    assert all(event["id"] > last for event in json.loads(capsys.readouterr().out)["events"])


def test_console_file_path_and_cli_share_durable_commands(tmp_path, monkeypatch, capsys):
    state, mission, first = create(tmp_path, monkeypatch, capsys)
    path = tmp_path / "task with spaces.md"
    path.write_text("Additional task\nsecond paragraph\n", encoding="utf-8")
    result = json.loads(dispatch_console_command(mission, f'add "{path}"'))
    assert result["status"] == "QUEUED"
    assert result["original_prompt"] == path.read_text()
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.db.execute("SELECT status FROM commands ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "APPLIED"
        tasks = store.list_tasks(mission)
        assert len(tasks) == 2 and tasks[1]["prompt"] == path.read_text()
    assert "invalid choice" in dispatch_console_command(mission, "echo not-a-shell")
    assert main(["doctor", "--mission", mission, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sqlite_integrity"] == "ok"


def test_cancellation_preserves_previously_verified_work(tmp_path, monkeypatch, capsys):
    state, mission, first = create(tmp_path, monkeypatch, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mission, "RUNNING")
        operation = store.begin_operation(first, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
    assert main(["task", "verify", "--mission", mission, "--task", str(first), "--approve", "--json"]) == 0
    capsys.readouterr()
    assert main(["cancel", "--mission", mission, "--json"]) == 0
    capsys.readouterr()
    snapshot = status_snapshot(mission)
    assert snapshot["mission"]["status"] == "CANCELLED"
    assert snapshot["tasks"][0]["status"] == "COMPLETED"
    assert snapshot["tasks"][0]["evidence"][-1]["source"] == "approval"
