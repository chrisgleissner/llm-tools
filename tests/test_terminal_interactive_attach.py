"""Interactive station switching keeps source sessions alive and selects clients safely."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import pytest

from llm_tools.terminal import TerminalAction, TmuxTerminalBackend
from llm_tools import terminal
from llm_tools.bridge_tui import _execute_terminal_action
from tests.conftest import scaled

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


def _wait_for(master: int, process: subprocess.Popen[bytes], marker: bytes, timeout: float = 5.0) -> bytes:
    end = time.monotonic() + scaled(timeout)
    output = bytearray()
    while time.monotonic() < end and process.poll() is None:
        ready, _, _ = select.select([master], [], [], 0.05)
        if ready:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                break
            if marker in output:
                return bytes(output)
    assert marker in output, f"missing terminal marker {marker!r}: {bytes(output)[-800:]!r}"
    return bytes(output)


def _drain(master: int) -> None:
    ready, _, _ = select.select([master], [], [], 0.05)
    if ready:
        try:
            os.read(master, 65536)
        except OSError:
            pass


def _pty_attach(backend: TmuxTerminalBackend, handle, *, readonly: bool = False):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

    def child_setup() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    args = ["tmux", "-S", str(handle.socket_path), "-f", str(handle.config_path), "attach-session"]
    if readonly:
        args.append("-r")
    args.extend(["-t", handle.session_name])
    env = {key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}
    env["TERM"] = "xterm-256color"
    child = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, env=env, preexec_fn=child_setup)
    os.close(slave)
    return child, master


def _station(backend: TmuxTerminalBackend, root: Path, name: str):
    provider = [sys.executable, "-u", "-c", f"import time; print('PROVIDER_{name}_READY', flush=True); time.sleep(120)"]
    bridge = [sys.executable, "-u", "-c", f"import time; print('BRIDGE_{name}_READY', flush=True); time.sleep(120)"]
    return backend.start(
        provider_argv=provider,
        bridge_argv=bridge,
        cwd=root,
        socket_path=root / f"{name}.sock",
        session_name=f"station-{name}",
    )


def _capture_switch_error(backend, source, destination, client_name, errors) -> None:
    try:
        backend.switch_client_to(source, destination, client_name=client_name)
    except BaseException as exc:
        errors.append(exc)


def test_real_outer_pty_switches_station_and_one_detach_returns_to_the_launch_point(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    source = _station(backend, tmp_path, "source")
    destination = _station(backend, tmp_path, "destination")
    client = master = None
    try:
        client, master = _pty_attach(backend, source)
        _wait_for(master, client, b"PROVIDER_source_READY")
        writers = backend.writable_clients(source)
        assert len(writers) == 1

        switch_errors: list[BaseException] = []
        switch = threading.Thread(
            target=_capture_switch_error,
            args=(backend, source, destination, writers[0], switch_errors),
            daemon=True,
        )
        switch.start()
        _wait_for(master, client, b"PROVIDER_destination_READY")
        switch.join(timeout=scaled(2))
        assert not switch.is_alive(), "attach command must return to the Bridge UI while the destination is attached"
        assert switch_errors == []
        assert backend.server_exists(source) and backend.server_exists(destination)
        assert backend.session_exists(source) and backend.session_exists(destination)

        # Ctrl-] d detaches from the destination. The terminal returns to where it
        # was launched from (here: the attach process ends) instead of stacking
        # the source station back underneath; both stations keep running.
        os.write(master, b"\x1dd")
        deadline = time.monotonic() + 8.0
        while client.poll() is None and time.monotonic() < deadline:
            _drain(master)
        assert client.poll() is not None, "detaching from the destination must end the attach command"
        switch.join(timeout=scaled(5))
        assert not switch.is_alive()
        assert backend.session_exists(source)
        assert backend.session_exists(destination)
        assert backend.writable_clients(source) == () and backend.writable_clients(destination) == ()
    finally:
        if client is not None and client.poll() is None:
            client.send_signal(signal.SIGTERM)
            try:
                client.wait(timeout=scaled(2))
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait(timeout=scaled(2))
        if master is not None:
            os.close(master)
        for handle in (destination, source):
            try:
                backend.stop(handle)
            except (OSError, RuntimeError, PermissionError, subprocess.SubprocessError):
                pass


def test_readonly_and_control_clients_are_not_writable_choices(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    source = _station(backend, tmp_path, "multi")
    clients: list[tuple[subprocess.Popen[bytes], int]] = []
    try:
        first = _pty_attach(backend, source)
        clients.append(first)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(backend.writable_clients(source)) < 1:
            time.sleep(0.02)
        assert len(backend.writable_clients(source)) == 1

        observer = _pty_attach(backend, source, readonly=True)
        clients.append(observer)
        writer = _pty_attach(backend, source)
        clients.append(writer)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(backend.writable_clients(source)) < 2:
            time.sleep(0.02)
        writable = backend.writable_clients(source)
        assert len(writable) == 2
        assert all(re.fullmatch(r"/dev/[A-Za-z0-9._/-]+", item) for item in writable)
        assert observer[0].poll() is None
        with pytest.raises(RuntimeError, match="multiple writable clients"):
            backend.detach_client(source)
        backend.detach_client(source, client_name=writable[0])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(backend.writable_clients(source)) != 1:
            time.sleep(0.02)
        assert len(backend.writable_clients(source)) == 1
        assert observer[0].poll() is None
        assert sum(process.poll() is None for process, _master in (first, writer)) == 1
    finally:
        for process, master in clients:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=scaled(2))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=scaled(2))
            os.close(master)
        try:
            backend.stop(source)
        except (OSError, RuntimeError, PermissionError, subprocess.SubprocessError):
            pass


def test_writable_client_projection_rejects_observers_control_clients_and_bad_rows(monkeypatch, tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = terminal.TerminalHandle(tmp_path / "s.sock", tmp_path / "s.sock.conf", "s", "%0", "%1", 6)
    output = "\n".join((
        "/dev/pts/4\t0\t0",
        "/dev/pts/5\t1\t0",
        "/dev/pts/6\t0\t1",
        "control-mode\t0\t0",
        "malformed row",
    ))
    monkeypatch.setattr(backend, "_require_owned", lambda _handle: None)
    monkeypatch.setattr(backend, "_run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, output, ""))
    assert backend.writable_clients(handle) == ("/dev/pts/4",)


def test_standalone_terminal_action_attaches_after_optional_same_mission_focus(monkeypatch, tmp_path: Path) -> None:
    source = terminal.TerminalHandle(tmp_path / "source.sock", tmp_path / "source.sock.conf", "source", "%0", "%1", 6)
    destination = terminal.TerminalHandle(tmp_path / "destination.sock", tmp_path / "destination.sock.conf", "dest", "%2", "%3", 6)

    class FakeBackend:
        calls = []
        detached = []
        def focus(self, handle, *, pane, expand=False):
            self.calls.append(("focus", handle, pane, expand))
        def attach(self, handle):
            self.calls.append(("attach", handle))
        def detach_client(self, handle, *, client_name=None):
            self.detached.append((handle, client_name))

    fake = FakeBackend()
    monkeypatch.setattr(terminal, "TmuxTerminalBackend", lambda: fake)
    monkeypatch.setattr("llm_tools.bridge._load_handle", lambda mission: source if mission == "mission-a" else destination)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)

    _execute_terminal_action("mission-a", TerminalAction("mission-a"))
    _execute_terminal_action("mission-a", TerminalAction("mission-b"))
    assert fake.calls == [
        ("focus", source, "provider", True),
        ("attach", source),
        ("attach", destination),
    ]
    _execute_terminal_action("mission-a", TerminalAction("mission-b", "/dev/pts/5", "detach"))
    assert fake.detached == [(destination, "/dev/pts/5")]


def test_terminal_action_is_typed_and_does_not_require_worker_io() -> None:
    assert TerminalAction("mission-b", "%14") == TerminalAction("mission-b", "%14")
    assert TerminalAction("mission-b").client is None


def test_embedded_attach_requires_exact_owned_source_and_explicit_multi_client(monkeypatch, tmp_path: Path) -> None:
    source = terminal.TerminalHandle(tmp_path / "source.sock", tmp_path / "source.sock.conf", "source", "%0", "%1", 6)
    destination = terminal.TerminalHandle(tmp_path / "destination.sock", tmp_path / "destination.sock.conf", "dest", "%2", "%3", 6)

    class FakeBackend:
        clients = ("/dev/pts/3", "/dev/pts/7")
        switched = None
        focused = None
        detached = None

        def writable_clients(self, handle):
            assert handle == source
            return self.clients

        def select_writable_client(self, handle, client_name=None):
            assert handle == source
            if client_name is None:
                if len(self.clients) != 1:
                    raise RuntimeError("multiple writable clients are attached")
                return self.clients[0]
            if client_name not in self.clients:
                raise PermissionError("client is not writable")
            return client_name

        def switch_client_to(self, actual_source, actual_destination, *, client_name):
            client_name = self.select_writable_client(actual_source, client_name)
            self.switched = actual_source, actual_destination, client_name

        def focus(self, handle, *, pane, **_kwargs):
            self.focused = handle, pane

        def server_owned(self, socket):
            return True  # any other tmux server in this test is another Bridge station

        def attach(self, *_args):
            raise AssertionError("embedded terminal should not start a nested attach")

        def detach_client(self, handle, *, client_name=None):
            self.detached = handle, client_name

    fake = FakeBackend()
    monkeypatch.setattr(terminal, "TmuxTerminalBackend", lambda: fake)
    monkeypatch.setattr("llm_tools.bridge._load_handle", lambda mission: source if mission == "mission-a" else destination)
    monkeypatch.setenv("TMUX", f"{source.socket_path},1234,0")
    monkeypatch.setenv("TMUX_PANE", source.bridge_pane)

    with pytest.raises(RuntimeError, match="multiple writable clients"):
        _execute_terminal_action("mission-a", TerminalAction("mission-b"))
    assert fake.switched is None
    _execute_terminal_action("mission-a", TerminalAction("mission-b", "/dev/pts/7"))
    assert fake.switched == (source, destination, "/dev/pts/7")
    with pytest.raises(PermissionError, match="client is not writable"):
        _execute_terminal_action("mission-a", TerminalAction("mission-b", "/dev/pts/99"))
    _execute_terminal_action("mission-a", TerminalAction("mission-a"))
    assert fake.focused == (source, "provider")
    _execute_terminal_action("mission-a", TerminalAction("mission-a", "/dev/pts/3", "detach"))
    assert fake.detached == (source, "/dev/pts/3")
    with pytest.raises(PermissionError, match="limited to its active mission"):
        _execute_terminal_action("mission-a", TerminalAction("mission-b", "/dev/pts/3", "detach"))

    monkeypatch.setenv("TMUX_PANE", source.provider_pane)
    with pytest.raises(PermissionError, match="terminal actions"):
        _execute_terminal_action("mission-a", TerminalAction("mission-b", "/dev/pts/7"))
    monkeypatch.setenv("TMUX_PANE", source.bridge_pane)
    monkeypatch.setenv("TMUX", f"{tmp_path / 'other-station.sock'},1234,0")
    with pytest.raises(PermissionError, match="terminal actions"):
        _execute_terminal_action("mission-a", TerminalAction("mission-b", "/dev/pts/7"))


def test_real_curses_dispatches_terminal_action_after_restoring_terminal(tmp_path: Path) -> None:
    record = tmp_path / "action.txt"
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    script = (
        "from pathlib import Path\n"
        "import curses\n"
        "import llm_tools.bridge_tui as tui\n"
        "from llm_tools.terminal import TerminalAction\n"
        f"record = Path({str(record)!r})\n"
        "def execute(source, action):\n"
        "    record.write_text(f'{source}|{action.mission_id}|{action.client}|{curses.isendwin()}')\n"
        "tui._execute_terminal_action = execute\n"
        "def dispatch(mission, line):\n"
        "    return TerminalAction('mission-b', '/dev/pts/7') if line.startswith('attach') else 'UI_RESUMED_AFTER_ATTACH'\n"
        "def snapshot(mission): return {'mission': {'id': mission, 'name': 'Attach Action Mission', 'status': 'RUNNING'}}\n"
        "raise SystemExit(tui.run_control('mission-a', dispatch, snapshot))\n"
    )
    env = dict(os.environ, TERM="xterm-256color")
    process = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)
    try:
        _wait_for(master, process, b"Attach Action Mission")
        os.write(master, b"attach --mission mission-b --client /dev/pts/7\r")
        deadline = time.monotonic() + scaled(5)
        # write_text() creates the file before it writes the content, so wait for the content.
        while time.monotonic() < deadline and not (record.exists() and record.read_text()):
            select.select([master], [], [], 0.05)
            try:
                os.read(master, 65536)
            except OSError:
                break
        assert record.read_text() == "mission-a|mission-b|/dev/pts/7|True"
        os.write(master, b"status\r")
        _wait_for(master, process, b"UI_RESUMED_AFTER_ATTACH")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=scaled(2))
        os.close(master)

