"""CLIUX-003: the operator can read the provider's output and see the workspace change before approving."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from llm_tools import bridge_commands as commands
from llm_tools import bridge_help, bridge_missions, bridge_remote, bridge_tui
from llm_tools.bridge import dispatch_console_command, main
from llm_tools.bridge_progress import repository_checkpoint
from llm_tools.bridge_store import BridgeStore


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    return directory


def _git(folder: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(folder), *args],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _repo(tmp_path: Path) -> Path:
    folder = tmp_path / "repo"
    folder.mkdir()
    _git(folder, "init", "-q")
    (folder / "hello.txt").write_text("hello\n")
    (folder / "keep.txt").write_text("keep\n")
    _git(folder, "add", ".")
    _git(folder, "commit", "-q", "-m", "init")
    return folder


def _mission(state: Path, cwd: Path) -> tuple[str, int]:
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "objective", cwd=cwd, initial_prompt="do it", config={"providers": "codex"})
        store.set_mission_status(mission["id"], "RUNNING")
        return str(mission["id"]), int(mission["initial_task_id"])


def _run_task(state: Path, mission_id: str, task_id: int, cwd: Path, *, log: Path | None, checkpoint: bool = True) -> str:
    with BridgeStore(state / "bridge.sqlite3") as store:
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.mark_operation_running(operation, pane_id="%1", process_pid=4242, process_start="boot", route_id="codex", model="gpt")
        if checkpoint:
            store.record_runtime_event(mission_id, "repository-checkpoint", {
                "operation_id": operation, "task_id": task_id, "revision": 1, "checkpoint": repository_checkpoint(cwd),
            })
        return operation


def _finish(state: Path, operation: str, *, log: Path | None, exit_code: int = 0) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.finish_operation(operation, exit_code=exit_code, logs_path=str(log) if log else None)


def _cli(capsys, *argv: str, code: int = 0) -> tuple[str, str]:
    assert main(list(argv)) == code, capsys.readouterr()
    captured = capsys.readouterr()
    return captured.out, captured.err


# ---------------------------------------------------------------- repository checkpoint


def test_checkpoint_records_the_head_commit_and_files_already_modified(tmp_path) -> None:
    folder = _repo(tmp_path)
    (folder / "keep.txt").write_text("edited before the run\n")
    checkpoint = repository_checkpoint(folder)
    head = subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    assert checkpoint["head"] == head
    assert checkpoint["dirty"] == ["keep.txt"]


# ---------------------------------------------------------------- task output


def test_task_output_prints_the_retained_log_plainly_when_not_a_terminal(tmp_path, state, capsys) -> None:
    mission_id, task_id = _mission(state, tmp_path)
    log = tmp_path / "attempt-1.out"
    log.write_text("provider said hello\nsecond line\n")
    _finish(state, _run_task(state, mission_id, task_id, tmp_path, log=log, checkpoint=False), log=log)
    out, _ = _cli(capsys, "task", "output", "--task", "1", "--mission", mission_id)
    assert out == "provider said hello\nsecond line\n"


def test_task_output_follow_needs_a_terminal(tmp_path, state, capsys) -> None:
    mission_id, task_id = _mission(state, tmp_path)
    log = tmp_path / "attempt-1.out"
    log.write_text("x\n")
    _finish(state, _run_task(state, mission_id, task_id, tmp_path, log=log, checkpoint=False), log=log)
    _, err = _cli(capsys, "task", "output", "--task", "1", "--mission", mission_id, "--follow", code=2)
    assert "--follow needs an interactive terminal" in err


def test_task_output_says_when_the_task_has_not_run(tmp_path, state, capsys) -> None:
    mission_id, _ = _mission(state, tmp_path)
    _, err = _cli(capsys, "task", "output", "--task", "1", "--mission", mission_id, code=2)
    assert "task 1 has not run" in err


def test_task_output_says_when_the_log_was_pruned_and_keeps_the_audit_digest(tmp_path, state, capsys) -> None:
    mission_id, task_id = _mission(state, tmp_path)
    log = tmp_path / "attempt-1.out"
    log.write_text("gone soon\n")
    operation = _run_task(state, mission_id, task_id, tmp_path, log=log, checkpoint=False)
    _finish(state, operation, log=log)
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.record_pruned_operation_log(mission_id, str(log), size_bytes=10, sha256="ab" * 32)
    _, err = _cli(capsys, "task", "output", "--task", "1", "--mission", mission_id, code=2)
    assert "pruned" in err and "10 bytes" in err and "abababab" in err and "--log-retained-runs" in err


def test_task_output_reports_a_log_file_that_vanished(tmp_path, state, capsys) -> None:
    mission_id, task_id = _mission(state, tmp_path)
    log = tmp_path / "attempt-1.out"
    _finish(state, _run_task(state, mission_id, task_id, tmp_path, log=log, checkpoint=False), log=log)
    _, err = _cli(capsys, "task", "output", "--task", "1", "--mission", mission_id, code=2)
    assert "no longer on disk" in err


def test_task_output_json_describes_an_unavailable_log(tmp_path, state, capsys) -> None:
    mission_id, _ = _mission(state, tmp_path)
    out, _ = _cli(capsys, "task", "output", "--task", "1", "--mission", mission_id, "--json", code=2)
    data = json.loads(out)
    assert data["available"] is False and "has not run" in data["message"]


def test_task_output_uses_the_latest_operation(tmp_path, state, capsys) -> None:
    mission_id, task_id = _mission(state, tmp_path)
    first, second = tmp_path / "attempt-1.out", tmp_path / "attempt-2.out"
    first.write_text("first run\n")
    second.write_text("second run\n")
    _finish(state, _run_task(state, mission_id, task_id, tmp_path, log=first, checkpoint=False), log=first, exit_code=1)
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.db.execute("UPDATE tasks SET status='QUEUED' WHERE id=?", (task_id,))
        store.db.commit()
    operation = _run_task(state, mission_id, task_id, tmp_path, log=second, checkpoint=False)
    _finish(state, operation, log=second)
    out, _ = _cli(capsys, "task", "output", "--task", "1", "--mission", mission_id)
    assert out == "second run\n"


def test_console_task_output_is_the_log_text(tmp_path, state, capsys) -> None:
    mission_id, task_id = _mission(state, tmp_path)
    log = tmp_path / "attempt-1.out"
    log.write_text("console sees this\n")
    _finish(state, _run_task(state, mission_id, task_id, tmp_path, log=log, checkpoint=False), log=log)
    assert dispatch_console_command(mission_id, "task output --task 1") == "console sees this\n"
    assert dispatch_console_command(mission_id, "task output --task 2").startswith("error: ")


# ---------------------------------------------------------------- task diff


def test_task_diff_shows_changes_since_the_recorded_checkpoint(tmp_path, state, capsys) -> None:
    folder = _repo(tmp_path)
    mission_id, task_id = _mission(state, folder)
    (folder / "keep.txt").write_text("edited before the run\n")  # already modified when the run starts
    operation = _run_task(state, mission_id, task_id, folder, log=None)
    (folder / "hello.txt").write_text("hello\nworld\n")
    (folder / "new.txt").write_text("brand new\n")
    _finish(state, operation, log=None)
    out, _ = _cli(capsys, "task", "diff", "--task", "1", "--mission", mission_id)
    assert "+world" in out and "hello.txt" in out
    assert "Untracked files (1): new.txt" in out
    assert "keep.txt" in out and "already modified before the run" in out


def test_task_diff_for_a_clean_tree_says_nothing_changed(tmp_path, state, capsys) -> None:
    folder = _repo(tmp_path)
    mission_id, task_id = _mission(state, folder)
    _finish(state, _run_task(state, mission_id, task_id, folder, log=None), log=None)
    out, _ = _cli(capsys, "task", "diff", "--task", "1", "--mission", mission_id)
    assert "No changes since the start of the last run of task 1." in out


def test_task_diff_names_a_folder_that_is_not_a_git_repository(tmp_path, state, capsys) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    mission_id, task_id = _mission(state, folder)
    _finish(state, _run_task(state, mission_id, task_id, folder, log=None), log=None)
    _, err = _cli(capsys, "task", "diff", "--task", "1", "--mission", mission_id, code=2)
    assert "not a git repository" in err and str(folder) in err


def test_task_diff_without_a_checkpoint_explains_why(tmp_path, state, capsys) -> None:
    folder = _repo(tmp_path)
    mission_id, task_id = _mission(state, folder)
    _, err = _cli(capsys, "task", "diff", "--task", "1", "--mission", mission_id, code=2)
    assert "no repository checkpoint" in err
    _finish(state, _run_task(state, mission_id, task_id, folder, log=None, checkpoint=False), log=None)
    _, err = _cli(capsys, "task", "diff", "--task", "1", "--mission", mission_id, code=2)
    assert "no repository checkpoint" in err


def test_task_diff_json_and_console_text(tmp_path, state, capsys) -> None:
    folder = _repo(tmp_path)
    mission_id, task_id = _mission(state, folder)
    operation = _run_task(state, mission_id, task_id, folder, log=None)
    (folder / "hello.txt").write_text("changed\n")
    _finish(state, operation, log=None)
    out, _ = _cli(capsys, "task", "diff", "--task", "1", "--mission", mission_id, "--json")
    data = json.loads(out)
    assert data["available"] is True and data["files"] == ["hello.txt"] and "+changed" in data["text"]
    assert "+changed" in dispatch_console_command(mission_id, "task diff --task 1")


# ---------------------------------------------------------------- typed command layer


def test_output_and_diff_are_typed_read_operations(tmp_path, state) -> None:
    folder = _repo(tmp_path)
    mission_id, task_id = _mission(state, folder)
    log = tmp_path / "attempt-1.out"
    log.write_text("remote readable\n")
    operation = _run_task(state, mission_id, task_id, folder, log=log)
    (folder / "hello.txt").write_text("changed\n")
    _finish(state, operation, log=log)
    for name in ("task.output", "task.diff"):
        assert commands.lookup(name).mutating is False and commands.lookup(name).needs_mission
    output = commands.dispatch(commands.CommandRequest("task.output", {"task_number": 1}, mission_id=mission_id))
    assert output["available"] and output["text"] == "remote readable\n" and output["truncated"] is False
    diff = commands.dispatch(commands.CommandRequest("task.diff", {"task_number": 1}, mission_id=mission_id))
    assert diff["available"] and "+changed" in diff["text"]


def test_remote_request_maps_task_output_and_diff(monkeypatch) -> None:
    import argparse

    args = argparse.Namespace(command="task", task_action="output", task=3, task_by_id=None, mission="m1", follow=False, json=False)
    request, _ = bridge_remote.request_for(args, "pi")
    assert request.operation == "task.output" and request.params == {"task_number": 3}
    args.task_action = "diff"
    request, _ = bridge_remote.request_for(args, "pi")
    assert request.operation == "task.diff" and request.params == {"task_number": 3}


# ---------------------------------------------------------------- console


def _console(snapshot: dict[str, object], dispatched: list[str] | None = None) -> bridge_tui._ConsoleState:
    seen = dispatched if dispatched is not None else []

    def dispatch(_mission: str, command: str) -> str:
        seen.append(command)
        return "body of " + command

    state = bridge_tui._ConsoleState("M1", dispatch, lambda *_: {}, full=True)
    state.snapshot = bridge_tui._bounded_snapshot(snapshot)
    return state


def _snapshot(*, files: list[str] | None = None, total: int | None = None) -> dict[str, object]:
    progress = {"operation_id": "op1", "task_id": 7, "before": "a", "after": "b", "exit_code": 0}
    if files is not None:
        progress["changed_files"] = files
        progress["changed_total"] = total if total is not None else len(files)
    return {
        "mission": {"id": "M1", "status": "RUNNING"},
        "tasks": [{"id": 7, "ordinal": 1, "title": "Write hello", "status": "VERIFYING", "evidence": []}],
        "operations": [{"id": "op1", "task_id": 7, "station": "claude", "route_id": "claude", "status": "EXITED", "exit_code": 0,
                        "created_at": "2026-01-01T10:00:00+00:00", "updated_at": "2026-01-01T10:00:12+00:00"}],
        "events": [{"id": 1, "kind": "operation-progress", "task_id": 7, "data": progress}],
    }


def test_approval_summary_lists_the_changed_files() -> None:
    lines = bridge_tui._approval_summary(_snapshot(files=["hello.txt"]), ("task", "1"))
    assert "Workspace: 1 file changed: hello.txt" in lines
    many = bridge_tui._approval_summary(_snapshot(files=["a", "b", "c", "d", "e", "f"], total=9), ("task", "1"))
    assert any(line.startswith("Workspace: 9 files changed: a, b, c, d, e") and "and 4 more" in line for line in many)
    assert "Workspace: changed during the run" in bridge_tui._approval_summary(_snapshot(), ("task", "1"))


def test_approval_dialog_offers_output_and_diff_keys_that_open_the_pager() -> None:
    dispatched: list[str] = []
    state = _console(_snapshot(files=["hello.txt"]), dispatched)
    assert state._confirm_or_dispatch("approve 1")
    assert "Y approve · N or Esc cancel · o output · d diff" in state.output[-1]
    state.handle_key("o")
    assert state.confirm_command is not None  # the dialog stays open behind the pager
    deadline = time.monotonic() + 5
    while state.document_viewer is None and time.monotonic() < deadline:
        state.refresh(force=True)
        time.sleep(0.01)
    assert dispatched == ["task output --task 1"]
    assert state.document_viewer is not None
    state.close_document()
    state.handle_key("d")
    deadline = time.monotonic() + 5
    while state.document_viewer is None and time.monotonic() < deadline:
        state.refresh(force=True)
        time.sleep(0.01)
    assert dispatched[-1] == "task diff --task 1" and state.document_viewer is not None


def test_pager_shows_the_error_when_there_is_no_output() -> None:
    def dispatch(_mission: str, _command: str) -> str:
        return "error: task 1 has not run; there is no provider output yet"

    state = bridge_tui._ConsoleState("M1", dispatch, lambda *_: {}, full=True)
    state.snapshot = bridge_tui._bounded_snapshot(_snapshot())
    state.view_index = bridge_tui.VIEWS.index("Tasks")
    state.handle_key("o")
    deadline = time.monotonic() + 5
    while state.document_pending and time.monotonic() < deadline:
        state.refresh(force=True)
        time.sleep(0.01)
    assert state.document_viewer is None
    assert any("has not run" in line for line in state.output)


def test_tasks_inspect_shows_the_last_run_and_its_keys() -> None:
    state = _console(_snapshot(files=["hello.txt"]))
    state.view_index = bridge_tui.VIEWS.index("Tasks")
    state.task_detail = True
    rows = state._order_rows()
    assert "Last run: claude · exit 0 · 12s · o output · d diff" in rows


# ---------------------------------------------------------------- help and docs


def test_help_and_readme_mention_the_two_commands() -> None:
    text = bridge_help.topic_text("verification")
    assert "task output" in text and "task diff" in text
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert "task output" in readme and "task diff" in readme


# ---------------------------------------------------------------- recorded change, live logs, terminal paths


def test_recorded_change_names_the_files_for_the_approval_dialog(tmp_path) -> None:
    from llm_tools.bridge_review import recorded_change

    folder = _repo(tmp_path)
    (folder / "keep.txt").write_text("edited before the run\n")
    base = repository_checkpoint(folder)
    (folder / "hello.txt").write_text("changed\n")
    (folder / "new.txt").write_text("new\n")
    recorded = recorded_change(folder, base)
    assert recorded == {"changed_files": ["hello.txt", "keep.txt", "new.txt"], "changed_total": 3, "already_modified": 1}
    assert recorded_change(tmp_path / "missing", base) == {}
    assert recorded_change(folder, {"head": None}) == {}


def test_a_running_operation_exposes_its_log_for_follow(tmp_path, state, capsys) -> None:
    mission_id, task_id = _mission(state, tmp_path)
    operation = _run_task(state, mission_id, task_id, tmp_path, log=None, checkpoint=False)
    _, err = _cli(capsys, "task", "output", "--task", "1", "--mission", mission_id, code=2)
    assert "still running" in err
    log = tmp_path / "attempt-1.out"
    log.write_text("partial\n")
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.record_operation_log_path(operation, str(log))
        store.record_operation_log_path(operation, str(tmp_path / "other.out"))  # the first path stays
    out, _ = _cli(capsys, "task", "output", "--task", "1", "--mission", mission_id)
    assert out == "partial\n"


def test_output_on_a_terminal_opens_the_pager(tmp_path, state, monkeypatch, capsys) -> None:
    import sys

    from llm_tools import bridge_viewer

    mission_id, task_id = _mission(state, tmp_path)
    log = tmp_path / "attempt-1.out"
    log.write_bytes(b"line one\n\xff bad byte\n")
    _finish(state, _run_task(state, mission_id, task_id, tmp_path, log=log, checkpoint=False), log=log)
    opened: list[tuple[str, bool]] = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(bridge_viewer, "run_document", lambda source, *, title, path=None, follow=False: opened.append((source + "|" + title, follow)) or 0)
    _cli(capsys, "task", "output", "--task", "1", "--mission", mission_id)
    _cli(capsys, "task", "output", "--task", "1", "--mission", mission_id, "--follow")
    assert opened[0][0].startswith("line one\n� bad byte\n|Output · task 1 · codex") and opened[0][1] is False
    assert opened[1][1] is True


def _wait_for_viewer(state: bridge_tui._ConsoleState) -> None:
    deadline = time.monotonic() + 5
    while state.document_viewer is None and time.monotonic() < deadline:
        state.refresh(force=True)
        time.sleep(0.01)


def test_the_tasks_view_opens_output_and_diff_with_o_and_d() -> None:
    dispatched: list[str] = []
    state = _console(_snapshot(files=["hello.txt"]), dispatched)
    state.view_index = bridge_tui.VIEWS.index("Tasks")
    for key, kind in (("o", "output"), ("d", "diff")):
        state.handle_key(key)
        _wait_for_viewer(state)
        assert state.document_viewer is not None and kind in state.document_viewer.title
        assert state.document_task_id is None  # not an editable prompt
        state.close_document()
    assert dispatched == ["task output --task 1", "task diff --task 1"]


def test_closing_the_pager_returns_to_the_approval_dialog() -> None:
    state = _console(_snapshot(files=["hello.txt"]))
    assert state._confirm_or_dispatch("approve 1")
    state.handle_key("d")
    _wait_for_viewer(state)
    assert state.document_viewer is not None and not state.response_overlay  # the pager is visible, not hidden by the dialog
    state.close_document()
    assert state.response_overlay and state.confirm_command is not None
    state.handle_key("n")
    assert state.confirm_command is None
