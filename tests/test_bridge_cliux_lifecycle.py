"""CLIUX lifecycle findings: 024, 025, 026, 036, 043, 056, 057, 063, 064, 070, 075, 077, 078, 079."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_doctor, bridge_format, bridge_missions
from llm_tools.bridge import main
from llm_tools.bridge_help import topic_text
from llm_tools.bridge_store import BridgeStore, derive_mission_name


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    monkeypatch.setattr(bridge, "controller_running", lambda _mission_id: False)
    return directory


def _run(capsys, *argv: str, code: int = 0) -> str:
    assert main(list(argv)) == code, capsys.readouterr()
    captured = capsys.readouterr()
    return captured.out if code == 0 else captured.err


def _create(tmp_path: Path, capsys, name: str = "work", *extra: str) -> dict:
    folder = tmp_path / name
    folder.mkdir()
    out = _run(capsys, "mission", "create", "--prompt", f"{name} objective", "--name", name, "--cwd", str(folder), "--providers", "codex", *extra, "--json")
    return json.loads(out)


def _awaiting_review(state: Path, mission_id: str, task_id: int) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mission_id, "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
        assert store.get_task(task_id)["status"] == "VERIFYING"


def _status(state: Path, mission_id: str) -> str:
    with BridgeStore(state / "bridge.sqlite3") as store:
        return str(store.get_mission(mission_id)["status"])


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


# ---------------------------------------------------------------- CLIUX-024: cancel is guarded, repeats are named


def test_cancel_without_a_terminal_needs_yes_and_changes_nothing(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    err = _run(capsys, "cancel", "--mission", mid, code=2)
    assert "This cannot be undone" in err and f"llm-bridge cancel --mission {mid[:8]} --yes" in err
    assert _status(state, mid) == "CREATED"
    assert "Cancel requested" in _run(capsys, "cancel", "--mission", mid, "--yes")
    assert _status(state, mid) == "CANCELLED"


def test_cancel_asks_on_a_terminal_and_keeps_the_mission_unless_confirmed(tmp_path, state, capsys, monkeypatch) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(sys, "stderr", _Tty())
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert main(["cancel", "--mission", mid]) == 2
    prompt = sys.stderr.getvalue()  # type: ignore[attr-defined]
    assert "This cannot be undone" in prompt and "Kept mission" in prompt
    assert _status(state, mid) == "CREATED"
    assert main(["cancel", "--mission", mid]) == 0
    assert _status(state, mid) == "CANCELLED"


def test_cancelling_a_finished_mission_does_not_ask(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    _run(capsys, "cancel", "--mission", mid, "--yes")
    assert main(["cancel", "--mission", mid]) == 0  # already cancelled: nothing left to confirm
    capsys.readouterr()


def test_console_cancel_does_not_ask_twice(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    reply = json.loads(bridge.dispatch_console_command(mid, "/cancel"))  # the console's own dialog already asked
    assert reply["status"] == "APPLIED"
    assert _status(state, mid) == "CANCELLED"


def test_repeated_pause_and_stop_say_the_mission_is_already_paused_or_stopped(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _awaiting_review(state, mid, created["task"]["id"])
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "PAUSED")
    assert _run(capsys, "pause", "--mission", mid).startswith("Already paused")
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        store.set_mission_status(mid, "STOPPED")
    assert _run(capsys, "stop", "--mission", mid).startswith("Already stopped")


def test_quickstart_has_a_stopping_work_table() -> None:
    text = topic_text("quickstart")
    assert "Stopping work" in text
    for command in ("pause", "stop --force", "finish", "task cancel", "cancel"):
        assert any(line.strip().startswith(command + " ") for line in text.splitlines()), command
    assert "cannot be undone" in text and "--yes" in text


# ---------------------------------------------------------------- CLIUX-025: a rejected task runs again, or is held


def test_reject_says_it_runs_again_and_hold_keeps_the_task_paused(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid, task_id = created["mission"]["id"], created["task"]["id"]
    _awaiting_review(state, mid, task_id)
    out = _run(capsys, "task", "reject", "--mission", mid, "--task", "1", "--reason", "needs edge cases", "--hold")
    assert out.startswith('Sent back and held · task 1 "work objective"')
    assert f"llm-bridge task resume --mission {mid[:8]} --task 1" in out
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_task(task_id)["status"] == "PAUSED"


def test_reject_without_hold_names_the_paid_retry(tmp_path, state, capsys, monkeypatch) -> None:
    created = _create(tmp_path, capsys)
    mid, task_id = created["mission"]["id"], created["task"]["id"]
    _awaiting_review(state, mid, task_id)
    monkeypatch.setattr(bridge, "controller_running", lambda _mission_id: True)
    out = _run(capsys, "task", "reject", "--mission", mid, "--task", "1", "--reason", "needs edge cases")
    assert "will run again now (uses capacity)" in out and f"to hold it instead: llm-bridge task pause --mission {mid[:8]} --task 1" in out
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_task(task_id)["status"] == "QUEUED"


# ---------------------------------------------------------------- CLIUX-026: amending a task in review needs --requeue


def test_amend_of_a_task_in_review_is_refused_without_requeue(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid, task_id = created["mission"]["id"], created["task"]["id"]
    _awaiting_review(state, mid, task_id)
    err = _run(capsys, "task", "amend", "--mission", mid, "--task", "1", "--prompt", "Write parser tests incl. edge cases", code=2)
    assert "task 1 is awaiting review" in err and "--requeue" in err and "task approve" in err
    with BridgeStore(state / "bridge.sqlite3") as store:
        task = store.get_task(task_id)
        assert task["status"] == "VERIFYING" and task["revision"] == 1 and task["prompt"] == "work objective"


def test_amend_with_requeue_sends_the_result_back_and_updates_a_derived_title(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid, task_id = created["mission"]["id"], created["task"]["id"]
    _awaiting_review(state, mid, task_id)
    out = _run(capsys, "task", "amend", "--mission", mid, "--task", "1", "--prompt", "Write parser tests incl. edge cases", "--requeue")
    assert out.startswith("Amended · task 1 is now revision 2 · the earlier result was sent back; revision 2 runs when the mission starts (uses capacity)")
    with BridgeStore(state / "bridge.sqlite3") as store:
        task = store.get_task(task_id)
        assert task["status"] == "READY" and task["prompt"].startswith("Write parser tests")
        assert task["title"] == "Write parser tests incl. edge cases"  # the title followed the first prompt line


def test_amend_keeps_a_title_the_operator_chose(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid, task_id = created["mission"]["id"], created["task"]["id"]
    _run(capsys, "task", "add", "--mission", mid, "--prompt", "second prompt", "--title", "Chosen title")
    _run(capsys, "task", "amend", "--mission", mid, "--task", "2", "--prompt", "different prompt")
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.list_tasks(mid)[1]["title"] == "Chosen title"


# ---------------------------------------------------------------- CLIUX-036: doctor --mission points at the mission's own step


def test_doctor_next_step_for_a_mission_is_its_needs_you_hint(tmp_path, state, capsys, monkeypatch) -> None:
    created = _create(tmp_path, capsys)
    mid, task_id = created["mission"]["id"], created["task"]["id"]
    _awaiting_review(state, mid, task_id)
    ready = [{"name": "codex", "installed": True, "logged_in": True, "launch_cli": "codex"}]
    monkeypatch.setattr(bridge, "_doctor_provider_checks", lambda _mission: ready)
    text = _run(capsys, "doctor", "--mission", mid)
    next_line = next(line for line in text.splitlines() if line.startswith("Next"))
    assert "task 1 awaits your review" in next_line and f"llm-bridge task approve --mission {mid[:8]} --task 1" in next_line
    assert "press N" not in text


def test_doctor_next_steps_keep_the_generic_advice_without_a_mission_step() -> None:
    ready = [{"name": "codex", "installed": True, "logged_in": True, "launch_cli": "codex"}]
    assert "press N" in bridge_doctor.next_steps(tmux_available=True, providers=ready, folder="/x")[0]
    assert bridge_doctor.next_steps(tmux_available=True, providers=ready, folder="/x", needs_you="do the thing")[0] == "do the thing"


# ---------------------------------------------------------------- CLIUX-043: mission names and the creation receipt


@pytest.mark.parametrize("prompt,expected", [
    ("Write tests for the parser", "Write tests for the parser"),
    ("Create a file named hello.txt containing the word hello, then stop.", "Create a file named hello.txt…"),
    ("# Heading line is stripped\nsecond line", "Heading line is stripped"),
    ("x" * 80, "x" * 39 + "…"),
    ("", "fallback"),
])
def test_default_mission_name_is_the_first_line_cut_at_a_word_boundary(prompt, expected) -> None:
    name = derive_mission_name(prompt, "fallback")
    assert name == expected and len(name) <= 40


def test_creation_receipt_names_the_folder_and_a_long_prompt_gets_a_short_name(tmp_path, state, capsys) -> None:
    folder = tmp_path / "parser"
    folder.mkdir()
    out = _run(capsys, "mission", "create", "--prompt", "Create a file named hello.txt containing the word hello, then stop.", "--cwd", str(folder), "--providers", "codex")
    assert f'"Create a file named hello.txt…" in {folder} · task 1' in out


# ---------------------------------------------------------------- CLIUX-056 and CLIUX-057: task list shows the real run order


def _task_rows(text: str) -> list[list[str]]:
    lines = text.splitlines()
    return [line.split() for line in lines[1:] if line and line[0].isdigit()]


def test_task_list_follows_the_scheduler_order_and_shows_where_each_task_runs(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _run(capsys, "task", "add", "--mission", mid, "--prompt", "second", "--title", "Second")
    _run(capsys, "task", "add", "--mission", mid, "--prompt", "third", "--title", "Third")
    _run(capsys, "task", "priority", "--mission", mid, "--task", "3", "--value", "-5")
    _run(capsys, "task", "reorder", "--mission", mid, "--task", "1", "--value", "99")
    text = _run(capsys, "task", "list", "--mission", mid)
    assert text.splitlines()[0].split() == ["TASK", "STATUS", "RUNS", "PRIORITY", "ORDER", "AFTER", "TITLE"]
    rows = _task_rows(text)
    # The controller runs priority 0 tasks by ordering (task 2, then task 1 at 99), then the priority -5 task.
    assert [row[0] for row in rows] == ["2", "1", "3"]
    assert [row[2] for row in rows] == ["1", "2", "3"]
    assert "higher number runs first" in text and "lower number runs first" in text


def test_finished_tasks_come_last_in_the_task_list(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _run(capsys, "task", "add", "--mission", mid, "--prompt", "second", "--title", "Second")
    _run(capsys, "task", "cancel", "--mission", mid, "--task", "1")
    rows = _task_rows(_run(capsys, "task", "list", "--mission", mid))
    assert [row[0] for row in rows] == ["2", "1"]
    assert rows[1][1] == "CANCELLED" and rows[1][2] == "0"  # a finished task has no place in the run order


def test_task_list_json_orders_like_the_table_and_carries_the_run_position(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _run(capsys, "task", "add", "--mission", mid, "--prompt", "second", "--title", "Second", "--priority", "2")
    tasks = json.loads(_run(capsys, "task", "list", "--mission", mid, "--json"))
    assert [(task["ordinal"], task["run_position"]) for task in tasks] == [(2, 1), (1, 2)]


def test_priority_and_reorder_receipts_say_where_the_task_now_runs(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _run(capsys, "task", "add", "--mission", mid, "--prompt", "second", "--title", "Second")
    _run(capsys, "task", "add", "--mission", mid, "--prompt", "third", "--title", "Third")
    assert _run(capsys, "task", "priority", "--mission", mid, "--task", "3", "--value", "-5").strip() == "Priority of task 3 set to -5 · runs after tasks 1 and 2"
    assert _run(capsys, "task", "priority", "--mission", mid, "--task", "3", "--value", "4").strip() == "Priority of task 3 set to 4 · runs next"
    assert _run(capsys, "task", "reorder", "--mission", mid, "--task", "1", "--value", "99").strip() == "Order of task 1 set to 99 · runs after tasks 3 and 2"


def test_help_states_the_direction_of_priority_and_order(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["task", "priority", "--help"])
    priority = capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["task", "reorder", "--help"])
    order = capsys.readouterr().out
    assert "HIGHER number runs first" in " ".join(priority.split()) and "LOWER number runs first" in " ".join(order.split())


def test_console_task_list_says_prio_not_p_levels() -> None:
    from llm_tools import bridge_tui

    rows = bridge_tui._order_list_rows([{"id": 4, "ordinal": 4, "status": "queued", "title": "Update", "priority": 2}])
    assert "prio 2" in rows[0] and " P2 " not in rows[0]


# ---------------------------------------------------------------- CLIUX-063: station commands in a finished mission's folder


@pytest.mark.parametrize("command", [["attach"], ["detach"], ["finish"], ["station", "claude"]])
def test_station_commands_in_a_finished_folder_say_there_is_no_station(tmp_path, state, capsys, monkeypatch, command) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _run(capsys, "cancel", "--mission", mid, "--yes")
    monkeypatch.chdir(tmp_path / "work")
    err = _run(capsys, *command, code=2)
    assert f"mission {mid[:8]}" in err and "has no station" in err and "llm-bridge status shows its result" in err
    assert "follow-up" not in err


# ---------------------------------------------------------------- CLIUX-064: Bridge's own exit statuses are shown as words


@pytest.mark.parametrize("code,words", [
    (75, "Provider stopped by Bridge (no progress, or waiting for input)"),
    (76, "Provider stopped (you declined the folder-trust prompt)"),
    (130, "Provider interrupted"),
    (143, "Provider interrupted"),
])
def test_synthetic_exit_statuses_read_as_words_and_keep_their_number_only_in_events(code, words) -> None:
    event = {"id": 7, "kind": "operation-exited", "created_at": "2026-01-01T00:00:00+00:00", "data": {"exit_code": code, "operation_id": "op", "status": "EXITED"}, "task_ordinal": 6}
    assert bridge_format.event_phrase(event).startswith(words) and str(code) not in bridge_format.event_phrase(event)
    assert bridge_format.event_phrase(event, code=True).endswith(f"(exit {code})")
    assert f"(exit {code})" in bridge_format.format_events([event])


def test_ordinary_exit_codes_keep_their_number_everywhere() -> None:
    event = {"id": 7, "kind": "operation-exited", "created_at": "2026-01-01T00:00:00+00:00", "data": {"exit_code": 1}, "task_ordinal": 6}
    assert bridge_format.event_phrase(event).startswith("Provider exited (1)")
    assert bridge_format.event_phrase(event, code=True).count("(1)") == 1
    assert bridge_format.exit_short(1) == "exit 1" and bridge_format.exit_short(75) == "stopped by Bridge"
    assert bridge_format.exit_short("-") == "exit -"


def test_providers_view_and_last_run_use_the_words() -> None:
    from llm_tools import bridge_tui

    segments = bridge_tui._recent_operation_segments({"task_ordinal": 6, "status": "EXITED", "exit_code": 75}, "20m ago")
    text = "".join(part for part, _role in segments)
    assert "stopped by Bridge" in text and "75" not in text


# ---------------------------------------------------------------- CLIUX-070 and CLIUX-078: status and rotation already behave


def test_status_never_ends_with_an_empty_capacity_heading(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    _awaiting_review(state, mid, created["task"]["id"])
    _run(capsys, "task", "approve", "--mission", mid, "--task", "1")
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "COMPLETED")
    lines = _run(capsys, "status", "--mission", mid).splitlines()
    assert "Capacity" not in [line.strip() for line in lines[-1:]]
    assert all(not (line == "Capacity" and (index + 1 == len(lines) or not lines[index + 1].startswith(" "))) for index, line in enumerate(lines))


def test_status_prints_the_whole_mission_name(tmp_path, state, capsys) -> None:
    name = "A mission name that is much longer than forty characters and still complete"
    created = _create(tmp_path, capsys, name)
    mid = created["mission"]["id"]
    assert f'"{name}"' in _run(capsys, "status", "--mission", mid)


@pytest.mark.parametrize("flags,message", [
    (["--providers", ""], "a rotation needs at least one provider or route name"),
    (["--routes", "nosuch-route"], "unknown rotation entry 'nosuch-route'"),
])
def test_create_and_start_refuse_an_empty_or_unknown_rotation(tmp_path, state, capsys, flags, message) -> None:
    err = _run(capsys, "mission", "create", "--prompt", "x", "--cwd", str(tmp_path), *flags, code=2)
    assert message in err
    mid = _create(tmp_path, capsys, "ok")["mission"]["id"]
    assert message in _run(capsys, "start", "--mission", mid, *flags, code=2)


# ---------------------------------------------------------------- CLIUX-077: bare llm-bridge says why it shows help


def test_bare_command_without_a_terminal_explains_and_exits_2(tmp_path, state, capsys, monkeypatch) -> None:
    monkeypatch.delenv("LLM_BRIDGE_MANAGED", raising=False)
    assert main([]) == 2
    captured = capsys.readouterr()
    assert "llm-bridge: the home screen needs an interactive terminal" in captured.err
    assert "usage:" in captured.out


def test_bare_command_on_a_dumb_terminal_explains_and_exits_2(tmp_path, state, capsys, monkeypatch) -> None:
    monkeypatch.delenv("LLM_BRIDGE_MANAGED", raising=False)
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(sys, "stdout", _Tty())
    monkeypatch.setenv("TERM", "dumb")
    assert main([]) == 2
    assert "TERM is 'dumb'" in capsys.readouterr().err


def test_bare_command_in_a_managed_session_explains_without_failing(tmp_path, state, capsys, monkeypatch) -> None:
    monkeypatch.setattr(bridge, "_managed_session", lambda: True)
    assert main([]) == 0
    assert "Bridge-managed session" in capsys.readouterr().err
