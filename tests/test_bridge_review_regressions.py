"""Behavioral regressions found during independent Bridge review."""

from __future__ import annotations

import os
import fcntl
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import json

from llm_tools.bridge import _apply_operator_command
from llm_tools.bridge_runtime import _process_start_token, _terminate_owned_process_group, run_controller
from llm_tools.bridge_store import BridgeStore


def test_cancel_failed_task_has_atomic_successful_acknowledgement(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id = store.create_mission("cancel", "Cancel failed work", cwd=tmp_path)["id"]
        task_id = store.add_task(mission_id, "Failed task")
        store.set_mission_status(mission_id, "RUNNING")
        operation_id = store.begin_operation(task_id, iteration=1, station="fixture")
        store.finish_operation(operation_id, exit_code=1)

        result = _apply_operator_command(store, mission_id, {"action": "cancel"})

        assert result["status"] == "APPLIED", result
        assert store.get_mission(mission_id)["status"] == "CANCELLED"
        assert store.get_task(task_id)["status"] == "CANCELLED"


def test_rejected_command_rolls_back_prior_effects(tmp_path: Path, monkeypatch) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id = store.create_mission("atomic", "Reject atomically", cwd=tmp_path)["id"]
        task_id = store.add_task(mission_id, "Queued task")
        store.set_mission_status(mission_id, "RUNNING")

        def reject_task_transition(*args, **kwargs):
            raise ValueError("fixture rejects after the mission transition")

        monkeypatch.setattr(store, "set_task_status", reject_task_transition)
        result = _apply_operator_command(store, mission_id, {"action": "cancel"})

        assert result["status"] == "REJECTED"
        assert store.get_mission(mission_id)["status"] == "RUNNING"
        assert store.get_task(task_id)["status"] == "QUEUED"
        assert not any(
            event["kind"] == "mission-status" and event["data"].get("to") == "CANCELLED"
            for event in store.events(mission_id)
        )


def test_owned_group_termination_kills_descendant_after_leader_exits(tmp_path: Path) -> None:
    child_path = tmp_path / "owned-child.pid"
    child_script = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(child_path)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent_script = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child_script!r}]); time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", parent_script], start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while not child_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_path.exists(), "owned child did not start"
        child_pid = int(child_path.read_text())
        assert os.getpgid(child_pid) == process.pid
        token = _process_start_token(process.pid)
        assert token

        _terminate_owned_process_group(process.pid, token, timeout=0.2)
        process.wait(timeout=3)

        assert not _process_start_token(child_pid), "TERM-ignoring owned descendant survived complete stop"
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def test_controller_start_overrides_survive_complete_shutdown(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "missing-config.toml"))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission_id = store.create_mission(
            "config", "Persist effective station configuration", cwd=tmp_path,
            config={"providers": "codex", "routes": "", "headless": False, "native_tui": False},
        )["id"]

    def finish_without_launch(argv, *, bridge_hooks):
        assert argv[argv.index("--providers") + 1] == "claude"
        bridge_hooks.store.set_mission_status(mission_id, "STOPPING")
        return 0

    monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", finish_without_launch)
    assert run_controller(mission_id, headless=True, providers="claude") == 0

    with BridgeStore(state / "bridge.sqlite3") as reopened:
        mission = reopened.get_mission(mission_id)
        assert mission["status"] == "STOPPED"
        assert mission["config"]["providers"] == "claude"
        assert mission["config"]["headless"] is True


def test_legacy_attached_scheduler_forwards_outer_terminal_resize(tmp_path: Path) -> None:
    observed = tmp_path / "dimensions.json"
    provider = tmp_path / "interactive_provider.py"
    provider.write_text(
        "import os,signal,json,sys\nfrom pathlib import Path\n"
        f"observed=Path({str(observed)!r})\n"
        "def record(*_):\n"
        " size=os.get_terminal_size(0)\n"
        " temporary=observed.with_suffix('.tmp')\n"
        " temporary.write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),"
        "'size':[size.columns,size.lines]}))\n"
        " temporary.replace(observed)\n"
        "signal.signal(signal.SIGWINCH,record)\nrecord()\n"
        "for line in sys.stdin:\n"
        " if line.strip()=='q': break\n",
        encoding="utf-8",
    )
    helper = tmp_path / "attached_scheduler.py"
    helper.write_text(
        "from pathlib import Path\nimport sys\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parents[1])!r})\n"
        "from llm_tools.scheduler import SchedulerConfig,run_fresh_attached\n"
        f"cfg=SchedulerConfig(cwd={str(tmp_path)!r})\n"
        f"raise SystemExit(run_fresh_attached(cfg,[sys.executable,{str(provider)!r}],"
        f"Path({str(tmp_path / 'output')!r}),Path({str(tmp_path / 'status')!r})))\n",
        encoding="utf-8",
    )
    pid, terminal_fd = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, str(helper)])

    def drain() -> None:
        readable, _, _ = select.select([terminal_fd], [], [], 0.02)
        if readable:
            try:
                os.read(terminal_fd, 65536)
            except OSError:
                pass

    child_pid = 0
    child_token = ""
    try:
        fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        deadline = time.monotonic() + 3
        while not observed.exists() and time.monotonic() < deadline:
            drain()
        assert observed.exists(), "interactive provider did not start"
        initial = json.loads(observed.read_text())
        child_pid = int(initial["pid"])
        child_token = _process_start_token(child_pid)
        assert initial["size"] == [80, 24]

        fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.killpg(pid, signal.SIGWINCH)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            drain()
            if json.loads(observed.read_text())["size"] == [120, 40]:
                break
        assert json.loads(observed.read_text())["size"] == [120, 40]
    finally:
        try:
            os.write(terminal_fd, b"q\n")
        except OSError:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            ended, _ = os.waitpid(pid, os.WNOHANG)
            if ended:
                break
            drain()
        else:
            os.killpg(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        if child_pid and child_token and _process_start_token(child_pid) == child_token:
            os.killpg(os.getpgid(child_pid), signal.SIGKILL)
        os.close(terminal_fd)
