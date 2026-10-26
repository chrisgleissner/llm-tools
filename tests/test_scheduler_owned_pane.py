"""Scheduler submission contract for Bridge-owned native PTY panes."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from llm_tools import common, scheduler
from llm_tools.terminal import TmuxTerminalBackend


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("codex", ["codex", "-C", str(Path.cwd()), "objective"]),
        ("claude", ["claude", "objective"]),
        ("copilot", ["copilot", "-C", str(Path.cwd()), "-i", "objective"]),
        ("kilo", ["kilo", "--prompt", "objective"]),
        ("opencode", ["opencode", "--prompt", "objective"]),
    ],
)
def test_attached_provider_argv_uses_native_entrypoint(provider: str, expected: list[str]) -> None:
    assert scheduler.provider_default_argv(scheduler.SchedulerConfig(provider=provider, attached=True), "objective") == expected


def test_minimax_is_not_advertised_as_native_engineering_session(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        scheduler.provider_default_argv(scheduler.SchedulerConfig(provider="minimax", attached=True), "objective")
    assert exc.value.code == 2
    assert "not a native engineering session" in capsys.readouterr().err


def test_scheduler_submits_through_private_owned_pane_and_keeps_server_alive(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=["/bin/sh", "-c", "exit 0"],
        bridge_argv=["/bin/sh", "-c", "exec cat"],
        cwd=tmp_path,
        socket_path=tmp_path / "bridge.sock",
        session_name="bridge-owned-pane",
        size=(100, 30),
    )
    try:
        logs = common.setup_run_logs(tmp_path / "logs", "codex")
        output = logs.run_dir / "attempt-1.out"
        status = logs.run_dir / "attempt-1.status"
        cfg = scheduler.SchedulerConfig(
            provider="codex",
            cwd=str(tmp_path),
            exec_mode="tmux",
            tmux_target=f"{handle.session_name}:station",
            tmux_socket=str(handle.socket_path),
            tmux_pane=handle.provider_pane,
            native_tui=True,
            env_overrides={
                "LLM_BRIDGE_MANAGED": "1",
                "LLM_BRIDGE_OPERATION_ID": "owned-operation",
            },
        )
        rc = scheduler.run_tmux(
            cfg,
            logs,
            [sys.executable, "-c", "import os; print('bridge-owned-provider-output'); print('managed=' + os.environ.get('LLM_BRIDGE_MANAGED','')); print('operation=' + os.environ.get('LLM_BRIDGE_OPERATION_ID',''))"],
            output,
            status,
        )
        assert rc == 0
        assert status.read_text(encoding="utf-8").strip() == "0"
        assert "bridge-owned-provider-output" in output.read_text(encoding="utf-8")
        assert "managed=1" in output.read_text(encoding="utf-8")
        assert "operation=owned-operation" in output.read_text(encoding="utf-8")
        listed = subprocess.run(["tmux", "-S", str(handle.socket_path), "has-session", "-t", handle.session_name], check=False)
        assert listed.returncode == 0
        assert not backend.pane_is_alive(handle, pane="provider")
        assert "native_tui" in scheduler.safe_args_json(cfg)
    finally:
        backend.stop(handle)


def test_scheduler_refuses_to_send_shell_command_into_a_live_native_pane(tmp_path: Path, monkeypatch) -> None:
    backend = TmuxTerminalBackend()
    script = tmp_path / "still_live.py"
    script.write_text(
        "import sys, time\n"
        "print('READY_FOR_NATIVE_INPUT', flush=True)\n"
        "line = sys.stdin.readline()\n"
        "print('NATIVE_RECEIVED=' + line.strip(), flush=True)\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    handle = backend.start(
        provider_argv=[sys.executable, str(script)],
        bridge_argv=["/bin/sh", "-c", "exec cat"],
        cwd=tmp_path,
        socket_path=tmp_path / "busy.sock",
        session_name="busy-native-pane",
    )
    monkeypatch.setenv("LLM_SCHEDULER_TMUX_TIMEOUT", "2")
    try:
        import time

        time.sleep(0.2)
        assert "READY_FOR_NATIVE_INPUT" in backend.capture(handle, pane="provider")
        logs = common.setup_run_logs(tmp_path / "busy-logs", "codex")
        cfg = scheduler.SchedulerConfig(
            provider="codex",
            cwd=str(tmp_path),
            exec_mode="tmux",
            tmux_target=f"{handle.session_name}:station",
            tmux_socket=str(handle.socket_path),
            tmux_pane=handle.provider_pane,
        )
        rc = scheduler.run_tmux(cfg, logs, ["/bin/echo", "must-not-be-sent"], logs.run_dir / "out", logs.run_dir / "status")
        assert rc == common.AUTONOMY_ABORT_STATUS
        assert "READY_FOR_NATIVE_INPUT" in backend.capture(handle, pane="provider")
        assert "NATIVE_RECEIVED=" not in backend.capture(handle, pane="provider")
    finally:
        backend.stop(handle)

