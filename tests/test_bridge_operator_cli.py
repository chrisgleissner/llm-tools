"""Operator-facing CLI behaviour: readable output, mission addressing, slash commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_format
from llm_tools.bridge import dispatch_console_command, main
from llm_tools.bridge_store import BridgeStore, MissionLock


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    return directory


def _create(tmp_path: Path, capsys, *extra: str, name: str = "Fix the tests") -> dict:
    assert main(["mission", "create", "--prompt", "Do the work\nsecond line", "--name", name, "--cwd", str(tmp_path), *extra, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def _out(capsys, *argv: str, code: int = 0) -> str:
    assert main(list(argv)) == code
    captured = capsys.readouterr()
    return captured.out if code == 0 else captured.err


def test_mission_create_and_list_are_readable_and_json_is_unchanged(tmp_path, state, capsys) -> None:
    assert main(["mission", "create", "--prompt", "Do it", "--name", "Fix the tests", "--cwd", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    mission = BridgeStore(state / "bridge.sqlite3").list_missions()[0]
    short = mission["id"][:8]
    assert text.strip() == f'Created mission {short} "Fix the tests" · task 1 · start with: llm-bridge start --mission {short}'
    assert "frozen_tool_config" not in text

    listing = _out(capsys, "mission", "list")
    lines = listing.splitlines()
    assert lines[0].split() == ["ID", "STATUS", "UPDATED", "NAME", "FOLDER"]
    assert lines[1].startswith(short) and "CREATED" in lines[1] and "just now" in lines[1] and "Fix the tests" in lines[1]
    assert str(tmp_path) in lines[1]
    assert "frozen_tool_config" not in listing

    machine = json.loads(_out(capsys, "mission", "list", "--json"))
    assert machine["missions"][0]["id"] == mission["id"]
    assert "frozen_tool_config" not in machine["missions"][0]["config"]
    with_config = json.loads(_out(capsys, "mission", "list", "--json", "--include-config"))
    assert "frozen_tool_config" in with_config["missions"][0]["config"]


def test_empty_mission_list_explains_how_to_create_one(state, capsys) -> None:
    assert "No missions yet" in _out(capsys, "mission", "list")


def test_task_commands_print_receipts_and_tables(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    out = _out(capsys, "task", "add", "--mission", mid, "--prompt", "second job", "--title", "Second", "--priority", "3")
    assert out.strip() == 'Added task 2 "Second" · QUEUED · priority 3'
    table = _out(capsys, "task", "list", "--mission", mid).splitlines()
    assert table[0].split() == ["TASK", "STATUS", "PRIORITY", "AFTER", "TITLE"]
    assert ["2", "QUEUED", "3"] == next(line.split()[:3] for line in table if line.startswith("2"))
    assert _out(capsys, "task", "amend", "--mission", mid, "--task", "2", "--prompt", "new text").strip() == "Amended · task 2 is now revision 2"
    shown = _out(capsys, "task", "show", "--mission", mid, "--task", "2")
    assert 'task 2 "Second"' in shown and "revision 2" in shown and "new text" in shown and "second job" not in shown
    assert "pending amendment" not in shown
    assert _out(capsys, "task", "pause", "--mission", mid, "--task", "2").startswith("Paused task 2")
    assert _out(capsys, "task", "priority", "--mission", mid, "--task", "2", "--value", "9").startswith("Priority set for task 2")
    assert _out(capsys, "task", "verify", "--mission", mid, "--task", "2", "--approve", code=2)  # not awaiting verification


def test_task_verify_receipt(tmp_path, state, capsys) -> None:
    created = _create(tmp_path, capsys)
    mid = created["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        operation = store.begin_operation(1, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
    assert _out(capsys, "task", "verify", "--mission", mid, "--task", "1", "--approve").startswith('Verified · task 1 "Do the work" · COMPLETED')


def test_lifecycle_commands_print_one_line_receipts(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    short = mid[:8]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
    assert _out(capsys, "pause", "--mission", mid).strip() == f"Pause requested · mission {short} is PAUSED"
    # With the controller lock held, resume only records the request and never launches a provider.
    with MissionLock(state / "locks", mid):
        assert _out(capsys, "resume", "--mission", mid).strip() == f"Resume requested · mission {short} is RUNNING"
    assert _out(capsys, "stop", "--mission", mid).strip() == f"Stop requested · mission {short} is STOPPING"
    assert _out(capsys, "cancel", "--mission", mid).strip() == f"Cancel requested · mission {short} is CANCELLED"


def test_status_events_doctor_and_proposals_are_readable(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys, "--providers", "codex")["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        store.record_runtime_event(mid, "provider-selection", {
            "provider": "codex", "route": "", "selected_model": "m", "decision": {"usable": True},
            "decisions": [{"provider": "codex", "usable": True, "windows": [
                {"name": "5h", "kind": "reset_window", "remaining_percent": 73},
                {"name": "weekly", "kind": "reset_window", "remaining_percent": 55}]}],
        })
        store.add_task(mid, "later", title="Later job")
    status = _out(capsys, "status", "--mission", mid)
    short = mid[:8]
    assert f'Mission     {short} "Fix the tests" · RUNNING (background)' in status
    assert "Current     none" in status
    assert 'Next        task 1 "Do the work" · QUEUED' in status
    assert "Tasks       2 total: 2 QUEUED" in status
    assert "  codex         5h 73% left · weekly 55% left · usable" in status
    assert "frozen_tool_config" not in status and len(status.splitlines()) < 15

    events = _out(capsys, "events", "--mission", mid).splitlines()
    assert events[0].split() == ["ID", "TIME", "KIND", "DETAIL"]
    assert any("provider-selection" in line and "provider=codex" in line for line in events)

    doctor = _out(capsys, "doctor", "--mission", mid)
    assert "integrity ok" in doctor and f'Mission          {short} "Fix the tests"' in doctor
    assert "frozen_tool_config" not in doctor
    assert _out(capsys, "proposal", "list", "--mission", mid).strip() == "No proposals."


def test_mission_prefix_resolution_errors_and_success(tmp_path, state, monkeypatch, capsys) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        first = store.create_mission("first", "o", cwd=tmp_path, mission_id="abcd1111" + "0" * 24)["id"]
        second_dir = tmp_path / "other"
        second_dir.mkdir()
        second = store.create_mission("second", "o", cwd=second_dir, mission_id="abcd2222" + "0" * 24)["id"]
    assert json.loads(_out(capsys, "status", "--mission", "abcd1", "--json"))["mission"]["id"] == first
    assert json.loads(_out(capsys, "status", "--mission", second, "--json"))["mission"]["id"] == second
    err = _out(capsys, "status", "--mission", "abcd", code=2)
    assert "ambiguous" in err and "abcd1111" in err and "abcd2222" in err and "first" in err
    assert "no mission matches 'zzzz'; run llm-bridge mission list" in _out(capsys, "status", "--mission", "zzzz", code=2)
    assert "too short" in _out(capsys, "status", "--mission", "ab", code=2)


def test_mission_is_inferred_from_the_current_folder(tmp_path, state, monkeypatch, capsys) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    with BridgeStore(state / "bridge.sqlite3") as store:
        mid = store.create_mission("proj", "o", cwd=project)["id"]
    monkeypatch.chdir(project)
    assert json.loads(_out(capsys, "status", "--json"))["mission"]["id"] == mid
    monkeypatch.chdir(project / "src")
    assert json.loads(_out(capsys, "status", "--json"))["mission"]["id"] == mid
    assert "Mission     " + mid[:8] in _out(capsys, "status")
    monkeypatch.chdir(other)
    err = _out(capsys, "status", code=2)
    assert "no unfinished mission for folder" in err and "mission list" in err


def test_folder_inference_lists_candidates_and_prefers_live_missions(tmp_path, state, monkeypatch, capsys) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        stopped = store.create_mission("old stopped", "o", cwd=tmp_path)["id"]
        store.set_mission_status(stopped, "RUNNING")
        store.set_mission_status(stopped, "STOPPED")
        finished = store.create_mission("finished", "o", cwd=tmp_path)["id"]
        store.set_mission_status(finished, "CANCELLED")
    monkeypatch.chdir(tmp_path)
    assert json.loads(_out(capsys, "status", "--json"))["mission"]["id"] == stopped  # cancelled mission is ignored
    with BridgeStore(state / "bridge.sqlite3") as store:
        live = store.create_mission("live one", "o", cwd=tmp_path)["id"]
    assert json.loads(_out(capsys, "status", "--json"))["mission"]["id"] == live
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(live, "RUNNING")
        store.set_mission_status(live, "STOPPED")
    err = _out(capsys, "status", code=2)
    assert "several missions match" in err and stopped[:8] in err and live[:8] in err and "old stopped" in err


def test_doctor_does_not_infer_a_mission(tmp_path, state, monkeypatch, capsys) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.create_mission("proj", "o", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    assert json.loads(_out(capsys, "doctor", "--json"))["mission"] is None


def test_console_accepts_slash_commands_and_exit_hint(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    assert json.loads(dispatch_console_command(mid, "/status --json"))["mission"]["id"] == mid
    assert json.loads(dispatch_console_command(mid, "/task list --json"))[0]["id"] == 1
    assert dispatch_console_command(mid, "/") == dispatch_console_command(mid, "help")
    assert dispatch_console_command(mid, "/help keys") == dispatch_console_command(mid, "help keys")
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
    assert json.loads(dispatch_console_command(mid, "/pause"))["result"]["mission_status"] == "PAUSED"
    for word in ("/quit", "/exit", "quit"):
        assert dispatch_console_command(mid, word) == "press Ctrl-D to leave controls · the mission keeps running"


def test_console_commands_still_accept_a_mission_prefix(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    reply = json.loads(dispatch_console_command("unused-default", f"status --json --mission {mid[:6]}"))
    assert reply["mission"]["id"] == mid


def test_native_stop_with_active_provider_explains_how_to_finish(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys, "--native")["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        operation = store.begin_operation(1, iteration=1, station="codex")
        store.mark_operation_running(operation, process_pid=os.getpid(), process_start="t")
    out = _out(capsys, "stop", "--mission", mid)
    assert out.splitlines()[0] == f"Stop requested · mission {mid[:8]} is STOPPING"
    assert bridge_format.NATIVE_STOP_HINT in out
    assert "exit the provider (e.g. /quit) or use stop --force" in out
    status = _out(capsys, "status", "--mission", mid)
    assert bridge_format.NATIVE_STOP_HINT in status
    # The JSON receipt carries the same explanation for the console.
    reply = json.loads(dispatch_console_command(mid, "stop"))
    assert reply["result"]["detail"] == bridge_format.NATIVE_STOP_HINT


def test_non_native_stop_has_no_provider_wait_hint(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        operation = store.begin_operation(1, iteration=1, station="codex")
        store.mark_operation_running(operation, process_pid=os.getpid(), process_start="t")
    assert bridge_format.NATIVE_STOP_HINT not in _out(capsys, "stop", "--mission", mid)


def test_attach_without_a_station_says_how_to_start_one(tmp_path, state, monkeypatch, capsys) -> None:
    from llm_tools import bridge_runtime

    mid = _create(tmp_path, capsys)["mission"]["id"]
    monkeypatch.setattr(bridge_runtime, "mission_socket_path", lambda _mission_id, **_kw: tmp_path / "none.sock")
    err = _out(capsys, "attach", "--mission", mid, code=2)
    short = mid[:8]
    assert f"No provider station is running for mission {short} · start it with: llm-bridge start --mission {short}" in err
    assert "tmux" not in err.lower()


def test_load_handle_translates_a_missing_session(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_runtime
    from llm_tools.terminal import TmuxTerminalBackend

    socket = tmp_path / "s.sock"
    socket.touch()
    monkeypatch.setattr(bridge_runtime, "mission_socket_path", lambda _mission_id, **_kw: socket)

    def missing(self, **_kwargs):
        raise RuntimeError("no live terminal station named mission-x")

    monkeypatch.setattr(TmuxTerminalBackend, "open_existing", missing)
    with pytest.raises(RuntimeError, match="No provider station is running"):
        bridge._load_handle("m" * 32)

    def other(self, **_kwargs):
        raise RuntimeError("something else")

    monkeypatch.setattr(TmuxTerminalBackend, "open_existing", other)
    with pytest.raises(RuntimeError, match="something else"):
        bridge._load_handle("m" * 32)


def test_state_directory_permission_error_names_the_fix(tmp_path) -> None:
    shared = tmp_path / "state"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(PermissionError) as raised:
        BridgeStore(shared / "bridge.sqlite3")
    assert str(raised.value).endswith(f"· fix with: chmod 700 {shared}")
    assert shared.stat().st_mode & 0o777 == 0o755  # never tightened silently


def test_mission_create_help_describes_every_option_without_none_noise(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["mission", "create", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "(default: None)" not in text and "(default: False)" not in text
    assert "--verifier-timeout SECONDS" in text
    assert "seconds before a verifier run is treated as failed" in text and "(default: 300)" in text
    assert "--log-max-bytes BYTES" in text and "--name NAME" in text and "first line of the prompt" in text
    assert "--force" not in text
    with pytest.raises(SystemExit):
        main(["stop", "--help"])
    stop = " ".join(capsys.readouterr().out.split())
    assert "[--mission ID]" in stop and "--force" in stop and "(default: False)" not in stop
    assert "--mission 3f9a1c2b" in stop and "unique ID prefix" in stop


def test_default_rotation_follows_the_llm_tools_config(tmp_path, monkeypatch, capsys) -> None:
    assert bridge.default_rotation() == ("claude,codex,opencode", "")
    config = tmp_path / "config.toml"
    config.write_text('[defaults]\nproviders = ["claude", "codex"]\n[ralph]\nroutes = ["claude", "kilo-zai-glm-52"]\n', encoding="utf-8")
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(config))
    assert bridge.default_rotation() == ("claude,codex", "claude,kilo-zai-glm-52")
    with pytest.raises(SystemExit):
        main(["mission", "create", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "(default: claude,codex)" in text and "routes claude,kilo-zai-glm-52" in text
    config.write_text("not = [valid", encoding="utf-8")
    assert bridge.default_rotation() == ("claude,codex,opencode", "")


def test_new_missions_use_the_configured_rotation(tmp_path, state, monkeypatch, capsys) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[defaults]\nproviders = ["codex"]\n', encoding="utf-8")
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(config))
    created = _create(tmp_path, capsys)
    assert created["mission"]["config"]["providers"] == "codex"
    assert created["mission"]["config"]["routes"] == ""


def test_legacy_prompt_mode_no_start_prints_a_receipt(tmp_path, state, capsys) -> None:
    assert main(["--prompt", "Build it", "--cwd", str(tmp_path), "--no-start"]) == 0
    assert capsys.readouterr().out.startswith("Created mission ")


def test_format_helpers_cover_edge_cases() -> None:
    assert bridge_format.duration(5) == "5s" and bridge_format.duration(150) == "2m"
    assert bridge_format.duration(3600) == "1h" and bridge_format.duration(5400) == "1h30m" and bridge_format.duration(172800) == "2d"
    assert bridge_format.age(None) == "-" and bridge_format.age("garbage") == "-"
    assert bridge_format.age("2026-01-01T00:00:00+00:00", now=1767225600 + 7200) == "2h ago"
    assert bridge_format.home_relative(str(Path.home())) == "~"
    assert bridge_format.home_relative(str(Path.home() / "x")) == "~/x"
    assert bridge_format.format_task_list([]).startswith("No tasks")
    assert bridge_format.format_events([]) == "No events."
    event = {"id": 7, "created_at": "2033-05-18T03:00:00+00:00", "kind": "provider-selection", "data": {"route": "codex", "rotation_reason": "even-burn"}}
    rendered = bridge_format.format_events([event])
    assert "rotation_reason=spreading use" in rendered and "even-burn" not in rendered
    assert event["data"]["rotation_reason"] == "even-burn"
    assert bridge_format.format_proposals([]) == "No proposals."
    assert bridge_format.format_command_receipt("pause", {"mission_status": "PAUSED", "detail": "later"}, "a" * 32) == "Pause requested · mission aaaaaaaa is PAUSED\nlater"
    assert bridge_format.format_command_receipt("stop", {"mission": {"status": "STOPPED"}}, "a" * 32) == "Stop forced · mission aaaaaaaa is STOPPED"
    assert "Station closed" in bridge_format.format_command_receipt("stop", {"mission": {"status": "COMPLETED"}, "station_status": "STOPPED"}, "a" * 32)
    assert "No station to close" in bridge_format.format_command_receipt("stop", {"mission": {"status": "FAILED"}, "station_status": "ABSENT"}, "a" * 32)
    assert bridge_format.format_command_receipt("steer", {"kind": "review", "task_id": 3, "status": "APPLIED"}, "a" * 32) == "Steering review task 3 · APPLIED"
    assert bridge_format.format_command_receipt("station", {"viewed_station": "codex"}, "a" * 32) == "Viewing station codex · mission aaaaaaaa"
    assert bridge_format.format_proposal_result("accept", {"payload": {"task_id": 4}, "result": {}}) == "Proposal accepted · task 4"
    assert bridge_format.format_proposal_result("create", {"id": 5, "title": "T", "status": "QUEUED"}) == 'Proposed task 5 "T" · QUEUED'
    detail = bridge_format.format_task_detail({"id": 1, "title": "T", "status": "QUEUED", "depends_on": [2], "acceptance": ["a"], "evidence": [{"text": "pytest passed", "source": "operator"}], "prompt": "\n".join(str(i) for i in range(20))})
    assert "depends on 2" in detail and "  - a" in detail and "evidence:\n  - pytest passed (operator)" in detail and detail.endswith("  …")
    waiting = bridge_format.format_status({
        "mission": {"id": "b" * 32, "name": "n", "status": "RUNNING", "cwd": "/x", "config": {}},
        "capacity": {"selection": {"decisions": [{"provider": "kilo", "usable": False, "reason": "rate-limited", "windows": [{"name": "balance", "kind": "balance", "remaining_amount": 4.5, "currency": "$"}, {"name": "x", "kind": "unknown"}]}]}, "next_eligible_epoch": 1000 + 600},
        "lifecycle": {"controller": "running", "heartbeat": {"age_seconds": 3, "reported_state": "idle"}},
        "pending_commands": [{}], "health": {"stall_warning": "3 consecutive provider operations failed"},
        "task_counts": {}, "tasks": [],
    }, now=1000)
    assert "kilo" in waiting and "balance $4.5" in waiting and "x unknown" in waiting and "rate-limited" in waiting
    assert "next provider capacity in 10m" in waiting and "heartbeat idle 3s ago" in waiting
    assert "Pending     1 command(s)" in waiting and "Warning     3 consecutive" in waiting


def test_proposal_station_and_steer_receipts(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys, "--providers", "codex,claude")["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        operation = store.begin_operation(1, iteration=1, station="codex")
    created = _out(capsys, "proposal", "create", "--mission", mid, "--operation", operation, "--prompt", "follow up", "--title", "Follow up")
    assert created.startswith('Proposed task 2 "Follow up"')
    assert "Follow up" in _out(capsys, "proposal", "list", "--mission", mid)
    assert _out(capsys, "proposal", "accept", "--mission", mid, "--task", "2").strip() == "Proposal accepted · task 2"
    assert _out(capsys, "station", "--mission", mid, "claude").strip() == f"Viewing station claude · mission {mid[:8]}"
    assert _out(capsys, "steer", "--mission", mid, "--kind", "pause-after-iteration").strip().startswith("Steering pause-after-iteration")
    assert "not configured" in _out(capsys, "station", "--mission", mid, "nowhere", code=2)


def test_rejected_lifecycle_command_exits_nonzero_in_text_mode(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    assert main(["cancel", "--mission", mid]) == 0
    capsys.readouterr()
    assert main(["resume", "--mission", mid]) == 2  # a cancelled mission cannot be resumed
    assert "was cancelled and cannot be resumed" in capsys.readouterr().err
    assert main(["resume", "--mission", mid, "--json"]) == 0  # --json keeps its historical exit status
    assert json.loads(capsys.readouterr().out)["status"] == "REJECTED"


def test_completed_mission_stop_prints_station_receipt(tmp_path, state, capsys) -> None:
    mid = _create(tmp_path, capsys)["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mid, "RUNNING")
        store.set_mission_status(mid, "FAILED")
    assert _out(capsys, "stop", "--mission", mid).strip() == f"No station to close · mission {mid[:8]} is FAILED"
