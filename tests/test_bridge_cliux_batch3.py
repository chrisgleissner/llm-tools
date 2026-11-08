"""CLIUX-024 (pause/stop --after-task), CLIUX-025 (console Reject and hold) and CLIUX-038 (short ID in the console header)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_missions, bridge_tui
from llm_tools.bridge import main
from llm_tools.bridge_help import topic_text
from llm_tools.bridge_store import BridgeStore


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


def _create(tmp_path: Path, capsys) -> dict:
    folder = tmp_path / "work"
    folder.mkdir()
    return json.loads(_run(capsys, "mission", "create", "--prompt", "work objective", "--name", "work", "--cwd", str(folder), "--providers", "codex", "--json"))


def _config(state: Path, mission_id: str) -> dict:
    with BridgeStore(state / "bridge.sqlite3") as store:
        return dict(store.get_mission(mission_id)["config"])


# ---------------------------------------------------------------- CLIUX-024


def test_pause_after_task_stores_the_same_intent_as_the_steer_kind(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    out = _run(capsys, "pause", "--mission", mid, "--after-task")
    assert out.startswith("Pause scheduled")
    assert "nothing is interrupted" in out
    assert _config(state, mid)["pause_after_iteration"] is True


def test_stop_after_task_names_the_task_and_stores_the_same_intent(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid, task_id = created["mission"]["id"], created["task"]["id"]
    out = _run(capsys, "stop", "--mission", mid, "--after-task", "1")
    assert out.startswith("Stop scheduled") and "task 1" in out
    assert _config(state, mid)["stop_after_task"] == task_id


def test_stop_after_task_without_a_number_uses_the_running_task(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid, task_id = created["mission"]["id"], created["task"]["id"]
    _run(capsys, "stop", "--mission", mid, "--after-task", code=2)  # nothing is running yet
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.mark_operation_running(operation)
    out = _run(capsys, "stop", "--mission", mid, "--after-task")
    assert "task 1" in out
    assert _config(state, mid)["stop_after_task"] == task_id


def test_stop_after_task_refuses_force_and_a_finished_task(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    assert "cannot be combined" in _run(capsys, "stop", "--mission", mid, "--after-task", "1", "--force", code=2)
    _run(capsys, "task", "cancel", "--mission", mid, "--task", "1")
    assert "nothing to wait for" in _run(capsys, "stop", "--mission", mid, "--after-task", "1", code=2)


def test_steer_kinds_still_work_as_hidden_aliases(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    assert _run(capsys, "steer", "--mission", mid, "--kind", "pause-after-iteration").startswith("Steering pause-after-iteration")
    with pytest.raises(SystemExit):
        main(["steer", "--help"])
    assert "--kind pause-after-iteration" not in capsys.readouterr().out


def test_help_names_the_after_task_forms(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["pause", "--help"])
    assert "--after-task" in capsys.readouterr().out
    text = topic_text("quickstart")
    assert "pause --after-task" in text and "stop --after-task" in text


# ---------------------------------------------------------------- CLIUX-025


def _console(tasks: list[dict]):
    sent: list[str] = []
    console = bridge_tui._ConsoleState("0123456789abcdef0123456789abcdef", lambda _mission, text: sent.append(text) or "{}", lambda *_: {}, full=True)
    console.snapshot = {"mission": {"status": "RUNNING"}, "tasks": tasks}
    return console, sent


def _join_commands() -> None:
    for thread in threading.enumerate():
        if thread.name == "llm-bridge-command":
            thread.join(2)


def test_reject_dialog_offers_retry_and_hold(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    console, sent = _console([{"id": 3, "ordinal": 3, "status": "VERIFYING"}])
    assert console._confirm_or_dispatch("/reject needs tests")
    assert "Reject and hold" in console.output[-1] and "Reject and retry" in console.output[-1]
    console.handle_key("h")
    _join_commands()
    assert len(sent) == 1 and sent[0].endswith(" --hold") and sent[0].startswith("task reject --task 3")


def test_reject_dialog_retry_key_sends_no_hold(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    console, sent = _console([{"id": 3, "ordinal": 3, "status": "VERIFYING"}])
    console._confirm_or_dispatch("/reject needs tests")
    console.handle_key("y")
    _join_commands()
    assert len(sent) == 1 and "--hold" not in sent[0]


def test_hold_key_does_nothing_in_other_dialogs(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    console, sent = _console([{"id": 3, "ordinal": 3, "status": "VERIFYING"}])
    console._confirm_or_dispatch("/approve")
    console.handle_key("h")
    assert console.confirm_command == "task approve --task 3" and sent == []


# ---------------------------------------------------------------- CLIUX-038


def test_console_header_shows_the_short_id_next_to_the_name(monkeypatch) -> None:
    console, _sent = _console([])
    console.snapshot = {"mission": {"status": "RUNNING", "name": "work"}, "tasks": []}
    monkeypatch.setattr(bridge_tui, "ascii_mode", lambda: False)
    assert bridge_tui._header(console, 100) == "LLM BRIDGE · ▌ work (01234567) · ● RUNNING"
    monkeypatch.setattr(bridge_tui, "ascii_mode", lambda: True)
    assert "work (01234567)" in bridge_tui._header(console, 100)


def test_console_header_drops_the_id_when_the_name_would_get_too_short(monkeypatch) -> None:
    console, _sent = _console([])
    console.snapshot = {"mission": {"status": "RUNNING", "name": "a long mission name"}, "tasks": []}
    monkeypatch.setattr(bridge_tui, "ascii_mode", lambda: False)
    assert "(01234567)" in bridge_tui._header(console, 60)
    assert "(01234567)" not in bridge_tui._header(console, 40)
