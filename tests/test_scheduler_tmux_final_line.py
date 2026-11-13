"""A native provider's final line survives an immediate exit in a Bridge-owned tmux pane (DOG-025)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from llm_tools import common, scheduler

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")

REPETITIONS = 20
EXIT_STATUS = 3
SERVER_STOPPED_SECONDS = 0.25


def _tmux(socket: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", "-S", str(socket), "-f", "/dev/null", *args], text=True, capture_output=True, check=False)


def _slow_tmux_reads(monkeypatch, socket: Path) -> None:
    """Make the tmux server read the provider's output late, as a loaded host does, and run without pipe-pane.

    tmux checks for unread pty output before destroying a pane only when pipe-pane is active. The scheduler
    attaches pipe-pane after the respawn, so the pane script itself has to keep the pane alive. The server
    is stopped when the scheduler would attach pipe-pane (the provider starts 0.1 s after the respawn) and
    resumed 0.25 s later, so the provider has exited before the server first looks at the pty.
    """
    real_run = subprocess.run

    def run(argv, *args, **kwargs):
        if "pipe-pane" in argv:
            pid = int(_tmux(socket, "display-message", "-p", "#{pid}").stdout)
            os.kill(pid, signal.SIGSTOP)
            threading.Timer(SERVER_STOPPED_SECONDS, os.kill, (pid, signal.SIGCONT)).start()
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(scheduler.subprocess, "run", run)


def test_a_final_line_printed_just_before_an_immediate_exit_stays_in_the_pane(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_TOOLS_NO_INHIBIT", "1")
    socket = tmp_path / "t.sock"
    created = _tmux(socket, "new-session", "-d", "-s", "s", "-x", "100", "-y", "30", "-P", "-F", "#{pane_id}", "/bin/sh -c 'exit 0'", ";", "set", "-g", "remain-on-exit", "on")
    assert created.returncode == 0, created.stderr
    _slow_tmux_reads(monkeypatch, socket)
    pane = created.stdout.split()[0]
    lost: list[int] = []
    try:
        for index in range(REPETITIONS):
            token = f"FINAL-LINE-{index}-unique"
            # A burst of output followed by the final line, then an immediate exit with a status.
            code = f"print('\\n'.join(str(n) for n in range(3000))); print({token!r}); raise SystemExit({EXIT_STATUS})"
            logs = common.setup_run_logs(tmp_path / "logs", "codex")
            cfg = scheduler.SchedulerConfig(
                provider="codex", cwd=str(tmp_path), exec_mode="tmux", tmux_target="s:w",
                tmux_socket=str(socket), tmux_pane=pane, native_tui=True,
            )
            status_file = logs.run_dir / "attempt.status"
            status = scheduler.run_tmux(cfg, logs, [sys.executable, "-c", code], logs.run_dir / "attempt.out", status_file)
            assert status == EXIT_STATUS
            text = _tmux(socket, "capture-pane", "-p", "-J", "-S", "-", "-t", pane).stdout
            if token not in text:
                lost.append(index)
    finally:
        _tmux(socket, "kill-server")
    assert not lost, f"final line lost in {len(lost)} of {REPETITIONS} runs: {lost}"
