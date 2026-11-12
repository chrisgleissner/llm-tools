"""The Bridge command pane never stays in tmux copy mode; provider panes keep it (FIND-011)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from llm_tools.terminal import TerminalHandle, TmuxTerminalBackend
from tests.conftest import wait_for

pytestmark = [pytest.mark.tmux, pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")]

# Neither pane enables mouse reporting, as before the console starts curses.
IDLE_PANE = [sys.executable, "-c", "import sys; sys.stdin.read()"]


def _in_mode(handle: TerminalHandle, pane: str) -> bool:
    out = subprocess.run(
        ["tmux", "-S", str(handle.socket_path), "display-message", "-p", "-t", pane, "#{pane_in_mode}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return out == "1"


def _tmux(handle: TerminalHandle, *args: str) -> None:
    subprocess.run(["tmux", "-S", str(handle.socket_path), *args], capture_output=True, text=True, check=True)


def test_command_pane_leaves_copy_mode_and_provider_pane_keeps_it(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=IDLE_PANE, bridge_argv=IDLE_PANE, cwd=tmp_path,
        socket_path=tmp_path / "t.sock", session_name="copy-mode",
    )
    try:
        _tmux(handle, "copy-mode", "-t", handle.provider_pane)
        assert _in_mode(handle, handle.provider_pane), "provider panes keep copy mode for scrollback"
        _tmux(handle, "copy-mode", "-t", handle.bridge_pane)
        wait_for(lambda: not _in_mode(handle, handle.bridge_pane), 5, 0.05, "command pane left copy mode")
        assert _in_mode(handle, handle.provider_pane)
        # A second entry is cancelled the same way.
        _tmux(handle, "copy-mode", "-t", handle.bridge_pane)
        wait_for(lambda: not _in_mode(handle, handle.bridge_pane), 5, 0.05, "command pane left copy mode again")
    finally:
        backend.stop(handle)
