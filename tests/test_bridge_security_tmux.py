"""tmux expands `#(command)` in directory arguments and command strings; a folder name must never reach it unescaped."""
from __future__ import annotations

import os
import pty
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_tools import common, scheduler
from llm_tools.bridge import _validated_cwd
from llm_tools.terminal import TmuxTerminalBackend
from tests.conftest import wait_for as _shared_wait_for

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


def hostile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A folder whose name is a tmux command; the marker path travels in the environment."""
    marker = tmp_path / "PWNED"
    monkeypatch.setenv("PWNED_MARKER", str(marker))
    folder = tmp_path / "#(touch $PWNED_MARKER)"
    folder.mkdir()
    return folder, marker


def pane_paths(handle) -> list[str]:
    out = subprocess.run(["tmux", "-S", str(handle.socket_path), "list-panes", "-a", "-F", "#{pane_current_path}"],
                         capture_output=True, text=True, check=True).stdout
    return out.split("\n")[:-1]


def wait_for(predicate, timeout: float = 5.0) -> bool:
    return bool(_shared_wait_for(predicate, timeout, 0.05))


def test_common_tmux_literal_doubles_hashes() -> None:
    assert common.tmux_literal("a#b#(c)#{d}") == "a##b##(c)##{d}"
    assert common.tmux_literal("plain") == "plain"


def test_station_start_does_not_run_commands_embedded_in_the_folder_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder, marker = hostile(tmp_path, monkeypatch)
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["/bin/sh", "-c", "exec sleep 30"],
        bridge_argv=["/bin/sh", "-c", "exec sleep 30"],
        picker_argv=["/bin/sh", "-c", "true"],
        cwd=folder,
        socket_path=tmp_path / "s.sock",
        session_name="hostile-folder",
        size=(100, 30),
    )
    try:
        time.sleep(0.8)  # tmux runs #() jobs asynchronously
        assert not marker.exists(), "tmux executed a command taken from the folder name"
        # The panes still start in the real folder, not an expansion of its name.
        assert wait_for(lambda: pane_paths(handle) == [str(folder)] * 2), pane_paths(handle)
    finally:
        backend.stop(handle)


def test_detach_hook_does_not_run_commands_embedded_in_its_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder, marker = hostile(tmp_path, monkeypatch)
    out = folder / "ran"
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["/bin/sh", "-c", "exec sleep 30"],
        bridge_argv=["/bin/sh", "-c", "exec sleep 30"],
        cwd=tmp_path,
        socket_path=tmp_path / "s.sock",
        session_name="hostile-hook",
        size=(100, 30),
    )
    pid = -1
    try:
        backend.set_detach_command(handle, ["/bin/sh", "-c", 'echo "$1" > "$2"', "sh", "{client}", str(out)])
        pid, fd = pty.fork()
        if pid == 0:
            os.execvp("tmux", ["tmux", "-S", str(handle.socket_path), "-f", str(handle.config_path), "attach-session", "-t", handle.session_name])
        assert wait_for(lambda: backend.attached_clients(handle))
        subprocess.run(["tmux", "-S", str(handle.socket_path), "detach-client", "-s", handle.session_name], check=True)
        assert wait_for(out.exists), "the detach hook did not run its command from the literal path"
        assert not marker.exists(), "tmux executed a command taken from a hook argument"
        assert out.read_text(encoding="utf-8").strip().startswith("/dev/"), "{client} must still be substituted by tmux"
    finally:
        if pid > 0:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass
        backend.stop(handle)


def test_scheduler_respawn_and_pipe_do_not_run_commands_embedded_in_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder, marker = hostile(tmp_path, monkeypatch)
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["/bin/sh", "-c", "exit 0"],
        bridge_argv=["/bin/sh", "-c", "exec cat"],
        cwd=tmp_path,
        socket_path=tmp_path / "s.sock",
        session_name="hostile-sched",
        size=(100, 30),
    )
    try:
        logs = common.setup_run_logs(folder / "logs", "codex")
        output = logs.run_dir / "attempt-1.out"
        status = logs.run_dir / "attempt-1.status"
        cfg = scheduler.SchedulerConfig(
            provider="codex", cwd=str(folder), exec_mode="tmux",
            tmux_target=f"{handle.session_name}:station", tmux_socket=str(handle.socket_path),
            tmux_pane=handle.provider_pane, native_tui=True, env_overrides={"LLM_BRIDGE_MANAGED": "1"},
        )
        rc = scheduler.run_tmux(cfg, logs, [sys.executable, "-c", "import os; print('cwd=' + os.getcwd())"], output, status)
        time.sleep(0.8)
        assert rc == 0
        assert not marker.exists(), "tmux executed a command taken from the folder name"
        assert f"cwd={folder}" in output.read_text(encoding="utf-8")
    finally:
        backend.stop(handle)


@pytest.mark.parametrize("name", ["#(touch x)", "a#{pane_id}b"])
def test_mission_folder_with_tmux_format_syntax_is_refused(tmp_path: Path, name: str) -> None:
    folder = tmp_path / name
    folder.mkdir()
    with pytest.raises(ValueError, match="tmux"):
        _validated_cwd(str(folder))


def test_ordinary_hash_in_a_folder_name_is_accepted_and_survives_tmux(tmp_path: Path) -> None:
    folder = tmp_path / "C# notes #1"
    folder.mkdir()
    assert _validated_cwd(str(folder)) == str(folder)
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["/bin/sh", "-c", "exec sleep 30"], bridge_argv=["/bin/sh", "-c", "exec sleep 30"],
        cwd=folder, socket_path=tmp_path / "s.sock", session_name="hash-folder", size=(100, 30),
    )
    try:
        assert wait_for(lambda: pane_paths(handle) == [str(folder)] * 2), pane_paths(handle)
    finally:
        backend.stop(handle)
