"""`llm-bridge events`: operator wording, hidden bookkeeping, newest-first window, --since and --follow (CLIUX-020, 044, 058)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_missions
from llm_tools.bridge import main
from llm_tools.bridge_store import BridgeStore
from tests.conftest import without_help_reference


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
    return directory


def _mission(tmp_path: Path, capsys) -> str:
    assert main(["mission", "create", "--prompt", "Write the parser tests", "--name", "Parser", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    return json.loads(capsys.readouterr().out)["mission"]["id"]


def _events(capsys, *argv: str, code: int = 0) -> tuple[str, str]:
    assert main(["events", *argv]) == code, capsys.readouterr()
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_default_output_is_phrased_like_the_activity_view_and_hides_bookkeeping(tmp_path, state, capsys) -> None:
    mission = _mission(tmp_path, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.record_runtime_event(mission, "controller-heartbeat", {"controller_state": "ACTIVE", "owner_lock_held": True})
        store.record_runtime_event(mission, "controller-mode", {"actual_mode": "headless", "boottime_seconds": 198108.1})
        store.record_runtime_event(mission, "capacity-wake-plan", {"plan": "x"})
        store.record_runtime_event(mission, "prompt-assembled", {"assembled_bytes": 3383})
    out, _ = _events(capsys, "--mission", mission)
    assert "Mission created" in out and "Task 1 added" in out
    for plumbing in ("controller-heartbeat", "controller_state", "controller-mode", "boottime_seconds", "capacity-wake-plan", "prompt-assembled", "assembled_bytes", "controller alive"):
        assert plumbing not in out
    assert out.splitlines()[0].split()[:3] == ["ID", "TIME", "EVENT"]
    verbose, _ = _events(capsys, "--mission", mission, "--verbose")
    assert "controller-heartbeat" in verbose and "prompt-assembled" in verbose and "controller alive" in verbose


def test_json_keeps_raw_fields_and_still_hides_bookkeeping_unless_verbose(tmp_path, state, capsys) -> None:
    mission = _mission(tmp_path, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.record_runtime_event(mission, "controller-heartbeat", {"controller_state": "ACTIVE"})
    raw, _ = _events(capsys, "--mission", mission, "--json")
    events = json.loads(raw)["events"]
    assert {"id", "kind", "data", "created_at"} <= set(events[0])
    assert "controller-heartbeat" not in {event["kind"] for event in events}
    raw, _ = _events(capsys, "--mission", mission, "--json", "--verbose")
    assert "controller-heartbeat" in {event["kind"] for event in json.loads(raw)["events"]}


def test_a_rejected_command_event_records_and_shows_the_reason(tmp_path, state, capsys) -> None:
    mission = _mission(tmp_path, capsys)
    assert main(["task", "verify", "--mission", mission, "--task", "1", "--approve"]) == 4
    error = without_help_reference(capsys.readouterr().err).strip().removeprefix("llm-bridge: ")
    assert error
    out, _ = _events(capsys, "--mission", mission, "--verbose")
    rejected = [line for line in out.splitlines() if "rejected" in line.lower()]
    assert rejected, out
    assert error[:40] in rejected[0]
    raw, _ = _events(capsys, "--mission", mission, "--json", "--verbose")
    event = next(item for item in json.loads(raw)["events"] if item["kind"] == "command-rejected")
    assert event["data"]["result"]["error"] == error
    assert event["data"]["command"]


def test_default_shows_the_newest_events_oldest_to_newest(tmp_path, state, capsys) -> None:
    mission = _mission(tmp_path, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        for index in range(30):
            store.add_task(mission, f"extra {index}", title=f"Extra {index}")
        store.record_runtime_event(mission, "operation-exited", {"exit_code": 7})
    raw, _ = _events(capsys, "--mission", mission, "--limit", "5", "--json")
    ids = [event["id"] for event in json.loads(raw)["events"]]
    assert len(ids) == 5 and ids == sorted(ids)
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert ids[-1] == store.db.execute("SELECT MAX(id) FROM events").fetchone()[0]
    out, err = _events(capsys, "--mission", mission, "--limit", "5")
    assert "Provider exited (7)" in out
    assert re.search(r"showing the last 5 of \d+ events", out + err)
    assert "--limit" in (out + err)


def test_after_keeps_the_oldest_first_cursor(tmp_path, state, capsys) -> None:
    mission = _mission(tmp_path, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        for index in range(10):
            store.add_task(mission, f"extra {index}")
    first, _ = _events(capsys, "--mission", mission, "--after", "0", "--limit", "3", "--json")
    ids = [event["id"] for event in json.loads(first)["events"]]
    assert ids == sorted(ids) and ids[0] == min(ids)
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert ids[0] == store.db.execute("SELECT MIN(id) FROM events WHERE mission_id=?", (mission,)).fetchone()[0]
    more, _ = _events(capsys, "--mission", mission, "--after", str(ids[-1]), "--limit", "3", "--json")
    assert all(event["id"] > ids[-1] for event in json.loads(more)["events"])


def test_since_keeps_only_recent_events_and_rejects_a_bad_duration(tmp_path, state, capsys) -> None:
    mission = _mission(tmp_path, capsys)
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.db.execute("UPDATE events SET created_at=?", (old,))
        store.db.commit()
        store.record_runtime_event(mission, "operation-exited", {"exit_code": 3})
    raw, _ = _events(capsys, "--mission", mission, "--since", "10m", "--json")
    kinds = [event["kind"] for event in json.loads(raw)["events"]]
    assert kinds == ["operation-exited"]
    raw, _ = _events(capsys, "--mission", mission, "--since", "4h", "--json")
    assert "mission-created" in {event["kind"] for event in json.loads(raw)["events"]}
    with pytest.raises(SystemExit) as raised:
        main(["events", "--mission", mission, "--since", "yesterday"])
    assert raised.value.code == 2
    assert "--since" in capsys.readouterr().err


def test_follow_prints_new_events_until_interrupted(tmp_path, state, capsys, monkeypatch) -> None:
    mission = _mission(tmp_path, capsys)
    calls: list[int] = []

    def fake_sleep(_seconds: float) -> None:
        calls.append(1)
        if len(calls) == 1:
            with BridgeStore(state / "bridge.sqlite3") as store:
                store.record_runtime_event(mission, "operation-exited", {"exit_code": 9})
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge.time, "sleep", fake_sleep)
    assert main(["events", "--mission", mission, "--follow"]) == 0
    out = capsys.readouterr().out
    assert "Mission created" in out
    assert out.index("Mission created") < out.index("Provider exited (9)")
    assert out.count("Provider exited (9)") == 1


def test_help_says_ids_are_global_and_lists_the_new_options(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["events", "--help"])
    text = capsys.readouterr().out
    assert "global" in text
    for option in ("--since", "--follow", "--verbose", "--after", "--limit"):
        assert option in text
