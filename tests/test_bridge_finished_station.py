"""A finished mission's idle tmux station closes itself; a live provider or client keeps it."""

from __future__ import annotations

import os
import pty
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_tools import bridge_runtime
from llm_tools.bridge import main
from llm_tools.bridge_runtime import close_finished_station, gc_finished_station, mission_socket_path
from llm_tools.bridge_store import BridgeStore, MissionLock
from llm_tools.terminal import TerminalHandle, TmuxTerminalBackend
from tests.conftest import wait_for as _shared_wait_for

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")

_SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
_EXIT = ["/bin/sh", "-c", "exit 0"]


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    return directory


def _wait_for(predicate, timeout: float = 6.0) -> bool:
    return bool(_shared_wait_for(predicate, timeout, 0.05))


def _mission(state: Path, tmp_path: Path, status: str, **config) -> str:
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission_id = store.create_mission("station", "objective", cwd=tmp_path, initial_prompt="work", config=config)["id"]
        if status != "CREATED":
            store.set_mission_status(mission_id, "RUNNING")
        if status in {"COMPLETED", "FAILED"}:
            # A direct update: this suite tests station cleanup, not task completion rules.
            store.db.execute("UPDATE missions SET status=? WHERE id=?", (status, mission_id))
            store.db.commit()
    return mission_id


def _station(tmp_path: Path, mission_id: str, provider: list[str]) -> tuple[TmuxTerminalBackend, TerminalHandle]:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=provider, bridge_argv=_SLEEP, cwd=tmp_path,
        socket_path=mission_socket_path(mission_id), session_name=bridge_runtime._session_name(mission_id),
    )
    return backend, handle


class _Client:
    """A real tmux client on a pseudo-terminal, as an operator attached to the station."""

    def __init__(self, handle: TerminalHandle) -> None:
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # pragma: no cover - replaced by exec
            os.execvpe("tmux", ["tmux", "-S", str(handle.socket_path), "-f", str(handle.config_path), "attach-session", "-t", handle.session_name], {**os.environ, "TERM": "xterm-256color"})  # tmux refuses to attach without a usable TERM, which CI runners do not set
        self.handle = handle

    def detach(self) -> None:
        subprocess.run(["tmux", "-S", str(self.handle.socket_path), "detach-client"], check=False, capture_output=True)

    def close(self) -> None:
        for action in (lambda: os.kill(self.pid, 15), lambda: os.waitpid(self.pid, 0), lambda: os.close(self.fd)):
            try:
                action()
            except OSError:
                pass


def test_idle_station_of_a_completed_mission_is_closed_and_recorded(tmp_path, state) -> None:
    mission_id = _mission(state, tmp_path, "COMPLETED", native_tui=True)
    backend, handle = _station(tmp_path, mission_id, _EXIT)
    try:
        assert _wait_for(lambda: not backend.pane_is_alive(handle, pane="provider"))
        with BridgeStore(state / "bridge.sqlite3") as store:
            assert close_finished_station(store, backend, handle, mission_id) == "closed"
            assert any(event["kind"] == "station-stopped" and event["data"]["source"] == "finished-mission" for event in store.events(mission_id))
        assert not backend.session_exists(handle) and not handle.socket_path.exists()
    finally:
        backend.stop(handle)


def test_failed_mission_station_closes_the_same_way(tmp_path, state) -> None:
    mission_id = _mission(state, tmp_path, "FAILED", native_tui=True)
    backend, handle = _station(tmp_path, mission_id, _EXIT)
    try:
        assert _wait_for(lambda: not backend.pane_is_alive(handle, pane="provider"))
        assert gc_finished_station(mission_id) == "closed"
        assert not handle.socket_path.exists()
    finally:
        backend.stop(handle)


def test_a_live_native_provider_is_never_killed(tmp_path, state) -> None:
    mission_id = _mission(state, tmp_path, "COMPLETED", native_tui=True)
    backend, handle = _station(tmp_path, mission_id, _SLEEP)
    try:
        assert gc_finished_station(mission_id) == "provider-alive"
        assert backend.pane_is_alive(handle, pane="provider")
    finally:
        backend.stop(handle)


def test_autonomous_station_closes_although_its_pane_tails_a_log(tmp_path, state) -> None:
    mission_id = _mission(state, tmp_path, "COMPLETED", native_tui=False)
    backend, handle = _station(tmp_path, mission_id, _SLEEP)
    try:
        assert gc_finished_station(mission_id) == "closed"
        assert not handle.socket_path.exists()
    finally:
        backend.stop(handle)


def test_unfinished_mission_and_active_controller_leave_the_station_alone(tmp_path, state) -> None:
    mission_id = _mission(state, tmp_path, "RUNNING", native_tui=True)
    backend, handle = _station(tmp_path, mission_id, _EXIT)
    try:
        assert gc_finished_station(mission_id) == "not-finished"
        with BridgeStore(state / "bridge.sqlite3") as store:
            store.db.execute("UPDATE missions SET status='COMPLETED' WHERE id=?", (mission_id,))
            store.db.commit()
        lock = MissionLock(state / "locks", mission_id)
        lock.acquire()
        try:
            assert gc_finished_station(mission_id) == "controller-active"
        finally:
            lock.release()
        assert backend.session_exists(handle)
    finally:
        backend.stop(handle)


def test_missing_station_is_reported_not_created(tmp_path, state) -> None:
    mission_id = _mission(state, tmp_path, "COMPLETED")
    assert gc_finished_station(mission_id) == "no-station"


def test_attached_client_keeps_the_station_until_the_last_one_detaches(tmp_path, state) -> None:
    mission_id = _mission(state, tmp_path, "COMPLETED", native_tui=True)
    backend, handle = _station(tmp_path, mission_id, _EXIT)
    backend.set_detach_command(handle, bridge_runtime.station_detach_argv(mission_id))
    client = _Client(handle)
    try:
        assert _wait_for(lambda: backend.attached_clients(handle) != ())
        with BridgeStore(state / "bridge.sqlite3") as store:
            assert close_finished_station(store, backend, handle, mission_id) == "clients-attached"
        assert backend.session_exists(handle)
        client.detach()
        assert _wait_for(lambda: not handle.socket_path.exists()), "detaching the last client must close the finished mission's station"
    finally:
        client.close()
        backend.stop(handle)


def test_detach_hook_does_not_close_a_station_whose_mission_is_still_running(tmp_path, state) -> None:
    mission_id = _mission(state, tmp_path, "RUNNING", native_tui=True)
    backend, handle = _station(tmp_path, mission_id, _EXIT)
    backend.set_detach_command(handle, bridge_runtime.station_detach_argv(mission_id))
    client = _Client(handle)
    try:
        assert _wait_for(lambda: backend.attached_clients(handle) != ())
        client.detach()
        time.sleep(1.0)
        assert backend.session_exists(handle)
    finally:
        client.close()
        backend.stop(handle)


def test_excluded_client_is_ignored_even_if_tmux_still_lists_it(tmp_path, state) -> None:
    mission_id = _mission(state, tmp_path, "COMPLETED", native_tui=True)
    backend, handle = _station(tmp_path, mission_id, _EXIT)
    client = _Client(handle)
    try:
        assert _wait_for(lambda: backend.attached_clients(handle) != ())
        (name,) = backend.attached_clients(handle)
        assert backend.attached_clients(handle, exclude=(name,)) == ()
        assert gc_finished_station(mission_id, (name,)) == "closed"
    finally:
        client.close()
        backend.stop(handle)


def test_station_gc_command_prints_the_outcome(tmp_path, state, capsys) -> None:
    mission_id = _mission(state, tmp_path, "COMPLETED")
    assert main(["_station-gc", "--mission", mission_id, "--exclude-client", "/dev/pts/9"]) == 0
    assert capsys.readouterr().out.strip() == "no-station"


def test_attach_to_a_finished_mission_explains_instead_of_suggesting_start(tmp_path, state, capsys) -> None:
    mission_id = _mission(state, tmp_path, "COMPLETED")
    assert main(["attach", "--mission", mission_id]) == 2
    err = capsys.readouterr().err
    assert f"Mission {mission_id[:8]} is completed and its station was closed" in err and "llm-bridge start" not in err


# --- idle provider pane message ---------------------------------------------------


def _pane_text(handle: TerminalHandle) -> str:
    return subprocess.run(["tmux", "-S", str(handle.socket_path), "capture-pane", "-p", "-S", "-100", "-t", handle.provider_pane], text=True, capture_output=True, check=True).stdout


def test_message_is_shown_in_an_empty_pane_and_refreshed_but_never_replaces_a_provider(tmp_path) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(provider_argv=_EXIT, bridge_argv=_SLEEP, cwd=tmp_path, socket_path=tmp_path / "msg.sock", session_name="msg")
    try:
        assert _wait_for(lambda: not backend.pane_is_alive(handle, pane="provider"))
        text = "Waiting for capacity: copilot monthly quota resets in 2d 9h. Ctrl-] then B for options."
        assert backend.show_provider_message(handle, text + "\x1b[31m\n")
        assert _wait_for(lambda: text in _pane_text(handle)), _pane_text(handle)
        assert "Provider exited" not in _pane_text(handle)
        assert not backend.pane_runs_provider(handle), "the message pane is not a provider"
        newer = "Waiting for capacity: claude 5h quota resets in 1h. Ctrl-] then B for options."
        assert backend.show_provider_message(handle, newer)
        assert _wait_for(lambda: newer in _pane_text(handle)) and text not in _pane_text(handle)
        # A launched provider replaces the message (the scheduler respawns the pane) and is then protected.
        subprocess.run(["tmux", "-S", str(handle.socket_path), "respawn-pane", "-k", "-t", handle.provider_pane, "echo provider started; sleep 30"], check=True)
        assert _wait_for(lambda: "provider started" in _pane_text(handle))
        assert "Waiting for capacity" not in _pane_text(handle)
        assert backend.pane_runs_provider(handle)
        assert not backend.show_provider_message(handle, "must not appear")
        assert "must not appear" not in _pane_text(handle)
    finally:
        backend.stop(handle)


def test_message_does_not_overwrite_a_previous_providers_output(tmp_path) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(provider_argv=["/bin/sh", "-c", "echo previous provider output"], bridge_argv=_SLEEP, cwd=tmp_path, socket_path=tmp_path / "keep.sock", session_name="keep")
    try:
        assert _wait_for(lambda: not backend.pane_is_alive(handle, pane="provider"))
        assert not backend.show_provider_message(handle, "Waiting for capacity")
        assert "previous provider output" in _pane_text(handle)
    finally:
        backend.stop(handle)


def test_finished_mission_station_closes_over_a_waiting_message_pane(tmp_path, state) -> None:
    mission_id = _mission(state, tmp_path, "RUNNING", native_tui=True)
    backend, handle = _station(tmp_path, mission_id, _EXIT)
    try:
        assert _wait_for(lambda: not backend.pane_is_alive(handle, pane="provider"))
        assert backend.show_provider_message(handle, "Waiting for capacity: x")
        with BridgeStore(state / "bridge.sqlite3") as store:
            store.db.execute("UPDATE missions SET status='CANCELLED' WHERE id=?", (mission_id,))
            store.db.commit()
        assert gc_finished_station(mission_id) == "closed"
    finally:
        backend.stop(handle)


# --- the scheduler may replace only the message pane -----------------------------------


class _Completed:
    def __init__(self, stdout: str = "") -> None:
        self.returncode = 0
        self.stdout = stdout


@pytest.mark.parametrize(("start_command", "expected"), [("printf x; exec sleep 99 # llm-bridge-wait", 0), ("codex --resume", 75)])
def test_scheduler_replaces_a_live_pane_only_when_it_is_the_waiting_message(tmp_path, monkeypatch, start_command, expected) -> None:
    from llm_tools import common, scheduler

    monkeypatch.setattr(common, "have_cmd", lambda _name: True)
    respawned: list[list[str]] = []
    status = {}

    def fake_tmux(argv, **_kwargs):
        command = argv[3]
        if command == "display-message":
            fmt = argv[-1]
            if fmt == "#{pane_dead}\t#{pane_start_command}":  # one process reads both
                return _Completed(f"0\t{start_command}")
            if fmt == "#{pane_dead}":
                return _Completed("0" if not respawned else "0")
            if fmt == "#{pane_start_command}":
                return _Completed(start_command)
        if command == "respawn-pane":
            respawned.append(argv)
            status["file"].write_text("0", encoding="utf-8")
        return _Completed()

    monkeypatch.setattr(scheduler.subprocess, "run", fake_tmux)
    monkeypatch.setattr(scheduler.time, "sleep", lambda _seconds: None)
    logs = common.setup_run_logs(tmp_path / "logs", "codex")
    output, status["file"] = logs.run_dir / "attempt.out", logs.run_dir / "attempt.status"
    cfg = scheduler.SchedulerConfig(provider="codex", cwd=str(tmp_path), exec_mode="tmux", tmux_target="bridge:station", tmux_socket=str(tmp_path / "b.sock"), tmux_pane="%1", native_tui=True)
    assert scheduler.run_tmux(cfg, logs, ["true"], output, status["file"]) == expected
    assert bool(respawned) == (expected == 0)


def test_detach_hook_output_never_puts_the_remaining_client_into_view_mode(tmp_path, state) -> None:
    """The hook's status word once surfaced as tmux view mode in the other client's pane, swallowing keys."""
    mission_id = _mission(state, tmp_path, "RUNNING", native_tui=True)
    backend, handle = _station(tmp_path, mission_id, _EXIT)
    backend.set_detach_command(handle, bridge_runtime.station_detach_argv(mission_id))
    staying = _Client(handle)
    leaving = _Client(handle)
    try:
        assert _wait_for(lambda: len(backend.attached_clients(handle)) == 2)
        leaving.detach()
        assert _wait_for(lambda: len(backend.attached_clients(handle)) == 1)
        time.sleep(1.0)  # let the background hook finish and print, if it would
        in_mode = backend._run(
            handle.socket_path, "list-panes", "-s", "-t", handle.session_name, "-F", "#{pane_in_mode}",
            check=False, capture=True,
        ).stdout.split()
        assert in_mode and set(in_mode) == {"0"}, f"a pane entered view mode after detach: {in_mode}"
    finally:
        leaving.close()
        staying.close()
        backend.stop(handle)
