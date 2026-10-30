from __future__ import annotations

import subprocess
from pathlib import Path
import shutil
import sys

import pytest

from llm_tools.bridge_tui import _station_status_context
from llm_tools.terminal import STATUS_CONTEXT_FORMAT, TerminalHandle, TmuxTerminalBackend


def test_station_context_reports_current_task_and_mission_state() -> None:
    assert _station_status_context({
        "mission": {"status": "PAUSED"},
        "current_task": {"id": 17, "status": "RUNNING", "title": "ignored from compact line"},
    }) == "Task 17 RUNNING · PAUSED"


def test_station_context_reports_no_current_task() -> None:
    assert _station_status_context({"mission": {"status": "COMPLETED"}}) == "No task · COMPLETED"


def test_station_context_is_short_when_mission_is_running() -> None:
    running = {"mission": {"status": "RUNNING"}, "current_task": {"id": 1, "status": "RUNNING"}}
    assert _station_status_context(running) == "Task 1 RUNNING"
    assert _station_status_context({"mission": {"status": "RUNNING"}}) == "No task · RUNNING"
    assert _station_status_context({}) == "No task"
    assert _station_status_context({**running, "mission": {"status": "STOPPING"}}) == "Task 1 RUNNING · STOPPING"


def test_status_context_is_sanitized_and_written_to_owned_server(monkeypatch) -> None:
    backend = TmuxTerminalBackend()
    handle = TerminalHandle(Path("/tmp/bridge.sock"), Path("/tmp/bridge.conf"), "mission-x", "%0", "%1", 6)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(backend, "_require_owned", lambda _handle: None)
    monkeypatch.setattr(
        backend,
        "_run",
        lambda _socket, *args, **_kwargs: calls.append(args) or subprocess.CompletedProcess([], 0),
    )

    backend.set_status_context(handle, "Task 4 RUNNING #{status-left}\nInjected")

    assert calls == [("set-option", "-g", "@llm_bridge_context", "Task 4 RUNNING status-left Injected")]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required for status rendering")
def test_status_context_appears_in_real_station_window_label(tmp_path) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        bridge_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        socket_path=tmp_path / "context.sock",
        session_name="context",
        size=(80, 24),
    )
    try:
        backend.set_status_context(handle, "Task 7 VERIFYING / Mission PAUSED")
        rendered = backend._run(handle.socket_path, "display-message", "-p", "-t", handle.session_name,
                                "#{@llm_bridge_context}", capture=True).stdout.strip()
        status_format = backend._run(handle.socket_path, "show-options", "-gv", "window-status-current-format",
                                     capture=True).stdout.strip()
        assert rendered == "Task 7 VERIFYING / Mission PAUSED"
        assert status_format == STATUS_CONTEXT_FORMAT
    finally:
        backend.stop(handle)
