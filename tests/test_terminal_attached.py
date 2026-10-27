"""Attached tmux client transport tests through a real outer PTY."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import uuid

import pytest

from llm_tools.terminal import TmuxTerminalBackend

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


def test_attached_client_input_paste_mouse_resize_detach_and_reattach(tmp_path: Path) -> None:
    fake = tmp_path / "native.py"
    fake.write_text('''import fcntl, os, signal, struct, sys, termios, tty
from pathlib import Path
out = Path(sys.argv[1])
tty.setraw(0)
def size(*args):
    rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\\0"*8))
    out.with_suffix(".size").write_text(f"{cols}x{rows}")
signal.signal(signal.SIGWINCH, size)
size()
os.write(1, b"\\x1b[?1049h\\x1b[?1000h\\x1b[?1004h\\x1b[?1006h\\x1b[?2004h\\x1b[32mNATIVE READY\\x1b[0m")
with out.open("ab", buffering=0) as log:
    while True:
        data = os.read(0, 4096)
        if not data: break
        log.write(data)
        if b"reattached" in data:
            os.write(1, b"\\nACK reattached\\n")
''', encoding="utf-8")
    backend = TmuxTerminalBackend()
    provider, controls = tmp_path / "provider.bin", tmp_path / "controls.bin"
    handle = backend.start(
        provider_argv=[sys.executable, str(fake), str(provider)],
        bridge_argv=[sys.executable, str(fake), str(controls)],
        cwd=tmp_path,
        socket_path=tmp_path / "terminal.sock",
        session_name="attached",
    )
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    before = termios.tcgetattr(slave)
    env = {key: value for key, value in os.environ.items() if key != "TMUX"}
    env["TERM"] = "xterm-256color"

    def attach_code() -> str:
        return (
            "from pathlib import Path; from llm_tools.terminal import "
            "TerminalHandle,TmuxTerminalBackend; h=TerminalHandle("
            + ",".join(
                [
                    f"Path({str(handle.socket_path)!r})",
                    f"Path({str(handle.config_path)!r})",
                    repr(handle.session_name),
                    repr(handle.provider_pane),
                    repr(handle.bridge_pane),
                    str(handle.bridge_rows),
                ]
            )
            + "); raise SystemExit(TmuxTerminalBackend().attach(h))"
        )

    def controlling_terminal() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    def attach() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, "-c", attach_code()],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            preexec_fn=controlling_terminal,
        )

    client_name: str | None = None

    def active_pane() -> str:
        return subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "display-message", "-p", "-t", client_name or "", "#{pane_id}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def attached_geometry() -> tuple[str, dict[str, str]]:
        client_size = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "list-clients", "-F", "#{client_width}x#{client_height}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        pane_output = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "list-panes", "-F", "#{pane_id} #{pane_width}x#{pane_height}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        panes = {line.split(maxsplit=1)[0]: line.split(maxsplit=1)[1] for line in pane_output.splitlines()}
        return client_size, panes

    def prefix_pending() -> bool:
        result = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "list-clients", "-F", "#{client_prefix}"],
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip() == "1"

    def detach_client() -> None:
        prefix_key("d")
        wait_for(lambda: process.poll() is not None)

    process = attach()
    screen = bytearray()

    def wait_for(predicate, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            if select.select([master], [], [], 0.05)[0]:
                screen.extend(os.read(master, 65536))
        assert predicate(), f"timed out; client exit={process.poll()}"

    def received(path: Path, expected: bytes) -> bool:
        return path.exists() and expected in path.read_bytes()

    def pane_top(pane: str) -> int:
        return int(subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "display-message", "-p", "-t", pane, "#{pane_top}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip())

    def zoomed() -> bool:
        return subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "display-message", "-p", "-t", client_name or "", "#{window_zoomed_flag}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip() == "1"

    def prefix_key(key: str) -> None:
        for _attempt in range(5):
            wait_for(lambda: not prefix_pending())
            os.write(master, b"\x1d")
            try:
                wait_for(prefix_pending, timeout=0.5)
            except AssertionError:
                continue
            os.write(master, key.encode("ascii"))
            wait_for(lambda: not prefix_pending())
            break
        else:
            raise AssertionError(f"tmux did not accept the configured prefix before {key!r}")
        expected = handle.bridge_pane if key == "B" else handle.provider_pane if key == "P" else None
        if expected:
            try:
                wait_for(lambda: active_pane() == expected)
            except AssertionError as exc:
                raise AssertionError(f"prefix {key} target mismatch active={active_pane()} zoom={zoomed()}") from exc

    try:
        wait_for(lambda: b"NATIVE READY" in screen)
        writers = backend.writable_clients(handle)
        assert len(writers) == 1
        client_name = writers[0]
        assert b"\x1b[" in screen
        os.write(master, b"keyboard")
        wait_for(lambda: received(provider, b"keyboard"))
        os.write(master, b"\x1b[200~multiline\npaste\x1b[201~")
        wait_for(lambda: received(provider, b"\x1b[200~multiline\npaste\x1b[201~"))

        # Pane selection and native SGR mouse forwarding happen together.
        os.write(master, b"\x1b[<0;3;3M\x1b[<0;3;3m")
        wait_for(lambda: received(provider, b"\x1b[<0;3;3M"))
        bridge_click_y = pane_top(handle.bridge_pane) + 3
        os.write(master, f"\x1b[<0;3;{bridge_click_y}M\x1b[<0;3;{bridge_click_y}m".encode("ascii"))
        wait_for(lambda: received(controls, b"\x1b[<0;3;3M"))
        os.write(master, b"controls")
        wait_for(lambda: received(controls, b"controls"))
        assert not received(provider, b"controls")
        wait_for(lambda: received(provider, b"\x1b[O") and received(controls, b"\x1b[I"))

        # Native mouse wheel reporting survives tmux pane routing.
        os.write(master, f"\x1b[<64;3;{bridge_click_y}M\x1b[<65;3;{bridge_click_y}M".encode("ascii"))
        wait_for(
            lambda: received(controls, b"\x1b[<64;3;3M")
            and received(controls, b"\x1b[<65;3;3M")
        )

        # Ctrl-] B/P are the deliberate escape from a native CLI that owns raw
        # input, alternate screen, and mouse reporting.
        prefix_key("P")
        os.write(master, b"provider-shortcut-path")
        wait_for(lambda: received(provider, b"provider-shortcut-path"))
        prefix_key("B")
        os.write(master, b"bridge-shortcut-path")
        wait_for(lambda: received(controls, b"bridge-shortcut-path"))

        # Prefix shortcuts unzoom the window before focusing their target.
        prefix_key("P")
        prefix_key("z")
        wait_for(zoomed)
        prefix_key("B")
        wait_for(lambda: active_pane() == handle.bridge_pane and not zoomed())
        os.write(master, b"bridge-after-key-unzoom")
        wait_for(lambda: received(controls, b"bridge-after-key-unzoom"))

        # Status labels remain tmux-owned and clickable while the provider is
        # zoomed despite its raw SGR mouse mode.
        prefix_key("P")
        prefix_key("z")
        wait_for(zoomed)
        client_height = int(attached_geometry()[0].split("x", 1)[1])
        os.write(master, f"\x1b[<0;3;{client_height}M\x1b[<0;3;{client_height}m".encode("ascii"))
        wait_for(lambda: active_pane() == handle.bridge_pane and not zoomed())
        os.write(master, b"bridge-after-status-click")
        wait_for(lambda: received(controls, b"bridge-after-status-click"))

        prefix_key("z")
        wait_for(zoomed)
        client_width, client_height_text = attached_geometry()[0].split("x", 1)
        status_y = int(client_height_text)
        status_right_x = int(client_width)
        for status_right_x in range(int(client_width) - 1, int(client_width) - 31, -1):
            click = f"\x1b[<0;{status_right_x};{status_y}M\x1b[<0;{status_right_x};{status_y}m".encode("ascii")
            os.write(master, click)
            deadline = time.monotonic() + 0.15
            while time.monotonic() < deadline and (active_pane() != handle.provider_pane or zoomed()):
                select.select([master], [], [], 0.01)
            if active_pane() == handle.provider_pane and not zoomed():
                break
        assert active_pane() == handle.provider_pane and not zoomed(), f"right click state active={active_pane()} zoom={zoomed()} x={status_right_x}"
        print(f"right status target column {status_right_x}")
        os.write(master, b"provider-after-status-click")
        wait_for(lambda: received(provider, b"provider-after-status-click"))

        # Ctrl-] Ctrl-] sends a literal prefix byte to the provider process.
        wait_for(lambda: not prefix_pending())
        os.write(master, b"\x1b[<0;3;3M\x1b[<0;3;3m")
        wait_for(lambda: active_pane() == handle.provider_pane)
        os.write(master, b"\x02")
        wait_for(lambda: received(provider, b"\x02"))
        wait_for(lambda: not prefix_pending())
        os.write(master, b"\x1d")
        wait_for(prefix_pending)
        os.write(master, b"\x1d")
        wait_for(lambda: received(provider, b"\x1d"))

        # The provider receives the real attached-client resize, minus the
        # six rows assigned to the command pane.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.killpg(process.pid, signal.SIGWINCH)
        wait_for(lambda: provider.with_suffix(".size").read_text() == "80x16")
        wait_for(
            lambda: attached_geometry() == (
                "80x24",
                {handle.provider_pane: "80x16", handle.bridge_pane: "80x6"},
            )
        )
        # tmux can emit a focus transition after updating pane geometry. Prove
        # that the attached input path has settled by retrying unique printable
        # tokens until the active native process logs one intact token.
        wait_for(lambda: active_pane() == handle.provider_pane)
        for attempt in range(8):
            resize_sentinel = f"resize-ready-{uuid.uuid4().hex}".encode("ascii")
            os.write(master, resize_sentinel)
            try:
                wait_for(lambda token=resize_sentinel: received(provider, token), timeout=0.25)
                break
            except AssertionError:
                if attempt == 7:
                    raise
                wait_for(lambda: active_pane() == handle.provider_pane)

        # Detach uses the configured prefix. It leaves both native PTYs alive.
        detach_client()
        assert process.returncode == 0
        assert backend.is_alive(handle)
        assert backend.pane_is_alive(handle, pane="provider")
        assert backend.pane_is_alive(handle, pane="bridge")
        assert termios.tcgetattr(slave) == before

        # A new client resumes the same tmux session and provider process.
        process = attach()
        screen.clear()
        wait_for(lambda: b"NATIVE READY" in screen)
        wait_for(lambda: active_pane() == handle.provider_pane)
        os.write(master, b"reattached")
        wait_for(lambda: received(provider, b"reattached"))
        wait_for(lambda: b"ACK reattached" in screen)
        backend.stop(handle)
        wait_for(lambda: process.poll() is not None)
        assert process.returncode in {0, 1}
        assert not backend.server_exists(handle)
        assert termios.tcgetattr(slave) == before
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        backend.stop(handle)
        os.close(master)
        os.close(slave)
