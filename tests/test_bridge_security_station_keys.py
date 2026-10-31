"""A station pane must not be able to reach tmux's command prompt through pasted or typed keys."""
from __future__ import annotations

import fcntl
import os
import shutil
import struct
import subprocess
import termios
import time
from pathlib import Path

import pytest

from llm_tools.terminal import TmuxTerminalBackend

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


def tmux(handle, *args: str) -> str:
    return subprocess.run(["tmux", "-S", str(handle.socket_path), *args], capture_output=True, text=True).stdout


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


@pytest.fixture()
def station(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["/bin/sh", "-c", "exec cat"],
        bridge_argv=["/bin/sh", "-c", "exec sleep 60"],
        picker_argv=["/bin/sh", "-c", "true"],
        cwd=tmp_path,
        socket_path=tmp_path / "s.sock",
        session_name="keys",
        size=(100, 30),
    )
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}
    env["TERM"] = "xterm-256color"
    client = subprocess.Popen(
        ["tmux", "-S", str(handle.socket_path), "-f", str(handle.config_path), "attach-session", "-t", "keys"],
        stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True, close_fds=True,
    )
    os.close(slave)
    assert wait_for(lambda: bool(tmux(handle, "list-clients")))
    time.sleep(0.5)  # let the client finish its first redraw
    try:
        yield handle, master
    finally:
        client.kill()
        client.wait()
        os.close(master)
        backend.stop(handle)


def test_pasted_prefix_burst_cannot_reach_the_tmux_command_prompt(station) -> None:
    handle, master = station
    os.write(master, b"\x1d")
    time.sleep(0.3)
    os.write(master, b":")
    time.sleep(0.3)
    os.write(master, b"set -g @pwn yes\r")
    time.sleep(0.6)
    assert tmux(handle, "show-options", "-gv", "@pwn").strip() == ""
    keys = tmux(handle, "list-keys", "-T", "prefix")
    assert "command-prompt" not in keys and "run-shell" not in keys
    assert "detach-client" in keys and "send-prefix" in keys


def test_single_unbracketed_burst_cannot_reach_the_tmux_command_prompt(station) -> None:
    handle, master = station
    os.write(master, b"\x1d:" + b" " * 40 + b"set -g @pwn3 yes\r")
    time.sleep(0.6)
    assert tmux(handle, "show-options", "-gv", "@pwn3").strip() == ""


def test_copy_mode_prompts_are_unbound(station) -> None:
    handle, master = station
    os.write(master, b"\x1d[")
    time.sleep(0.3)
    os.write(master, b"g")
    time.sleep(0.3)
    os.write(master, b'1" ; set -g @pwn2 yes ; "\r')
    time.sleep(0.6)
    assert tmux(handle, "show-options", "-gv", "@pwn2").strip() == ""
    for table in ("copy-mode", "copy-mode-vi"):
        assert "command-prompt" not in tmux(handle, "list-keys", "-T", table)


def test_application_cannot_set_the_clipboard_and_config_says_external(station) -> None:
    handle, _ = station
    assert tmux(handle, "show-options", "-sv", "set-clipboard").strip() == "external"
    assert "set-clipboard external" in Path(handle.config_path).read_text()


def test_mouse_menus_are_unbound(station) -> None:
    handle, _ = station
    assert "display-menu" not in tmux(handle, "list-keys", "-T", "root")
