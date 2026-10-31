"""The verifier's shared SIGTERM-then-SIGKILL process-group stop."""

from __future__ import annotations

import signal
import subprocess
import sys
import time

from llm_tools.bridge_runtime import _kill_process_group, _run_bounded_verifier

IGNORE_TERM = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
HONOUR_TERM = "import time; print('ready', flush=True); time.sleep(60)"


def _start(code: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, start_new_session=True)
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == b"ready"
    return proc


def test_a_process_that_honours_sigterm_is_stopped_by_sigterm() -> None:
    proc = _start(HONOUR_TERM)
    started = time.monotonic()
    _kill_process_group(proc, grace=5)
    assert proc.returncode == -signal.SIGTERM
    assert time.monotonic() - started < 4


def test_a_process_that_ignores_sigterm_is_killed_after_the_grace_period() -> None:
    proc = _start(IGNORE_TERM)
    started = time.monotonic()
    _kill_process_group(proc, grace=0.2)
    assert proc.returncode == -signal.SIGKILL
    assert time.monotonic() - started < 5


def test_an_exited_process_is_reaped_without_error() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait()
    _kill_process_group(proc)
    assert proc.returncode == 0


def test_verifier_timeout_stops_a_child_that_ignores_sigterm(tmp_path) -> None:
    started = time.monotonic()
    # The child inherits SIGTERM as ignored from the start, so a timeout that fires
    # before its interpreter has run IGNORE_TERM (likely on a loaded host) still
    # meets a child that ignores SIGTERM.
    previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        result = _run_bounded_verifier(
            [sys.executable, "-c", IGNORE_TERM], str(tmp_path), 0.5, tmp_path / "verifier.log",
        )
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert result["timed_out"] is True
    assert result["exit_code"] == -signal.SIGKILL
    assert time.monotonic() - started < 10
