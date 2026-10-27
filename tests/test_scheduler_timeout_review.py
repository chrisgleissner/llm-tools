"""The owned-pane default must not change explicit legacy timeout values."""

from pathlib import Path
import subprocess

import pytest

from llm_tools import common, scheduler


@pytest.mark.parametrize("timeout", ["0", "-1"])
def test_explicit_nonpositive_legacy_tmux_timeout_does_not_become_unbounded(tmp_path: Path, monkeypatch, timeout: str) -> None:
    monkeypatch.setenv("LLM_SCHEDULER_TMUX_TIMEOUT", timeout)
    monkeypatch.setattr(common, "have_cmd", lambda _name: True)
    logs = common.setup_run_logs(tmp_path / "logs", "codex")
    output = logs.run_dir / "attempt.out"
    status = logs.run_dir / "attempt.status"
    def tmux(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, "", "")
    def sleep(seconds):
        # A completion after one poll keeps this regression bounded even if
        # the explicit legacy timeout has accidentally become unbounded.
        if seconds == 1:
            status.write_text("0", encoding="utf-8")
    monkeypatch.setattr(scheduler.subprocess, "run", tmux)
    monkeypatch.setattr(scheduler.time, "sleep", sleep)
    config = scheduler.SchedulerConfig(provider="codex", cwd=str(tmp_path), exec_mode="tmux", tmux_target="legacy:station")

    result = scheduler.run_tmux(config, logs, ["true"], output, status)

    assert result == 124
