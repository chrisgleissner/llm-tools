"""A failed attach reclaims a dead socket only while no controller owns the mission lock."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from llm_tools.bridge_store import MissionLock, default_state_dir
from llm_tools.terminal import TerminalHandle, TmuxTerminalBackend

MISSION = "0123456789abcdef0123456789abcdef"


def failing_attach_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session: str) -> tuple[TmuxTerminalBackend, TerminalHandle, list[Path]]:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    socket_path = tmp_path / "station.sock"
    handle = TerminalHandle(socket_path, socket_path.with_name("station.sock.conf"), session, "%0", "%1", 6)
    backend = TmuxTerminalBackend()
    reclaimed: list[Path] = []
    monkeypatch.setattr(backend, "_require_owned", lambda h: None)
    monkeypatch.setattr(backend, "reclaim_dead_station", lambda path: reclaimed.append(Path(path)) or True)
    monkeypatch.setattr(
        "llm_tools.terminal.subprocess.run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout="", stderr=""),
    )
    return backend, handle, reclaimed


def test_failed_attach_does_not_reclaim_while_the_mission_lock_is_held(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backend, handle, reclaimed = failing_attach_backend(monkeypatch, tmp_path, f"mission-{MISSION}")
    controller = MissionLock(default_state_dir() / "locks", MISSION)
    controller.acquire()
    try:
        assert backend.attach(handle) == 1
    finally:
        controller.release()
    assert reclaimed == []


def test_failed_attach_reclaims_under_the_mission_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backend, handle, reclaimed = failing_attach_backend(monkeypatch, tmp_path, f"mission-{MISSION}")
    seen_held: list[bool] = []
    monkeypatch.setattr(
        backend, "reclaim_dead_station",
        lambda path: seen_held.append(MissionLock.is_held(default_state_dir() / "locks", MISSION)) or True,
    )
    assert backend.attach(handle) == 1
    assert seen_held == [True], "reclaim ran without holding the mission lock"
    assert not MissionLock.is_held(default_state_dir() / "locks", MISSION), "the lock is released afterwards"


def test_failed_attach_to_a_station_that_is_not_a_mission_still_reclaims(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backend, handle, reclaimed = failing_attach_backend(monkeypatch, tmp_path, "gone")
    assert backend.attach(handle) == 1
    assert reclaimed == [handle.socket_path]
