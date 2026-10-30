"""The tmux backend verifies a station once and starts few tmux processes per operation."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from llm_tools import common, terminal
from llm_tools.terminal import TmuxTerminalBackend

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


class TmuxCalls:
    """Records every tmux process the backend starts."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.argvs: list[list[str]] = []
        real_run = subprocess.run

        def counting_run(command, *args, **kwargs):
            if isinstance(command, list) and command and os.path.basename(str(command[0])) == "tmux":
                self.argvs.append([str(part) for part in command])
            return real_run(command, *args, **kwargs)

        monkeypatch.setattr(terminal.subprocess, "run", counting_run)

    def subcommands(self) -> list[str]:
        names = []
        for argv in self.argvs:
            rest = [part for index, part in enumerate(argv) if part not in {"-S", "-f"} and argv[index - 1] not in {"-S", "-f"}]
            names.append(rest[1] if len(rest) > 1 else "")
        return names

    def clear(self) -> None:
        self.argvs.clear()


def _station(tmp_path: Path, backend: TmuxTerminalBackend | None = None, name: str = "cost"):
    backend = backend or TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["sleep", "120"],
        bridge_argv=["sleep", "120"],
        cwd=tmp_path,
        socket_path=tmp_path / f"{name}.sock",
        session_name=f"mission-{name}",
        picker_argv=["sleep", "1"],
    )
    return backend, handle


def test_station_start_uses_few_tmux_processes_and_keeps_every_binding(tmp_path: Path, monkeypatch) -> None:
    calls = TmuxCalls(monkeypatch)
    backend, handle = _station(tmp_path)
    try:
        started = list(calls.subcommands())
        assert len(started) <= 4, started
        assert started.count("new-session") == 1

        def tmux(*args: str) -> str:
            return subprocess.run(["tmux", "-S", str(handle.socket_path), *args], capture_output=True, text=True, check=True).stdout

        prefix = tmux("list-keys", "-T", "prefix")
        for key in ("B", "b", "P", "p", "m", "M", "?"):
            assert any(line.split()[3:4] == [key] for line in prefix.splitlines() if line.startswith("bind-key")), (key, prefix)
        assert "display-popup" in prefix
        root = tmux("list-keys", "-T", "root")
        for name in ("MouseDown1Pane", "MouseUp1StatusLeft", "MouseUp1StatusRight", "MouseUp1Status", "WheelUpPane"):
            assert name in root
        assert handle.provider_pane == "%0" and handle.bridge_pane == "%1"
        assert tmux("show-options", "-gqv", "@llm_bridge_station").strip() == "1"
    finally:
        backend.stop(handle)


def test_resize_hook_runs_inside_tmux_and_restores_the_command_pane_and_zoom(tmp_path: Path) -> None:
    backend, handle = _station(tmp_path, name="hook")
    try:
        def tmux(*args: str) -> str:
            return subprocess.run(["tmux", "-S", str(handle.socket_path), *args], capture_output=True, text=True, check=True).stdout.strip()

        hook = tmux("show-hooks", "-g", "client-resized")
        assert "sleep" not in hook, "the resize hook must not start a shell job on tmux 3.2+"
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        client = subprocess.Popen(
            ["tmux", "-S", str(handle.socket_path), "attach-session", "-t", handle.session_name],
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True, env={**os.environ, "TERM": "xterm-256color"},
        )

        def pump(seconds: float) -> None:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if select.select([master], [], [], 0.05)[0]:
                    os.read(master, 65536)

        def geometry() -> tuple[str, str]:
            height = tmux("display-message", "-p", "-t", handle.bridge_pane, "#{pane_height}")
            return height, tmux("display-message", "-p", "-t", handle.session_name + ":station", "#{window_zoomed_flag}")

        try:
            pump(1.0)
            for rows, cols in ((24, 80), (32, 100), (40, 120)):
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                os.killpg(client.pid, signal.SIGWINCH)
                deadline = time.monotonic() + 5
                while geometry() != ("6", "1") and time.monotonic() < deadline:
                    pump(0.1)
                assert geometry() == ("6", "1"), (rows, cols, geometry())  # zoomed station keeps its zoom and command-pane height
            tmux("resize-pane", "-Z", "-t", handle.provider_pane)  # unzoom
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
            os.killpg(client.pid, signal.SIGWINCH)
            deadline = time.monotonic() + 5
            while geometry() != ("6", "0") and time.monotonic() < deadline:
                pump(0.1)
            assert geometry() == ("6", "0")
        finally:
            os.killpg(client.pid, signal.SIGTERM)
            client.wait(timeout=5)
            os.close(master)
            os.close(slave)
    finally:
        backend.stop(handle)


def test_ownership_is_verified_once_not_on_every_operation(tmp_path: Path, monkeypatch) -> None:
    backend, handle = _station(tmp_path, name="owned")
    try:
        assert backend.server_owned(handle.socket_path)
        calls = TmuxCalls(monkeypatch)
        for _ in range(5):
            assert backend.pane_is_alive(handle, pane="provider")
            assert backend.session_exists(handle)
            backend.capture(handle, pane="provider")
        assert "show-option" not in calls.subcommands(), calls.subcommands()
        assert len(calls.argvs) == 15  # one tmux process for each of the 15 operations, none for re-verification
    finally:
        backend.stop(handle)


def test_cached_ownership_is_dropped_when_the_server_goes_away_or_is_replaced(tmp_path: Path) -> None:
    backend, handle = _station(tmp_path, name="gone")
    assert backend.server_owned(handle.socket_path)
    first_identity = terminal._socket_identity(handle.socket_path)
    subprocess.run(["tmux", "-S", str(handle.socket_path), "kill-server"], check=True)
    deadline = time.monotonic() + 3
    while backend.server_owned(handle.socket_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not backend.server_owned(handle.socket_path)
    assert not backend.pane_is_alive(handle, pane="provider")
    with pytest.raises(PermissionError):
        backend.capture(handle, pane="provider")
    backend.stop(handle)  # removes the dead socket and configuration

    backend2, handle2 = _station(tmp_path, backend, name="gone")  # same path, new server
    try:
        assert terminal._socket_identity(handle2.socket_path) != first_identity
        assert backend2.server_owned(handle2.socket_path)
        assert backend2.pane_is_alive(handle2, pane="provider")
    finally:
        backend2.stop(handle2)


def test_open_existing_reuses_a_verified_handle_without_starting_tmux(tmp_path: Path, monkeypatch) -> None:
    backend, handle = _station(tmp_path, name="handle")
    try:
        first = backend.open_existing(handle.socket_path, handle.session_name)
        calls = TmuxCalls(monkeypatch)
        again = backend.open_existing(handle.socket_path, handle.session_name)
        assert again == first == handle
        assert calls.argvs == []

        monkeypatch.setattr(terminal, "_HANDLE_CACHE_SECONDS", 0.0)
        terminal._STATION_HANDLES.clear()
        backend.open_existing(handle.socket_path, handle.session_name)  # rebuilt from tmux
        backend.open_existing(handle.socket_path, handle.session_name)  # expired at once, so rebuilt again
        assert calls.subcommands().count("list-panes") == 2
    finally:
        backend.stop(handle)


def test_open_existing_does_not_serve_a_cached_handle_after_the_server_dies(tmp_path: Path) -> None:
    backend, handle = _station(tmp_path, name="dead")
    backend.open_existing(handle.socket_path, handle.session_name)
    subprocess.run(["tmux", "-S", str(handle.socket_path), "kill-server"], check=True)
    deadline = time.monotonic() + 3
    while terminal.unix_socket_accepts(handle.socket_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    with pytest.raises(terminal.DeadStationError):
        backend.open_existing(handle.socket_path, handle.session_name)


def test_publish_status_sends_only_changed_options_in_one_invocation(tmp_path: Path, monkeypatch) -> None:
    backend, handle = _station(tmp_path, name="status")
    try:
        segments = [("Task 1 ", "info"), ("RUNNING", "ok")]
        calls = TmuxCalls(monkeypatch)
        assert backend.publish_status(handle, label="repo", context="Task 1 RUNNING", chip=1, segments=segments)
        assert calls.subcommands() == ["set-option"]  # four options, one process
        assert calls.argvs[0].count("set-option") == 4

        calls.clear()
        assert not backend.publish_status(handle, label="repo", context="Task 1 RUNNING", chip=1, segments=segments)
        assert calls.argvs == []

        assert backend.publish_status(handle, label="repo", context="Task 1 no output for 30s", chip=1,
                                      segments=[("Task 1 ", "info"), ("no output for 30s", "muted")])
        assert calls.subcommands() == ["set-option"]
        assert calls.argvs[0].count("set-option") == 2  # the plain and the coloured text; label and chip are unchanged

        def option(name: str) -> str:
            return subprocess.run(["tmux", "-S", str(handle.socket_path), "show-options", "-gqv", name], capture_output=True, text=True).stdout.strip()

        assert option("@llm_bridge_label") == "repo"
        assert option("@llm_bridge_context") == "Task 1 no output for 30s"
        assert "no output for 30s" in option("@llm_bridge_context_styled")
        assert option("@llm_bridge_chip").startswith("#[")
    finally:
        backend.stop(handle)


def test_publish_status_forgets_what_it_wrote_when_the_station_is_replaced(tmp_path: Path) -> None:
    backend, handle = _station(tmp_path, name="again")
    backend.publish_status(handle, label="repo", context="Task 1 RUNNING", chip=1, segments=[("Task 1 RUNNING", "info")])
    backend.stop(handle)
    backend2, handle2 = _station(tmp_path, backend, name="again")
    try:
        assert backend2.publish_status(handle2, label="repo", context="Task 1 RUNNING", chip=1, segments=[("Task 1 RUNNING", "info")])
    finally:
        backend2.stop(handle2)


def test_publish_status_sanitises_text_so_it_cannot_split_the_tmux_command(tmp_path: Path) -> None:
    backend, handle = _station(tmp_path, name="clean")
    try:
        backend.publish_status(handle, label="a;b", context="x ; kill-server ;", chip=1, segments=[("y;", "info")])
        assert backend.server_owned(handle.socket_path)
        value = subprocess.run(["tmux", "-S", str(handle.socket_path), "show-options", "-gqv", "@llm_bridge_context"], capture_output=True, text=True).stdout
        assert ";" not in value
    finally:
        backend.stop(handle)


def test_pane_activity_screen_captures_only_when_the_epoch_moved(tmp_path: Path, monkeypatch) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["/bin/sh", "-c", "echo hello-provider; exec sleep 120"], bridge_argv=["sleep", "120"],
        cwd=tmp_path, socket_path=tmp_path / "act.sock", session_name="mission-act",
    )
    try:
        deadline = time.monotonic() + 3
        reading = None
        while time.monotonic() < deadline:
            reading = backend.pane_activity_screen(handle)
            if reading and reading[1] and "hello-provider" in reading[1]:
                break
            time.sleep(0.05)
        assert reading is not None and reading[1] is not None and "hello-provider" in reading[1]
        epoch = reading[0]
        calls = TmuxCalls(monkeypatch)
        unchanged = backend.pane_activity_screen(handle, seen_epoch=epoch)
        assert unchanged == (epoch, None)
        assert len(calls.argvs) == 1  # epoch and the conditional capture share one process
        stale = backend.pane_activity_screen(handle, seen_epoch=epoch - 5)
        assert stale is not None and stale[1] is not None and "hello-provider" in stale[1]
    finally:
        backend.stop(handle)


def test_pane_activity_screen_reports_a_dead_pane_as_unreadable(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["/bin/sh", "-c", "exit 0"], bridge_argv=["sleep", "120"],
        cwd=tmp_path, socket_path=tmp_path / "dead.sock", session_name="mission-dead",
    )
    try:
        deadline = time.monotonic() + 3
        while backend.pane_is_alive(handle, pane="provider") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert backend.pane_activity_screen(handle) is None
    finally:
        backend.stop(handle)


def test_tmux_is_looked_up_once_per_path_value(tmp_path: Path, monkeypatch) -> None:
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "tmux").write_text("#!/bin/sh\nexit 0\n")
    (fake / "tmux").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    assert common.resolve_executable("tmux") == str(fake / "tmux")
    lookups: list[str] = []
    real_which = shutil.which
    monkeypatch.setattr(common.shutil, "which", lambda *a, **k: lookups.append(a[0]) or real_which(*a, **k))
    for _ in range(3):
        assert common.resolve_executable("tmux") == str(fake / "tmux")
    assert lookups == []  # remembered for this PATH
    monkeypatch.setenv("PATH", "/nonexistent")
    assert common.resolve_executable("tmux") == "tmux"  # not found: the bare name, so callers see the usual FileNotFoundError
    assert lookups == ["tmux"]


def test_switcher_without_picker_command_is_bound_after_start_and_help_is_in_the_config(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["sleep", "120"], bridge_argv=["sleep", "120"], cwd=tmp_path,
        socket_path=tmp_path / "nopicker.sock", session_name="mission-nopicker",
    )
    try:
        config = handle.config_path.read_text()
        assert "bind-key -T prefix ? display-message" in config
        assert "display-popup" not in config
        keys = subprocess.run(["tmux", "-S", str(handle.socket_path), "list-keys", "-T", "prefix", "m"], capture_output=True, text=True).stdout
        assert "send-keys" in keys and "/missions" in keys
    finally:
        backend.stop(handle)
