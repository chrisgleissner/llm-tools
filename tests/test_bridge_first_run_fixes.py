"""First-time-user fixes: stop never cancels, per-mission task numbers, plain messages.

Every test runs against an isolated state directory with mocked backends; no
provider or station is started unless a test says so.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from llm_tools import bridge_missions, bridge, bridge_format
from llm_tools.bridge import dispatch_console_command, main
from llm_tools.bridge_store import SCHEMA_VERSION, BridgeStore

LONG_ID = re.compile(r"[0-9a-f]{32}")


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    return directory


def _run(capsys, *argv: str, code: int = 0) -> str:
    assert main(list(argv)) == code, capsys.readouterr()
    captured = capsys.readouterr()
    return captured.out if code == 0 else captured.err


def _folder(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    return folder


def _create(tmp_path: Path, capsys, name: str = "work", *extra: str) -> dict:
    out = _run(capsys, "mission", "create", "--prompt", f"{name} objective", "--name", name, "--cwd", str(_folder(tmp_path, name)), "--providers", "codex", *extra, "--json")
    return json.loads(out)


def _finish_first_task(state: Path, mission_id: str, task_id: int) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mission_id, "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)


# ---------------------------------------------------------------- P0: stop never cancels


@pytest.mark.parametrize("status", ["CREATED", "STOPPED", "PAUSED"])
def test_stop_on_a_saved_mission_changes_nothing(tmp_path, state, capsys, status) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        if status != "CREATED":
            store.set_mission_status(mid, "RUNNING")
            store.set_mission_status(mid, "PAUSED" if status == "PAUSED" else "STOPPED")
    out = _run(capsys, "stop", "--mission", mid)
    assert out.startswith(f"Nothing to stop · mission {mid[:8]} is saved ({status}) · ")
    assert "Stop requested" not in out and "CANCELLED" not in out
    assert ("start it with: llm-bridge start" in out) if status != "PAUSED" else ("resume it with: llm-bridge resume" in out)
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid)["status"] == status
        assert store.get_task(created["task"]["id"])["status"] == "QUEUED"


def test_stop_force_and_console_stop_never_cancel_a_created_mission(tmp_path, state, capsys, monkeypatch) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    assert _run(capsys, "stop", "--mission", mid, "--force").startswith(f"Nothing to stop · mission {mid[:8]} is saved (CREATED)")
    reply = json.loads(dispatch_console_command(mid, "/stop"))
    assert reply["status"] == "APPLIED" and reply["result"]["noop"] is True
    assert reply["result"]["detail"] == f"Nothing to stop · mission {mid[:8]} is saved (CREATED) · start it with: llm-bridge start"
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid)["status"] == "CREATED"
    # The mission is still the folder's unfinished mission, so start and status find it.
    monkeypatch.chdir(tmp_path / "work")
    assert mid[:8] in _run(capsys, "status")
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert bridge.resolve_mission_ref(store, None) == mid


def test_only_cancel_cancels_a_mission(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    assert "Cancel requested" in _run(capsys, "cancel", "--mission", mid)
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid)["status"] == "CANCELLED"


def test_stop_with_a_controller_still_asks_it_to_stop(tmp_path, state, capsys, monkeypatch) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        store.set_mission_status(mid, "PAUSED")
    monkeypatch.setattr(bridge, "controller_running", lambda _mission_id: True)
    assert _run(capsys, "stop", "--mission", mid).startswith(f"Stop requested · mission {mid[:8]} is STOPPING")


# ---------------------------------------------------------------- P0: per-mission task numbers


def test_task_numbers_restart_at_one_in_every_mission(tmp_path, state, capsys) -> None:
    first = _create(tmp_path, capsys, "first")
    second = _create(tmp_path, capsys, "second")
    assert first["task"]["ordinal"] == 1 and second["task"]["ordinal"] == 1
    assert second["task"]["id"] != 1  # the global ID is still unique
    mid = second["mission"]["id"]
    added = _run(capsys, "task", "add", "--mission", mid, "--prompt", "more", "--title", "More")
    assert added.startswith('Added task 2 "More"')
    shown = _run(capsys, "task", "show", "--mission", mid, "--task", "1")
    assert shown.splitlines()[0].startswith('task 1 "second objective"')
    listing = _run(capsys, "task", "list", "--mission", mid).splitlines()
    assert listing[0].split()[0] == "TASK" and [line.split()[0] for line in listing[1:]] == ["1", "2"]
    created_line = _run(capsys, "mission", "create", "--prompt", "third", "--cwd", str(_folder(tmp_path, "third")), "--providers", "codex")
    assert "· task 1 ·" in created_line


def test_snapshot_tasks_carry_ordinal_and_keep_the_global_id(tmp_path, state, capsys) -> None:
    _create(tmp_path, capsys, "first")
    second = _create(tmp_path, capsys, "second")
    snapshot = json.loads(_run(capsys, "status", "--mission", second["mission"]["id"], "--json"))
    task = snapshot["tasks"][0]
    assert task["ordinal"] == 1 and task["id"] == second["task"]["id"] != 1
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.add_task(second["mission"]["id"], "dependent", depends_on=[task["id"]])
    tasks = json.loads(_run(capsys, "task", "list", "--mission", second["mission"]["id"], "--json"))
    assert tasks[1]["ordinal"] == 2 and tasks[1]["depends_on"] == [task["id"]] and tasks[1]["depends_on_ordinals"] == [1]
    events = json.loads(_run(capsys, "events", "--mission", second["mission"]["id"], "--json"))["events"]
    assert [event["task_ordinal"] for event in events if event["kind"] == "task-added"] == [1, 2]


def test_unknown_task_number_names_the_valid_range_without_long_ids(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.add_task(mid, "two")
        store.add_task(mid, "three")
    err = _run(capsys, "task", "show", "--mission", mid, "--task", "5", code=2)
    assert err.strip() == f"llm-bridge: mission {mid[:8]} has tasks 1–3; there is no task 5"
    assert not LONG_ID.search(err)
    other = _create(tmp_path, capsys, "other")["mission"]["id"]
    err = _run(capsys, "task", "show", "--mission", other, "--task", "2", code=2)
    assert err.strip() == f"llm-bridge: mission {other[:8]} has task 1; there is no task 2"
    assert main(["task", "cancel", "--mission", mid, "--task", "9"]) == 2
    assert "there is no task 9" in capsys.readouterr().err


def test_task_id_option_takes_the_global_id_and_rejects_foreign_tasks(tmp_path, state, capsys) -> None:
    first = _create(tmp_path, capsys, "first")
    second = _create(tmp_path, capsys, "second")
    global_id = second["task"]["id"]
    shown = _run(capsys, "task", "show", "--mission", second["mission"]["id"], "--task-id", str(global_id))
    assert shown.startswith('task 1 "second objective"')
    err = _run(capsys, "task", "show", "--mission", first["mission"]["id"], "--task-id", str(global_id), code=2)
    assert "belongs to another mission" in err and not LONG_ID.search(err)
    assert "no task has the global ID 99" in _run(capsys, "task", "show", "--mission", first["mission"]["id"], "--task-id", "99", code=2)
    with pytest.raises(SystemExit):
        main(["task", "show", "--mission", first["mission"]["id"], "--task", "1", "--task-id", "1"])
    capsys.readouterr()


def test_dependencies_and_steering_use_mission_task_numbers(tmp_path, state, capsys) -> None:
    _create(tmp_path, capsys, "first")
    mid = _create(tmp_path, capsys, "second")["mission"]["id"]
    _run(capsys, "task", "add", "--mission", mid, "--prompt", "later", "--depends-on", "1")
    tasks = json.loads(_run(capsys, "task", "list", "--mission", mid, "--json"))
    assert tasks[1]["depends_on_ordinals"] == [1]
    assert _run(capsys, "task", "dependencies", "--mission", mid, "--task", "1", "--add", "2", code=2)  # would be a cycle
    assert "there is no task 9" in _run(capsys, "task", "add", "--mission", mid, "--prompt", "x", "--depends-on", "9", code=2)
    review = json.loads(_run(capsys, "steer", "--mission", mid, "--kind", "review", "--task", "1", "--json"))
    assert review["result"]["depends_on_ordinal"] == 1 and review["result"]["task_ordinal"] == 3
    text = _run(capsys, "steer", "--mission", mid, "--kind", "review", "--task", "2")
    assert text.startswith("Steering review task 4 ")
    prompt = json.loads(_run(capsys, "task", "show", "--mission", mid, "--task", "3", "--json"))["prompt"]
    assert prompt.startswith("Review task 1 (")


def test_console_aliases_take_mission_task_numbers(tmp_path, state, capsys) -> None:
    _create(tmp_path, capsys, "first")
    created = _create(tmp_path, capsys, "second")
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    listing = dispatch_console_command(mid, "/task list")
    assert listing.splitlines()[0].split()[0] == "TASK" and "VERIFYING" in listing
    reply = json.loads(dispatch_console_command(mid, "/approve 1"))
    assert reply["status"] == "APPLIED" and reply["result"]["status"] == "COMPLETED" and reply["result"]["ordinal"] == 1
    bare = dispatch_console_command(mid, "/approve")
    assert bare == "error: approve needs a task number · no task awaits approval"
    assert dispatch_console_command(mid, "/reject 7 because").startswith(f"error: mission {mid[:8]} has task 1; there is no task 7")


def test_managed_context_names_the_mission_task_number(tmp_path, state) -> None:
    from llm_tools.bridge_runtime import BridgeMissionHooks

    with BridgeStore(state / "bridge.sqlite3") as store:
        store.create_mission("one", "objective one", cwd=_folder(tmp_path, "one"), initial_prompt="work one")
        second = store.create_mission("two", "objective two", cwd=_folder(tmp_path, "two"), initial_prompt="work two")
        store.set_mission_status(second["id"], "RUNNING")
        hook = BridgeMissionHooks(second["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hook.bind(store)
        context = hook.before_iteration()
    assert context["task_id"] == second["initial_task_id"] != 1
    assert "Task 1 (work two), revision 1" in context["prompt"]
    assert f"Task {second['initial_task_id']} " not in context["prompt"]


def test_schema_migration_numbers_existing_tasks_by_creation_within_each_mission(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "private" / "bridge.sqlite3"
    path.parent.mkdir(mode=0o700)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE missions(id TEXT PRIMARY KEY,name TEXT NOT NULL,objective TEXT NOT NULL,cwd TEXT NOT NULL,repo_identity TEXT NOT NULL,config_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,mission_id TEXT NOT NULL,title TEXT NOT NULL,original_prompt TEXT NOT NULL DEFAULT '',prompt TEXT NOT NULL,pending_json TEXT NOT NULL DEFAULT '{}',priority INTEGER NOT NULL DEFAULT 0,ordering INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'QUEUED',revision INTEGER NOT NULL DEFAULT 1,acceptance_json TEXT NOT NULL DEFAULT '[]',evidence_json TEXT NOT NULL DEFAULT '[]',origin TEXT NOT NULL DEFAULT 'operator',proposal_status TEXT NOT NULL DEFAULT 'NONE',proposed_by_operation TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            PRAGMA user_version=6;
            """
        )
        for mission in ("a", "b"):
            db.execute("INSERT INTO missions(id,name,objective,cwd,repo_identity,status,created_at,updated_at) VALUES(?,?,?,?,?,'CREATED','now','now')", (mission, mission, "o", str(tmp_path), str(tmp_path)))
        # Interleaved creation: a, b, a, b, b, a
        for mission in ("a", "b", "a", "b", "b", "a"):
            db.execute("INSERT INTO tasks(mission_id,title,prompt,created_at,updated_at) VALUES(?,?,?,'now','now')", (mission, mission, mission))
    with BridgeStore(path) as store:
        rows = [(r["mission_id"], r["id"], r["ordinal"]) for r in store.db.execute("SELECT mission_id,id,ordinal FROM tasks ORDER BY id")]
        assert rows == [("a", 1, 1), ("b", 2, 1), ("a", 3, 2), ("b", 4, 2), ("b", 5, 3), ("a", 6, 3)]
        assert store.task_by_ordinal("b", 3)["id"] == 5
        assert store.add_task("a", "next") == 7 and store.get_task(7)["ordinal"] == 4
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("UPDATE tasks SET ordinal=1 WHERE id=3")
    with BridgeStore(path) as store:  # opening again keeps the numbers
        assert [r["ordinal"] for r in store.db.execute("SELECT ordinal FROM tasks ORDER BY id")] == [1, 1, 2, 2, 3, 3, 4]
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


# ---------------------------------------------------------------- P1: verify, console text, config


def test_verify_without_approve_only_records_evidence(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    out = _run(capsys, "task", "verify", "--mission", mid, "--task", "1", "--evidence", "tests passed")
    assert out.strip() == "Evidence recorded · task 1 still awaits approval · llm-bridge task verify --task 1 --approve"
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_task(created["task"]["id"])["status"] == "VERIFYING"
    shown = _run(capsys, "task", "show", "--mission", mid, "--task", "1")
    assert "evidence:\n  - tests passed (operator" in shown and "item(s)" not in shown
    done = _run(capsys, "task", "verify", "--mission", mid, "--task", "1", "--approve")
    assert done.startswith('Verified · task 1 "work objective" · COMPLETED')
    assert "  - operator approved (approval" in _run(capsys, "task", "show", "--mission", mid, "--task", "1")
    err = _run(capsys, "task", "verify", "--mission", mid, "--task", "1", "--approve", code=2)
    assert "task 1 is completed, not awaiting verification" in err


def test_verify_with_evidence_and_approve_completes(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    assert "COMPLETED" in _run(capsys, "task", "verify", "--mission", mid, "--task", "1", "--approve", "--evidence", "reviewed")
    shown = _run(capsys, "task", "show", "--mission", mid, "--task", "1")
    assert "  - reviewed (operator" in shown and "  - operator approved" in shown


def test_verify_needs_evidence_or_approval(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    assert "requires verification evidence or explicit operator approval" in _run(capsys, "task", "verify", "--mission", mid, "--task", "1", code=2)


def test_console_verify_alias_takes_evidence_literally(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    reply = json.loads(dispatch_console_command(mid, "/verify 1 it's green: 12 passed"))
    assert reply["status"] == "APPLIED" and reply["result"]["status"] == "VERIFYING"
    assert reply["result"]["evidence"][0]["text"] == "it's green: 12 passed"


@pytest.mark.parametrize("reason", ["doesn't handle empty input", 'say "hello', "unbalanced ' quote", "plain words"])
def test_console_reject_takes_the_reason_literally(tmp_path, state, capsys, reason) -> None:
    created = _create(tmp_path, capsys, "work")
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    reply = json.loads(dispatch_console_command(mid, f"/reject 1 {reason}"))
    assert reply["status"] == "APPLIED", reply
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.latest_rejection_reason(mid, created["task"]["id"]) == reason


def test_console_reject_without_a_number_uses_the_single_waiting_task(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    reply = json.loads(dispatch_console_command(mid, "reject it's incomplete"))
    assert reply["status"] == "APPLIED"
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.latest_rejection_reason(mid, created["task"]["id"]) == "it's incomplete"


def test_console_steer_and_proposal_reject_take_free_text_literally(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    reply = json.loads(dispatch_console_command(mid, "steer replace 1 don't touch the \"config\" file"))
    assert reply["status"] == "APPLIED" and reply["result"]["status"] == "APPLIED"
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_task(created["task"]["id"])["prompt"] == 'don\'t touch the "config" file'
    assert json.loads(dispatch_console_command(mid, "steer pause-after-iteration"))["result"]["kind"] == "pause-after-iteration"
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        operation = store.begin_operation(created["task"]["id"], iteration=1, station="codex")
        proposal = store.propose_task(mid, "extra", source_operation_id=operation)
        ordinal = store.get_task(proposal)["ordinal"]
    rejected = json.loads(dispatch_console_command(mid, f"proposal reject {ordinal} it's out of scope"))
    assert rejected["status"] == "APPLIED"
    with BridgeStore(state / "bridge.sqlite3") as store:
        reasons = [e["data"].get("reason") for e in store.events(mid) if e["kind"] == "task-proposal-rejected"]
    assert reasons == ["it's out of scope"]


@pytest.mark.parametrize("line", ["/status", "/mission list", "/task list", "/events", "/rotation", "/doctor"])
def test_console_read_only_commands_print_text_not_json(tmp_path, state, capsys, line) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    text = dispatch_console_command(mid, line)
    assert not text.startswith(("{", "[")) and "frozen_tool_config" not in text and "{\n" not in text
    assert "error:" not in text


def test_console_status_matches_the_cli_and_json_is_opt_in(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    assert dispatch_console_command(mid, "/status") == _run(capsys, "status", "--mission", mid).strip()
    data = json.loads(dispatch_console_command(mid, "/status --json"))
    assert data["mission"]["id"] == mid and "frozen_tool_config" not in data["mission"]["config"]
    listing = json.loads(dispatch_console_command(mid, "/mission list --json"))
    assert "frozen_tool_config" not in json.dumps(listing)
    assert dispatch_console_command(mid, "/theme").splitlines()[0]
    assert not dispatch_console_command(mid, "/theme").startswith("{")


def test_console_task_show_is_human_text_and_json_on_request(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mission_id = created["mission"]["id"]
    assert not dispatch_console_command(mission_id, "task show --task 1").lstrip().startswith("{")
    record = json.loads(dispatch_console_command(mission_id, "task show --task 1 --json"))  # the task editor asks for JSON
    assert record["ordinal"] == 1 and record["prompt"]


def test_mission_json_leaves_out_the_saved_tool_config_unless_asked(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    assert "frozen_tool_config" not in created["mission"]["config"]
    listed = json.loads(_run(capsys, "mission", "list", "--json"))
    assert "frozen_tool_config" not in listed["missions"][0]["config"]
    with_config = json.loads(_run(capsys, "mission", "list", "--json", "--include-config"))
    assert "frozen_tool_config" in with_config["missions"][0]["config"]
    explicit = json.loads(_run(capsys, "mission", "create", "--prompt", "x", "--cwd", str(_folder(tmp_path, "x")), "--providers", "codex", "--json", "--include-config"))
    assert "frozen_tool_config" in explicit["mission"]["config"]
    with BridgeStore(state / "bridge.sqlite3") as store:  # the stored copy is untouched
        assert "frozen_tool_config" in store.get_mission(created["mission"]["id"])["config"]


# ---------------------------------------------------------------- P1: detach


class _FakeTmux:
    """Backend whose tmux calls answer list-clients from a table and record the rest."""

    FIELDS = ("client_name", "client_readonly", "client_control_mode", "client_width", "client_height", "client_activity")

    def __init__(self, table: str) -> None:
        import subprocess
        from types import SimpleNamespace

        from llm_tools.terminal import TmuxTerminalBackend

        rows = [line.split("\t") for line in table.splitlines()]
        calls = self.calls = []
        self.handle = SimpleNamespace(socket_path="/tmp/fake.sock")

        class Backend(TmuxTerminalBackend):
            def _require_owned(self, handle) -> None:
                return None

            def _run(self, socket_path, *args, **kwargs):
                calls.append(args)
                out = ""
                if args[0] == "list-clients":
                    template = args[args.index("-F") + 1]
                    for row in rows:
                        line = template
                        for field, value in zip(_FakeTmux.FIELDS, row):
                            line = line.replace("#{" + field + "}", value)
                        out += line + "\n"
                return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

        self.backend = Backend()


_CLIENTS = (
    "/dev/pts/18\t0\t0\t200\t50\t{recent}\n"
    "/dev/pts/21\t0\t0\t120\t40\t{old}\n"
    "/dev/pts/30\t1\t0\t80\t24\t{recent}\n"
    "/dev/pts/40\t0\t1\t80\t24\t{recent}\n"
)


def test_detach_with_several_clients_lists_each_one() -> None:
    import time

    now = int(time.time())
    fake = _FakeTmux(_CLIENTS.format(recent=now - 1, old=now - 600))
    with pytest.raises(RuntimeError) as raised:
        fake.backend.detach_client(fake.handle)
    message = str(raised.value)
    assert "multiple writable clients are attached; choose one with --client TTY:" in message
    assert "/dev/pts/18  200x50  active now" in message
    assert "/dev/pts/21  120x40  idle 10m" in message
    assert "/dev/pts/30  80x24  active now · read-only, never detached" in message
    assert "/dev/pts/40" not in message  # control-mode clients are not terminals
    assert not any(call[0] == "detach-client" for call in fake.calls)


def test_detach_picks_the_named_client_and_reports_it(tmp_path, state, capsys, monkeypatch) -> None:
    import time

    now = int(time.time())
    fake = _FakeTmux(_CLIENTS.format(recent=now, old=now - 5))
    mid = _create(tmp_path, capsys)["mission"]["id"]
    monkeypatch.setattr(bridge, "_load_handle", lambda _mission_id: fake.handle)
    monkeypatch.setattr("llm_tools.terminal.TmuxTerminalBackend", lambda: fake.backend)
    assert main(["detach", "--mission", mid, "--client", "/dev/pts/18"]) == 0
    assert capsys.readouterr().out.strip() == f"Detached /dev/pts/18 from mission {mid[:8]}"
    assert ("detach-client", "-t", "/dev/pts/18") in fake.calls
    assert main(["detach", "--mission", mid]) == 2
    assert "/dev/pts/21" in capsys.readouterr().err


def test_detach_with_one_writable_client_needs_no_option(tmp_path, state, capsys, monkeypatch) -> None:
    fake = _FakeTmux("/dev/pts/7\t0\t0\t100\t30\t1\n/dev/pts/8\t1\t0\t100\t30\t1\n")
    mid = _create(tmp_path, capsys)["mission"]["id"]
    monkeypatch.setattr(bridge, "_load_handle", lambda _mission_id: fake.handle)
    monkeypatch.setattr("llm_tools.terminal.TmuxTerminalBackend", lambda: fake.backend)
    assert main(["detach", "--mission", mid]) == 0
    assert capsys.readouterr().out.strip() == f"Detached /dev/pts/7 from mission {mid[:8]}"


# ---------------------------------------------------------------- P1: follow-ups on finished missions


def _completed_mission(tmp_path, state, capsys, name="done"):
    created = _create(tmp_path, capsys, name)
    mid, task = created["mission"]["id"], created["task"]["id"]
    _finish_first_task(state, mid, task)
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.verify_task(task, approved=True)
        store.set_mission_status(mid, "COMPLETED")
    return mid, task


def test_task_add_reopens_a_completed_mission(tmp_path, state, capsys) -> None:
    mid, _ = _completed_mission(tmp_path, state, capsys)
    out = _run(capsys, "task", "add", "--mission", mid, "--prompt", "follow-up work", "--title", "Follow-up")
    assert out.splitlines() == ['Added task 2 "Follow-up" · QUEUED · priority 0', "Mission reopened · start it with: llm-bridge start"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid)["status"] == "STOPPED"
        reopened = [e for e in store.events(mid) if e["kind"] == "mission-reopened"]
        assert reopened and reopened[0]["data"] == {"from": "COMPLETED", "reason": "task-added", "to": "STOPPED"}
        assert reopened[0]["task_ordinal"] == 2
        store.set_mission_status(mid, "RUNNING")  # start can continue it
    assert "Mission reopened" not in _run(capsys, "task", "add", "--mission", mid, "--prompt", "third")


def test_task_add_to_a_cancelled_mission_says_so(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    _run(capsys, "cancel", "--mission", mid)
    err = _run(capsys, "task", "add", "--mission", mid, "--prompt", "x", code=2)
    assert err.strip() == f"llm-bridge: mission {mid[:8]} was cancelled; create a new mission instead of adding a task"
    assert "terminal mission" not in err


def test_read_only_commands_use_the_folders_finished_mission(tmp_path, state, capsys, monkeypatch) -> None:
    mid, _ = _completed_mission(tmp_path, state, capsys)
    monkeypatch.chdir(tmp_path / "done")
    assert "COMPLETED" in _run(capsys, "status")
    assert "VERIFYING" not in _run(capsys, "task", "list") and "COMPLETED" in _run(capsys, "task", "list")
    assert _run(capsys, "task", "show", "--task", "1").startswith("task 1 ")
    assert "mission-created" in _run(capsys, "events")
    # A command that changes things keeps the rule and says how to continue.
    err = _run(capsys, "task", "add", "--prompt", "more", code=2)
    assert f"mission {mid[:8]} in folder" in err and "is finished (completed)" in err
    assert f"llm-bridge task add --mission {mid[:8]}" in err and not LONG_ID.search(err)
    assert "terminal mission" not in err


def test_several_finished_missions_in_a_folder_are_listed(tmp_path, state, capsys, monkeypatch) -> None:
    folder = _folder(tmp_path, "shared")
    for index in range(2):
        out = json.loads(_run(capsys, "mission", "create", "--prompt", f"p{index}", "--cwd", str(folder), "--providers", "codex", "--json"))
        _run(capsys, "cancel", "--mission", out["mission"]["id"])
    monkeypatch.chdir(folder)
    err = _run(capsys, "status", code=2)
    assert "several finished missions match folder" in err


# ---------------------------------------------------------------- P1: needs-you hint, controller wording


def _status_snapshot(state, mid):
    return bridge.status_snapshot(mid)


def test_status_says_what_needs_the_operator(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    text = _run(capsys, "status", "--mission", mid)
    assert "Needs you   task 1 awaits approval · llm-bridge task verify --task 1 --approve (or reject: llm-bridge task reject --task 1 --reason …)" in text
    snapshot = _status_snapshot(state, mid)
    assert snapshot["needs_you"]["kind"] == "approval" and snapshot["needs_you"]["tasks"] == [1]
    assert snapshot["needs_you"]["command"] == "llm-bridge task verify --task 1 --approve"
    _run(capsys, "task", "verify", "--mission", mid, "--task", "1", "--approve")
    assert "Needs you" not in _run(capsys, "status", "--mission", mid)
    assert _status_snapshot(state, mid)["needs_you"] is None


def test_status_lists_several_tasks_awaiting_approval(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        second = store.add_task(mid, "second")
        store.set_mission_status(mid, "RUNNING")
        for task in (created["task"]["id"], second):
            op = store.begin_operation(task, iteration=1, station="codex")
            store.finish_operation(op, exit_code=0)
    assert "Needs you   tasks 1, 2 await approval" in _run(capsys, "status", "--mission", mid)


def test_needs_attention_covers_failures_blocks_proposals_and_capacity() -> None:
    base = {"mission": {"id": "a" * 32, "status": "RUNNING"}, "tasks": [], "lifecycle": {}}
    failed_mission = bridge_format.needs_attention({**base, "mission": {"id": "a" * 32, "status": "FAILED"}, "lifecycle": {"failure_reason": "tmux missing"}})
    assert failed_mission["kind"] == "failed" and "tmux missing" in failed_mission["message"] and "llm-bridge events --mission aaaaaaaa" in failed_mission["message"]
    failed_task = bridge_format.needs_attention({**base, "current_task": {"id": 9, "ordinal": 3, "status": "FAILED"}})
    assert failed_task["kind"] == "failed" and failed_task["command"] == "llm-bridge task retry --task 3"
    blocked = bridge_format.needs_attention({**base, "current_task": {"id": 9, "ordinal": 3, "status": "BLOCKED"}})
    assert blocked["kind"] == "blocked" and "task 3 is blocked" in blocked["message"]
    proposals = bridge_format.needs_attention({**base, "proposal_counts": {"PENDING": 2}})
    assert proposals["kind"] == "proposal" and proposals["message"].startswith("2 proposed tasks await your decision")
    waiting = bridge_format.needs_attention({**base, "waiting": {"reason": "capacity", "entries": [{"name": "codex", "reset_epoch": 4000}], "until_epoch": 4000}})
    assert waiting["kind"] == "capacity" and "waiting for provider capacity" in waiting["message"]
    assert bridge_format.needs_attention(base) is None
    # Capacity waits keep their own Waiting line; Needs you is not repeated for them.
    text = bridge_format.format_status({**base, "mission": {**base["mission"], "name": "n", "cwd": "/x", "config": {}}, "waiting": waiting_entry(), "needs_you": waiting})
    assert "Needs you" not in text and "Waiting     for capacity" in text


def waiting_entry():
    return {"reason": "capacity", "entries": [{"name": "codex", "reset_epoch": 4000}]}


def test_running_controller_reads_active_with_heartbeat_age() -> None:
    snapshot = {
        "mission": {"id": "b" * 32, "name": "n", "status": "RUNNING", "cwd": "/x", "config": {"native_tui": True}},
        "lifecycle": {"controller": "unknown", "controller_running": True, "heartbeat": {"age_seconds": 3, "reported_state": "ACTIVE"}},
        "tasks": [], "task_counts": {},
    }
    assert "Controller  active · heartbeat 3s ago" in bridge_format.format_status(snapshot)
    assert "unknown" not in bridge_format.format_status(snapshot) and "ACTIVE" not in bridge_format.format_status(snapshot)
    snapshot["lifecycle"]["controller_running"] = None
    snapshot["lifecycle"]["controller"] = "RUNNING"
    assert "Controller  RUNNING · heartbeat active 3s ago" in bridge_format.format_status(snapshot)


# ---------------------------------------------------------------- P1: attach and mission ordering


def _folder_missions(tmp_path, capsys, count=3):
    folder = _folder(tmp_path, "shared")
    ids = []
    for index in range(count):
        out = json.loads(_run(capsys, "mission", "create", "--prompt", f"p{index}", "--name", f"m{index}", "--cwd", str(folder), "--providers", "codex", "--json"))
        ids.append(out["mission"]["id"])
    return folder, ids


def _touch(state, mission_id, stamp):
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.db.execute("UPDATE missions SET updated_at=? WHERE id=?", (stamp, mission_id))


def test_attach_prefers_the_single_mission_with_a_live_station(tmp_path, state, capsys, monkeypatch) -> None:
    folder, ids = _folder_missions(tmp_path, capsys)
    live = ids[0]
    monkeypatch.setattr(bridge_missions, "station_live", lambda mission_id: mission_id == live)
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert bridge.resolve_mission_ref(store, None, cwd=folder, prefer_station=True) == live
        with pytest.raises(ValueError, match="several missions match folder"):
            bridge.resolve_mission_ref(store, None, cwd=folder)
    monkeypatch.chdir(folder)
    opened = []
    from llm_tools import terminal

    monkeypatch.setattr(bridge, "_load_handle", lambda mission_id: opened.append(mission_id) or object())
    monkeypatch.setattr(terminal.TmuxTerminalBackend, "attach", lambda self, handle: 0)
    assert main(["attach"]) == 0 and opened == [live]


def test_attach_is_ambiguous_only_among_live_stations(tmp_path, state, capsys, monkeypatch) -> None:
    folder, ids = _folder_missions(tmp_path, capsys)
    monkeypatch.setattr(bridge_missions, "station_live", lambda mission_id: mission_id in ids[:2])
    monkeypatch.chdir(folder)
    err = _run(capsys, "attach", code=2)
    assert "several missions match folder" in err
    assert ids[0][:8] in err and ids[1][:8] in err and ids[2][:8] not in err


def test_mission_list_and_ambiguity_errors_share_one_order(tmp_path, state, capsys, monkeypatch) -> None:
    folder, ids = _folder_missions(tmp_path, capsys)
    for index, mission_id in enumerate(ids):
        _touch(state, mission_id, f"2026-01-0{index + 1}T00:00:00+00:00")  # ids[2] is the newest
    monkeypatch.setattr(bridge_missions, "station_live", lambda mission_id: mission_id == ids[0])  # the oldest is live
    expected = [ids[0], ids[2], ids[1]]  # live first, then newest first
    listed = json.loads(_run(capsys, "mission", "list", "--json"))["missions"]
    assert [m["id"] for m in listed] == expected
    human = _run(capsys, "mission", "list").splitlines()[1:]
    assert [line.split()[0] for line in human] == [i[:8] for i in expected]
    monkeypatch.chdir(folder)
    err = _run(capsys, "status", code=2)
    assert [line.split()[0] for line in err.splitlines()[1:]] == [i[:8] for i in expected]
    assert "(live)" in err.splitlines()[1] and "(live)" not in err.splitlines()[2]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(ids[2], "CANCELLED")
    ordered = [m["id"] for m in json.loads(_run(capsys, "mission", "list", "--json"))["missions"]]
    assert ordered == [ids[0], ids[1], ids[2]]  # finished missions sort last


# ---------------------------------------------------------------- P1: amend


def test_amend_of_a_not_running_task_applies_immediately(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _run(capsys, "task", "add", "--mission", mid, "--prompt", "second")
    _run(capsys, "task", "amend", "--mission", mid, "--task", "2", "--prompt", "second v2")
    out = _run(capsys, "task", "amend", "--mission", mid, "--task", "2", "--prompt", "second v3")
    assert out.strip() == "Amended · task 2 is now revision 3"
    shown = _run(capsys, "task", "show", "--mission", mid, "--task", "2")
    assert "second v3" in shown and "revision 3" in shown and "pending amendment" not in shown
    with BridgeStore(state / "bridge.sqlite3") as store:
        task = store.get_task(created["task"]["id"] + 1)
        assert task["prompt"] == "second v3" and task["pending"] == {} and task["original_prompt"] == "second"
        applied = [e for e in store.events(mid) if e["kind"] == "task-amendment-applied"]
        assert [e["data"]["revision"] for e in applied] == [2, 3] and applied[0]["data"]["reason"] == "no-active-step"
        statuses = [r["status"] for r in store.db.execute("SELECT status FROM amendments WHERE task_id=?", (task["id"],))]
        assert statuses == ["APPLIED", "APPLIED"]


def test_amend_of_a_running_task_stays_pending_and_task_show_says_so(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        store.begin_operation(created["task"]["id"], iteration=1, station="codex")
    out = _run(capsys, "task", "amend", "--mission", mid, "--task", "1", "--prompt", "sharper instructions")
    assert out.strip() == "Amended · task 1 is now revision 2 · applies after the current step"
    shown = _run(capsys, "task", "show", "--mission", mid, "--task", "1")
    assert "pending amendment (applies after the current step): sharper instructions" in shown
    assert "work objective" in shown  # the running text stays visible until it is applied
    reply = json.loads(_run(capsys, "steer", "--mission", mid, "--kind", "replace", "--task", "1", "--prompt", "again", "--json"))
    assert reply["result"]["status"] == "QUEUED_FOR_BOUNDARY"


def test_amend_of_a_verifying_task_keeps_its_requeue_behaviour(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    _run(capsys, "task", "amend", "--mission", mid, "--task", "1", "--prompt", "redo it")
    with BridgeStore(state / "bridge.sqlite3") as store:
        task = store.get_task(created["task"]["id"])
        assert task["status"] == "READY" and task["prompt"] == "redo it" and task["pending"] == {}


# ---------------------------------------------------------------- P2: wording


def test_help_with_an_unknown_topic_fails_and_lists_topics(capsys) -> None:
    assert main(["help", "nonsense"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("llm-bridge: Unknown help topic 'nonsense' · topics: ")
    assert "quickstart" in err and "keys" in err
    assert main(["help", "task", "verify"]) == 0
    assert "--approve" in capsys.readouterr().out
    assert main(["help", "keys"]) == 0
    capsys.readouterr()


def test_console_help_with_an_unknown_topic_is_an_error(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    assert dispatch_console_command(mid, "help nonsense").startswith("error: Unknown help topic 'nonsense' · topics: ")
    assert dispatch_console_command(mid, "help keys").startswith("Bridge keys") or "Ctrl" in dispatch_console_command(mid, "help keys")


def test_pause_of_an_unstarted_mission_is_not_a_raw_transition_error(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    out = _run(capsys, "pause", "--mission", mid)
    assert out.strip() == "Nothing to pause · mission is not running (CREATED)"
    assert "invalid mission transition" not in out
    reply = json.loads(dispatch_console_command(mid, "/pause"))
    assert reply["status"] == "APPLIED" and reply["result"]["detail"] == "Nothing to pause · mission is not running (CREATED)"
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid)["status"] == "CREATED"
    _run(capsys, "cancel", "--mission", mid)
    assert "Nothing to pause · mission is not running (CANCELLED)" in _run(capsys, "pause", "--mission", mid)


def test_refused_transitions_use_operator_wording(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _finish_first_task(state, mid, created["task"]["id"])
    _run(capsys, "task", "verify", "--mission", mid, "--task", "1", "--approve")
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "COMPLETED")
    err = _run(capsys, "resume", "--mission", mid, code=2)
    assert err.strip() == f'llm-bridge: mission {mid[:8]} is finished (completed); add a task with: llm-bridge task add --prompt "..." to continue it'
    err = _run(capsys, "task", "retry", "--mission", mid, "--task", "1", code=2)
    assert err.strip() == "llm-bridge: task 1 is completed and cannot become queued"
    err = _run(capsys, "task", "reject", "--mission", mid, "--task", "1", "--reason", "x", code=2)
    assert "task 1 is completed, not awaiting verification" in err
    for message in (err, ):
        assert "->" not in message and "invalid" not in message


def test_mission_create_names_a_missing_folder(tmp_path, state, capsys) -> None:
    err = _run(capsys, "mission", "create", "--prompt", "x", "--cwd", "/nonexistent/dir", code=2)
    assert err.strip() == "llm-bridge: Folder does not exist: /nonexistent/dir"
    plain = tmp_path / "file.txt"
    plain.write_text("x")
    assert _run(capsys, "mission", "create", "--prompt", "x", "--cwd", str(plain), code=2).strip() == f"llm-bridge: Not a folder: {plain}"
    assert main(["--prompt", "legacy", "--cwd", "/nonexistent/dir", "--no-start"]) == 2
    assert "Folder does not exist: /nonexistent/dir" in capsys.readouterr().err
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.list_missions() == []


# ---------------------------------------------------------------- P2: doctor


def _install(directory: Path, *names: str) -> None:
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / name).write_text("#!/bin/sh\n")
        (directory / name).chmod(0o755)


def test_doctor_reports_each_rotation_provider(tmp_path, state, capsys, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "x"}}))
    bin_dir = tmp_path / "bin"
    _install(bin_dir, "codex", "copilot", "tmux")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(bridge, "default_rotation", lambda: ("codex,claude,copilot", ""))
    text = _run(capsys, "doctor")
    assert "Providers        codex: installed, logged in" in text
    assert "\n                 claude: not on PATH" in text
    assert "\n                 copilot: installed, not logged in (run: copilot login)" in text
    report = json.loads(_run(capsys, "doctor", "--json"))
    by_name = {item["name"]: item for item in report["providers"]}
    assert by_name["codex"]["installed"] is True and by_name["codex"]["logged_in"] is True
    assert by_name["claude"]["installed"] is False and by_name["claude"]["logged_in"] is None
    assert by_name["copilot"]["logged_in"] is False


def test_doctor_login_checks_read_only_local_credential_files(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_doctor

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").write_text("github.com:\n    user: someone\n")
    (home / ".local" / "share" / "kilo").mkdir(parents=True)
    (home / ".local" / "share" / "kilo" / "auth.json").write_text("{}")
    bin_dir = tmp_path / "bin"
    _install(bin_dir, "claude", "copilot", "kilo", "mmx", "opencode")
    env = {"HOME": str(home), "PATH": str(bin_dir)}
    assert bridge_doctor.provider_check("claude", env=env)["logged_in"] is True
    assert bridge_doctor.provider_check("copilot", env=env)["logged_in"] is True
    assert bridge_doctor.provider_check("copilot", env={**env, "HOME": str(tmp_path / "nobody")})["logged_in"] is False
    assert bridge_doctor.provider_check("copilot", env={**env, "HOME": str(tmp_path / "nobody"), "GH_TOKEN": "t"})["logged_in"] is True
    assert bridge_doctor.provider_check("kilo", env=env)["logged_in"] is False  # empty auth store
    assert bridge_doctor.provider_check("opencode", env=env)["logged_in"] is False
    assert bridge_doctor.provider_check("minimax", env=env) | {"path": None} == {"name": "minimax", "launch_cli": "minimax", "installed": True, "path": None, "logged_in": None, "detail": ""}
    assert bridge_doctor.provider_check("claude", env={**env, "HOME": str(tmp_path / "nobody")})["logged_in"] is False
    assert bridge_doctor.provider_check("claude", env={**env, "HOME": str(tmp_path / "nobody"), "CLAUDE_CODE_OAUTH_TOKEN": "t"})["logged_in"] is True
    assert bridge_doctor.provider_check("zai", env=env)["installed"] is None
    checks = bridge_doctor.provider_checks([("claude", None), ("claude", None), ("copilot", None)], env)
    assert [c["name"] for c in checks] == ["claude", "copilot"]
    assert bridge_format.provider_check_text({"name": "kilo", "installed": True, "logged_in": None}) == "kilo: installed, login not checked"
    assert bridge_format.provider_check_text({"name": "zai", "installed": None, "detail": "capacity only; launched through a route"}) == "zai: capacity only; launched through a route"
    assert bridge_format.provider_check_text({"name": "x", "installed": True, "logged_in": False, "detail": ""}) == "x: installed, not logged in"


def test_doctor_for_a_mission_checks_the_missions_own_rotation(tmp_path, state, capsys, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    _install(bin_dir, "codex")
    monkeypatch.setenv("PATH", str(bin_dir))
    mid = _create(tmp_path, capsys, "doc")["mission"]["id"]
    report = json.loads(_run(capsys, "doctor", "--mission", mid, "--json"))
    assert [item["name"] for item in report["providers"]] == ["codex"]
    assert "frozen_tool_config" not in json.dumps(report)


def test_doctor_checks_a_route_through_its_launch_cli(tmp_path, state, capsys, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    _install(bin_dir, "kilo")
    monkeypatch.setenv("PATH", str(bin_dir))
    config = tmp_path / "routes.toml"
    config.write_text('[routes.kilo-fast]\nprovider = "kilo"\nmodel = "kilo/x"\n[routes.kilo-fast.capacity]\npolicy = "opaque"\n')
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(config))
    monkeypatch.setattr(bridge, "default_rotation", lambda: ("claude", "kilo-fast"))
    report = json.loads(_run(capsys, "doctor", "--json"))
    assert [(c["name"], c["launch_cli"], c["installed"]) for c in report["providers"]] == [("kilo-fast", "kilo", True)]


def test_missions_listing_shows_the_mission_task_number(tmp_path, state, capsys) -> None:
    _create(tmp_path, capsys, "first")
    second = _create(tmp_path, capsys, "second")
    mid = second["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        store.begin_operation(second["task"]["id"], iteration=1, station="codex")
    entries = json.loads(_run(capsys, "missions", "--json"))
    current = next(item for item in entries["missions"] if item["id"] == mid)["current_task"]
    assert current["ordinal"] == 1 and current["id"] == second["task"]["id"] != 1
    row = next(line for line in _run(capsys, "missions").splitlines() if mid[:8] in line)
    assert row.rstrip().endswith(" 1 second objective")
