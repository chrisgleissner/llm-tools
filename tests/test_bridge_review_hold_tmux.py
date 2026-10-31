"""Real scratch tmux station: a native mission does not start task 2 until task 1 is approved."""

from __future__ import annotations

import json
import shlex
import shutil
import sys
import time
from pathlib import Path

from llm_tools.bridge_runtime import mission_socket_path
from llm_tools.bridge_store import BridgeStore
from llm_tools.terminal import TmuxTerminalBackend
from tests.conftest import recorded_exit_status
from tests.test_bridge_recovery_tmux import call, controller_released, wait_for


def _statuses(database: Path, mission_id: str) -> list[str]:
    with BridgeStore(database) as store:
        return [str(task["status"]) for task in store.list_tasks(mission_id)]


def _runs(directory: Path) -> list[str]:
    log = directory / "provider-runs"
    return log.read_text(encoding="utf-8").split() if log.exists() else []


def _review_hint(backend: TmuxTerminalBackend, handle) -> str:
    return backend._run(handle.socket_path, "show-options", "-gqv", "@llm_bridge_review_hint", check=False, capture=True).stdout.strip()


def test_second_task_waits_for_approval_of_the_first(tmp_path: Path, env: dict[str, str]) -> None:
    assert shutil.which("tmux"), "CI must install tmux for Bridge station integration tests"
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({"codex": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}})
    provider = tmp_path / "provider.py"
    provider.write_text(
        "from pathlib import Path\n"
        "import time\n"
        "with open('provider-runs', 'a') as log:\n"
        "    log.write(f'run{int(time.time() * 1000)} ')\n"
        "print('fixture provider finished', flush=True)\n",
        encoding="utf-8",
    )
    created = json.loads(call(
        env, "mission", "create", "--prompt", "Task one", "--cwd", tmp_path, "--providers", "codex", "--native",
        "--command-template", shlex.join([sys.executable, str(provider)]), "--json",
    ))
    mission_id = created["mission"]["id"]
    database = state / "bridge.sqlite3"
    call(env, "task", "add", "--mission", mission_id, "--prompt", "Task two", "--json")
    socket_path = mission_socket_path(mission_id, env=env)
    backend = TmuxTerminalBackend()
    handle = None
    try:
        call(env, "start", "--mission", mission_id)
        wait_for(lambda: _statuses(database, mission_id)[0] == "VERIFYING", timeout=30)
        assert len(_runs(tmp_path)) == 1

        # The controller keeps holding: task 2 must not start while task 1 awaits review.
        wait_for(lambda: json.loads(call(env, "status", "--mission", mission_id, "--json")).get("hold") is not None, timeout=15)
        hold_seen = time.monotonic()
        snapshot = json.loads(call(env, "status", "--mission", mission_id, "--json"))
        assert snapshot["hold"]["reason"] == "review" and snapshot["hold"]["task_ordinal"] == 1
        assert snapshot["needs_you"]["message"].startswith("task 1 awaits your review · /approve 1 or /reject 1 reason")
        while time.monotonic() - hold_seen < 4:
            assert _statuses(database, mission_id)[0] == "VERIFYING" and _statuses(database, mission_id)[1] in {"QUEUED", "READY"}
            assert len(_runs(tmp_path)) == 1
            time.sleep(0.3)
        with BridgeStore(database) as store:
            assert [event["kind"] for event in store.events(mission_id) if event["kind"].startswith("review-hold")] == ["review-hold"]
        handle = backend.open_existing(socket_path, f"mission-{mission_id}")
        footer = "Task 1 is waiting for your review. Ctrl-] then B, then /approve or /reject."
        # The controller sets the footer text before it launches the provider; this holds on every tmux version.
        assert _review_hint(backend, handle) == footer
        if recorded_exit_status(backend, handle, timeout=15) == "0":
            wait_for(lambda: footer in backend.capture(handle, pane="provider"), timeout=15)
            assert "Provider exited (status 0)" in backend.capture(handle, pane="provider")  # the finished output is kept

        call(env, "task", "verify", "--mission", mission_id, "--task", "1", "--approve")
        wait_for(lambda: len(_runs(tmp_path)) == 2, timeout=30)
        assert footer not in backend.capture(handle, pane="provider")
        wait_for(lambda: _statuses(database, mission_id) == ["COMPLETED", "VERIFYING"], timeout=30)
        with BridgeStore(database) as store:
            events = [event for event in store.events(mission_id) if event["kind"].startswith("review-hold")]
        assert [(event["kind"], event["task_ordinal"]) for event in events[:2]] == [("review-hold", 1), ("review-hold-released", 1)]
        assert events[1]["data"]["how"] == "approved"

        call(env, "stop", "--mission", mission_id)
        wait_for(lambda: controller_released(state, mission_id))
    finally:
        try:
            call(env, "stop", "--mission", mission_id, "--force")  # a failed assertion must not leave a controller behind
        except AssertionError:
            pass
        try:
            handle = backend.open_existing(socket_path, f"mission-{mission_id}") if socket_path.exists() else None
        except (OSError, RuntimeError, PermissionError):
            handle = None
        if handle is not None:
            backend.stop(handle)


def test_review_hint_replaces_the_footer_only_after_a_clean_exit(tmp_path: Path) -> None:
    assert shutil.which("tmux")
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["/bin/sh", "-c", "exit 0"], bridge_argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path, socket_path=tmp_path / "hint.sock", session_name="hint", size=(120, 30),
    )
    try:
        assert backend.set_review_hint(handle, "Task #2 is waiting\nfor your review.")
        assert _review_hint(backend, handle) == "Task ##2 is waiting for your review."
        backend._run(handle.socket_path, "respawn-pane", "-k", "-t", handle.provider_pane, "sh -c true")
        if recorded_exit_status(backend, handle) == "0":
            wait_for(lambda: "Provider exited (status 0) · Task #2 is waiting for your review." in backend.capture(handle, pane="provider"))
        backend._run(handle.socket_path, "respawn-pane", "-k", "-t", handle.provider_pane, "sh -c 'exit 3'")
        if recorded_exit_status(backend, handle) == "3":
            wait_for(lambda: "Provider exited (status 3) · Ctrl-] then B for Bridge controls" in backend.capture(handle, pane="provider"))
        assert backend.set_review_hint(handle, "")
        assert _review_hint(backend, handle) == ""
        backend._run(handle.socket_path, "respawn-pane", "-k", "-t", handle.provider_pane, "sh -c true")
        if recorded_exit_status(backend, handle) == "0":
            wait_for(lambda: "Provider exited (status 0) · Ctrl-] then B for Bridge controls" in backend.capture(handle, pane="provider"))
    finally:
        backend.stop(handle)
