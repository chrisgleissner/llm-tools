"""Native station status markers follow actual tmux pane focus."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from llm_tools.terminal import TmuxTerminalBackend


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_focus_marker_remains_dynamic_after_prefix_pane_switch(tmp_path: Path):
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=[sys.executable, "-c", "import time; time.sleep(10)"],
        bridge_argv=[sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        socket_path=tmp_path / "tmux.sock",
        session_name="focus-marker-test",
        size=(80, 24),
    )
    def tmux(*args):
        return subprocess.run(["tmux", "-S", str(handle.socket_path), *args],
                              check=True, text=True, capture_output=True).stdout.strip()
    try:
        backend.focus(handle, pane="bridge", expand=True)
        # A prefix P binding selects the provider without calling backend.focus.
        tmux("select-pane", "-t", handle.provider_pane)
        tmux("resize-pane", "-Z", "-t", handle.provider_pane)
        assert tmux("display-message", "-p", "-t", handle.session_name,
                    "#{pane_id}") == handle.provider_pane
        left = tmux("show-option", "-g", "-v", "status-left")
        right = tmux("show-option", "-g", "-v", "status-right")
        assert "pane_index" in left and "pane_index" in right
    finally:
        backend.stop(handle)
