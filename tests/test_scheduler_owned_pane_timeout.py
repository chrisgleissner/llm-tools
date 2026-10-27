"""Timeout policy for the Bridge-owned native pane wait loop."""

from __future__ import annotations

from pathlib import Path

from llm_tools import common, scheduler


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def _run_fake_owned_pane(tmp_path: Path, monkeypatch, *, status_after_ticks: int) -> tuple[int, Path]:
    monkeypatch.setattr(common, "have_cmd", lambda _name: True)
    calls = 0

    def fake_tmux(argv, **_kwargs):
        nonlocal calls
        command = argv[3] if len(argv) > 2 and argv[1] == "-S" else argv[1]
        if command == "has-session":
            return _Completed()
        if command == "display-message":
            calls += 1
            if calls == 1:  # the initial pane-dead check permits respawn
                return _Completed(stdout="1")
            ticks = calls - 1
            if ticks > status_after_ticks:
                status.write_text("0", encoding="utf-8")
            return _Completed(stdout="0")
        return _Completed()

    monkeypatch.setattr(scheduler.subprocess, "run", fake_tmux)
    monkeypatch.setattr(scheduler.time, "sleep", lambda _seconds: None)
    logs = common.setup_run_logs(tmp_path / "logs", "codex")
    output = logs.run_dir / "attempt.out"
    status = logs.run_dir / "attempt.status"
    cfg = scheduler.SchedulerConfig(
        provider="codex",
        cwd=str(tmp_path),
        exec_mode="tmux",
        tmux_target="bridge:station",
        tmux_socket=str(tmp_path / "bridge.sock"),
        tmux_pane="%1",
        native_tui=True,
    )
    return scheduler.run_tmux(cfg, logs, ["true"], output, status), status


def test_owned_native_pane_has_no_implicit_one_hour_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LLM_SCHEDULER_TMUX_TIMEOUT", raising=False)
    status_code, status_file = _run_fake_owned_pane(tmp_path, monkeypatch, status_after_ticks=3601)

    assert status_code == 0
    assert status_file.read_text(encoding="utf-8") == "0"


def test_explicit_owned_pane_timeout_remains_honored(tmp_path: Path, monkeypatch) -> None:
    # The explicit scheduler timeout remains a user-controlled safety bound.
    monkeypatch.setenv("LLM_SCHEDULER_TMUX_TIMEOUT", "1")
    status_code, status_file = _run_fake_owned_pane(tmp_path, monkeypatch, status_after_ticks=3601)

    assert status_code == 124
    assert status_file.read_text(encoding="utf-8") == "124"
