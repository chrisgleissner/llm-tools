"""A native provider that prints its usage-limit notice is rotated away from, or waited out visibly (DOG-016)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from llm_tools import common, ralph_robin, routes
from llm_tools.bridge import main, status_snapshot
from llm_tools.bridge_runtime import BridgeMissionHooks, _process_start_token
from llm_tools.bridge_store import BridgeStore
from llm_tools.terminal import TerminalHandle, TmuxTerminalBackend
from tests import process_hygiene

FIXTURES = Path(__file__).parent / "fixtures" / "usage_limit"
BERLIN = ZoneInfo("Europe/Berlin")
NOW = int(datetime(2026, 11, 6, 21, 15, tzinfo=BERLIN).timestamp())
RESET = int(datetime(2026, 11, 6, 23, 40, tzinfo=BERLIN).timestamp())  # "limit resets 11:40pm"
READY = {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}


def screen(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture()
def native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A native mission whose provider pane shows ``fixture``; nothing real is started."""
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", str(NOW))
    monkeypatch.setenv("LLM_TOOLS_LOCAL_BLOCK_DIR", str(tmp_path / "blocks"))
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", json.dumps({"claude": READY, "codex": READY}))
    monkeypatch.setattr(BridgeMissionHooks, "USAGE_LIMIT_LOOK_SECONDS", 0.0)
    monkeypatch.setattr(TmuxTerminalBackend, "set_review_hint", lambda *_a, **_k: None)
    monkeypatch.setattr(ralph_robin, "guard_against_auto_suspend", lambda *_a: SimpleNamespace())
    shown = {"text": screen("claude_usage_limit_reached.txt")}

    def activity_screen(_self, _handle, *, pane: str = "provider", seen_epoch: int | None = None):
        return 7, (None if seen_epoch == 7 else shown["text"])  # tmux skips the capture while the pane is unchanged

    monkeypatch.setattr(TmuxTerminalBackend, "pane_activity_screen", activity_screen)
    store = BridgeStore(tmp_path / "state" / "bridge.sqlite3")
    handle = TerminalHandle(tmp_path / "s.sock", tmp_path / "s.conf", "m-test", "%1", "%2", 8)

    def mission(providers: str) -> tuple[str, BridgeMissionHooks]:
        created = store.create_mission("limit", "objective", cwd=tmp_path, initial_prompt="work", config={"providers": providers, "native_tui": True})
        store.set_mission_status(created["id"], "RUNNING")
        hooks = BridgeMissionHooks(created["id"], handle=handle, headless=False, native_tui=True, controller_logs=tmp_path / "controller.log")
        hooks.bind(store)
        return created["id"], hooks

    yield SimpleNamespace(store=store, mission=mission, pane=shown, tmp_path=tmp_path, patch=monkeypatch)
    store.close()
    monkeypatch.undo()
    time.tzset()


def _run_ralph(native, hooks: BridgeMissionHooks, providers: str, runner) -> int:
    native.patch.setattr(ralph_robin, "run_scheduler_inline", runner)
    return ralph_robin.main(
        [
            "--prompt", "objective", "--providers", providers, "--cwd", str(native.tmp_path),
            "--max-iterations", "1", "--max-duration", "0", "--min-iteration-seconds", "0", "--poll-interval", "1",
            "--log-dir", str(native.tmp_path / "ralph-logs"), "--state-file", str(native.tmp_path / "ralph-state.json"),
        ],
        bridge_hooks=hooks,
    )


def _watch_until_ended(scheduler_config) -> int:
    """What the scheduler's tmux wait loop does: poll the observer until it ends the run."""
    for _ in range(5):
        ended = scheduler_config.process_observer()
        if ended:
            return int(ended)
    raise AssertionError("the observer never ended the run although the pane shows a usage-limit notice")


def _kinds(store: BridgeStore, mission_id: str) -> list[str]:
    return [event["kind"] for event in store.events(mission_id)]


def test_claude_usage_limit_in_the_station_rotates_to_codex_and_resumes_the_task(native) -> None:
    mission_id, hooks = native.mission("claude,codex")
    launched: list[str] = []

    def run_provider(scheduler_config) -> int:
        launched.append(scheduler_config.provider)
        if scheduler_config.provider == "claude":
            return _watch_until_ended(scheduler_config)
        return 0

    assert _run_ralph(native, hooks, "claude,codex", run_provider) == 0

    assert launched == ["claude", "codex"]
    block = routes.read_local_block("claude")
    assert block is not None and block["reason"] == "rate-limited" and block["blocked_until"] == RESET
    events = native.store.events(mission_id)
    limit = next(event for event in events if event["kind"] == "provider-usage-limit")["data"]
    assert (limit["provider"], limit["reset_epoch"], limit["reset_known"]) == ("claude", RESET, True)
    assert "limit resets 11:40pm" in limit["notice"]
    diagnosed = [event["data"]["failure"]["class"] for event in events if event["kind"] == "provider-result-diagnosed"]
    assert diagnosed == ["capacity", "success"]
    assert any(event["kind"] == "provider-handoff" for event in events)
    assert any(event["kind"] == "station-handoff" and event["data"]["to"] == "codex" for event in events)
    assert native.store.db.execute("SELECT status FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0] == "VERIFYING"


def test_without_another_ready_provider_the_mission_waits_visibly_with_the_reset_time(native, capsys) -> None:
    mission_id, hooks = native.mission("claude")
    waits: list[dict[str, Any]] = []

    def stop_after_wait_is_visible(cfg, logs, selection, *args, refresh_selection=None, **kwargs) -> bool:
        waits.append(refresh_selection())  # what the real wait does first: re-read capacity and publish the wait
        native.store.set_mission_status(mission_id, "STOPPING")
        return True

    native.patch.setattr(ralph_robin, "suspend_until_available", stop_after_wait_is_visible)
    _run_ralph(native, hooks, "claude", lambda scheduler_config: _watch_until_ended(scheduler_config))

    assert waits and waits[0]["all_rate_limited"] is True
    native.store.set_mission_status(mission_id, "RUNNING")
    waiting = status_snapshot(mission_id)["waiting"]
    assert waiting is not None and waiting["reason"] == "capacity"
    assert waiting["until_epoch"] == RESET
    assert waiting["entries"][0]["name"] == "claude" and waiting["entries"][0]["reset_epoch"] == RESET
    assert main(["status", "--mission", mission_id]) == 0
    line = next(row for row in capsys.readouterr().out.splitlines() if row.startswith("Waiting"))
    assert "claude" in line and "resets in" in line
    task = native.store.db.execute("SELECT status FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0]
    assert task == "READY"  # resumable when the reset passes, not stranded as BLOCKED


def test_a_notice_with_an_unknown_reset_blocks_for_the_default_backoff(native) -> None:
    native.patch.setenv("LLM_TOOLS_LOCAL_BLOCK_BACKOFF", "600")
    native.pane["text"] = screen("claude_limit_without_reset.txt")
    mission_id, hooks = native.mission("claude,codex")
    launched: list[str] = []

    def run_provider(scheduler_config) -> int:
        launched.append(scheduler_config.provider)
        return _watch_until_ended(scheduler_config) if scheduler_config.provider == "claude" else 0

    assert _run_ralph(native, hooks, "claude,codex", run_provider) == 0
    assert launched == ["claude", "codex"]
    block = routes.read_local_block("claude")
    assert block is not None and block["blocked_until"] == NOW + 600
    limit = next(event for event in native.store.events(mission_id) if event["kind"] == "provider-usage-limit")["data"]
    assert limit["reset_known"] is False and limit["blocked_until"] == NOW + 600


def test_codex_notice_is_recognized_for_a_codex_run(native) -> None:
    native.pane["text"] = screen("codex_try_again_in.txt")
    mission_id, hooks = native.mission("codex,claude")
    launched: list[str] = []

    def run_provider(scheduler_config) -> int:
        launched.append(scheduler_config.provider)
        return _watch_until_ended(scheduler_config) if scheduler_config.provider == "codex" else 0

    assert _run_ralph(native, hooks, "codex,claude", run_provider) == 0
    assert launched == ["codex", "claude"]
    block = routes.read_local_block("codex")
    assert block is not None and block["blocked_until"] == NOW + 2 * 3600 + 13 * 60


def test_prose_that_mentions_a_usage_limit_does_not_end_the_run(native) -> None:
    native.pane["text"] = screen("claude_editing_code_that_mentions_limits.txt")
    mission_id, hooks = native.mission("claude,codex")
    observed: list[Any] = []

    def run_provider(scheduler_config) -> int:
        observed.extend(scheduler_config.process_observer() for _ in range(5))
        return 0

    assert _run_ralph(native, hooks, "claude,codex", run_provider) == 0
    assert observed == [None] * 5
    assert routes.read_local_block("claude") is None
    assert "provider-usage-limit" not in _kinds(native.store, mission_id)


def test_a_notice_must_be_seen_on_two_reads_before_the_run_ends(native) -> None:
    mission_id, hooks = native.mission("claude,codex")
    results: list[Any] = []

    def run_provider(scheduler_config) -> int:
        results.append(scheduler_config.process_observer())  # first read: candidate only
        results.append(scheduler_config.process_observer())  # unchanged pane, same verdict: confirmed
        return 0

    assert _run_ralph(native, hooks, "claude,codex", run_provider) == 0
    assert results == [None, common.AUTONOMY_ABORT_STATUS]


def test_the_provider_process_tree_is_stopped_before_the_pane_is_reused(native) -> None:
    mission_id, hooks = native.mission("claude,codex")
    task = native.store.ready_tasks(mission_id, limit=1)[0]
    operation_id = native.store.begin_operation(int(task["id"]), iteration=1, station="claude")
    native.store.mark_operation_running(operation_id, pane_id="%1", route_id="", model="")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        native.store.record_operation_process(operation_id, process_pid=child.pid, process_start=_process_start_token(child.pid))
        assert hooks._end_provider_process(operation_id) is True
        assert child.wait(timeout=5) is not None
    finally:
        process_hygiene.terminate_process_tree(child)


def test_usage_limit_event_has_a_phrase_for_the_activity_view(native) -> None:
    from llm_tools.bridge_event_text import _HUMANIZERS
    from llm_tools.bridge_vocab import EventKind

    assert str(EventKind.PROVIDER_USAGE_LIMIT) in _HUMANIZERS
