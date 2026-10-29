"""Real tmux PTY contract tests for the native provider terminal backend."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_tools.terminal import TmuxTerminalBackend, TerminalHandle


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def _fake_native_cli(path: Path) -> list[str]:
    path.write_text(
        "import os, sys, time, termios, fcntl, struct\n"
        "sys.stdout.write('READY\\x1b[31m RED \\x1b[0m\\x1b[?1049hALT-SCREEN\\n'); sys.stdout.flush()\n"
        "line = sys.stdin.readline().rstrip('\\n')\n"
        "size = struct.unpack('HHHH', fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\\0'*8))[:2]\n"
        "print('INPUT=' + line + ' SIZE=' + str(size[0]) + 'x' + str(size[1]), flush=True); time.sleep(0.3)\n"
        "sys.stdout.write('\\x1b[?1049l'); sys.stdout.flush()\n",
        encoding="utf-8",
    )
    return [sys.executable, str(path)]


def test_private_tmux_station_preserves_native_terminal_io_and_cleans_up(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle: TerminalHandle | None = None
    try:
        provider = _fake_native_cli(tmp_path / "fake_cli.py")
        bridge = ["/bin/sh", "-c", "exec cat"]
        handle = backend.start(
            provider_argv=provider,
            bridge_argv=bridge,
            cwd=tmp_path,
            socket_path=tmp_path / "tmux.sock",
            session_name="llm-bridge-test",
            size=(100, 30),
        )

        assert _wait_for(lambda: "ALT-SCREEN" in backend.capture(handle, pane="provider"))
        assert "READY" in backend.capture(handle, pane="provider", alternate_screen=True)
        assert "\x1b[31m" in backend.capture(handle, pane="provider", alternate_screen=True, ansi=True)
        options = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "show-option", "-g", "-v", "prefix"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert options == "C-]"
        secondary_prefix = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "show-option", "-g", "-v", "prefix2"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert secondary_prefix == "None"
        focus_events = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "show-option", "-g", "-v", "focus-events"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert focus_events == "on"
        status = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "show-option", "-g", "-v", "status"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert status == "on"
        status_left = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "show-option", "-g", "-v", "status-left"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        status_right = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "show-option", "-g", "-v", "status-right"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        assert "Bridge: Ctrl-] then B" in status_left and "pane_index" in status_left
        assert "Provider: Ctrl-] then P" in status_right and "pane_index" in status_right
        root_bindings = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "list-keys", "-T", "root"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        assert "MouseDown1Pane" in root_bindings and "select-pane -t = \\; send-keys -M \\; select-pane -t =" in root_bindings
        assert "WheelUpPane" in root_bindings and "WheelDownPane" in root_bindings
        root_bindings = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "list-keys", "-T", "root"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        prefix_bindings = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "list-keys", "-T", "prefix"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        for key, pane in (("MouseUp1StatusLeft", handle.bridge_pane), ("MouseUp1StatusRight", handle.provider_pane)):
            binding = next(line for line in root_bindings.splitlines() if key in line)
            assert f"select-pane -t \"{pane}\" \\; resize-pane -Z -t \"{pane}\"" in binding
        assert "B" in prefix_bindings and "P" in prefix_bindings

        backend.resize(handle, cols=120, rows=40)
        backend.send_keys(handle, pane="provider", keys="hello")
        backend.send_keys(handle, pane="provider", keys="Enter")
        assert _wait_for(lambda: "INPUT=hello SIZE=" in backend.capture(handle, pane="provider"))
        provider_size = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "display-message", "-p", "-t", handle.provider_pane, "#{pane_width}x#{pane_height}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert provider_size == "120x40"
        assert handle.config_path.stat().st_mode & 0o777 == 0o600

        assert backend.is_alive(handle)
        assert backend.pane_is_alive(handle, pane="provider")
        assert backend.capture(handle, pane="bridge") is not None
    finally:
        if handle is not None:
            backend.stop(handle)
    assert not (tmp_path / "tmux.sock").exists()
    assert handle is not None and not backend.server_exists(handle)


def test_terminal_backend_uses_only_its_private_tmux_server(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        bridge_argv=["/bin/sh", "-c", "exec cat"],
        cwd=tmp_path,
        socket_path=tmp_path / "isolated.sock",
        session_name="isolated-bridge",
            prefix="C-a",
    )
    try:
        sessions = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "list-sessions", "-F", "#S"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
        assert sessions == [handle.session_name]
        prefix = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "show-option", "-g", "-v", "prefix"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert prefix == "C-a"
        reopened = backend.open_existing(handle.socket_path, handle.session_name)
        assert reopened == handle
        assert backend.server_owned(handle.socket_path)
    finally:
        backend.stop(handle)


def test_focus_helper_expands_the_selected_owned_pane(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        bridge_argv=["/bin/sh", "-c", "exec cat"],
        cwd=tmp_path,
        socket_path=tmp_path / "focus.sock",
        session_name="focus-test",
    )

    def value(*args: str) -> str:
        return subprocess.run(
            ["tmux", "-S", str(handle.socket_path), *args], check=True, text=True, capture_output=True,
        ).stdout.strip()

    try:
        backend.focus(handle, pane="provider", expand=True)
        assert value("display-message", "-p", "#{pane_id}") == handle.provider_pane
        assert value("display-message", "-p", "#{window_zoomed_flag}") == "1"
        backend.focus(handle, pane="provider", expand=True)  # Already expanded is idempotent.
        assert value("display-message", "-p", "#{window_zoomed_flag}") == "1"
        backend.focus(handle, pane="bridge", expand=True)
        assert value("display-message", "-p", "#{pane_id}") == handle.bridge_pane
        assert value("display-message", "-p", "#{window_zoomed_flag}") == "1"
        backend.focus(handle, pane="provider")
        assert value("display-message", "-p", "#{pane_id}") == handle.provider_pane
        with pytest.raises(ValueError, match="at least 80"):
            backend.resize(handle, cols=79, rows=24)
    finally:
        backend.stop(handle)


def test_open_existing_rejects_a_foreign_private_tmux_server(tmp_path: Path) -> None:
    private_dir = tmp_path / "foreign"
    private_dir.mkdir(mode=0o700)
    socket = private_dir / "tmux.sock"
    subprocess.run(
        ["tmux", "-S", str(socket), "-f", "/dev/null", "new-session", "-d", "-s", "foreign"],
        check=True,
    )
    backend = TmuxTerminalBackend()
    try:
        assert backend.server_exists(
            TerminalHandle(socket, socket.with_name("tmux.sock.conf"), "foreign", "%0", "%1", 6)
        )
        assert not backend.server_owned(socket)
        with pytest.raises(PermissionError, match="not an LLM Bridge station"):
            backend.open_existing(socket, "foreign")
    finally:
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], check=False)


def test_private_server_requires_private_owned_socket_directory(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    backend = TmuxTerminalBackend()
    with pytest.raises(PermissionError, match="owned by this user and private"):
        backend.start(
            provider_argv=[sys.executable, "-c", "pass"],
            bridge_argv=["/bin/cat"],
            cwd=tmp_path,
            socket_path=shared / "tmux.sock",
            session_name="not-private",
        )
    assert shared.stat().st_mode & 0o777 == 0o755


def test_prefix_key_rejects_tmux_configuration_syntax(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    socket = tmp_path / "injection.sock"
    with pytest.raises(ValueError, match="prefix must be one tmux key"):
        backend.start(
            provider_argv=[sys.executable, "-c", "pass"],
            bridge_argv=["/bin/cat"],
            cwd=tmp_path,
            socket_path=socket,
            session_name="prefix-test",
            prefix="C-]; kill-server",
        )
    assert not socket.exists()
    assert not socket.with_name(socket.name + ".conf").exists()


def test_dead_provider_pane_is_not_reported_alive(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=[sys.executable, "-c", "print('done', flush=True)"],
        bridge_argv=["/bin/sh", "-c", "exec cat"],
        cwd=tmp_path,
        socket_path=tmp_path / "finished.sock",
        session_name="finished",
    )
    try:
        assert _wait_for(lambda: not backend.pane_is_alive(handle, pane="provider"))
        assert not backend.is_alive(handle)
        assert backend.pane_is_alive(handle, pane="bridge")
    finally:
        backend.stop(handle)
    assert not backend.is_alive(handle)


def test_tmux_commands_have_bounded_timeouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import llm_tools.terminal as terminal

    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(dict(kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    TmuxTerminalBackend()._run(private_dir / "tmux.sock", "new-session")
    assert calls[0]["timeout"] == TmuxTerminalBackend._COMMAND_TIMEOUT_SECONDS

