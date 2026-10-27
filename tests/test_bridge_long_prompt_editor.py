"""An unchanged real editor roundtrip must preserve full task instructions."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import sys
import termios
import time

import pytest

from llm_tools.bridge_store import BridgeStore
from llm_tools.terminal import TmuxTerminalBackend


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required for rendered terminal assertions")
@pytest.mark.parametrize("no_color", [False, True], ids=["color", "monochrome"])
def test_real_curses_amendment_preserves_long_prompt_and_whitespace(tmp_path: Path, env: dict[str, str], no_color: bool) -> None:
    state = tmp_path / "state"
    database = state / "bridge.sqlite3"
    original = "Initial instruction\n\tIndented instruction\r\n" + "x" * 20000 + "\nTAIL_INSTRUCTION\n"
    with BridgeStore(database) as store:
        mission = store.create_mission("Editor preservation", original, cwd=tmp_path, initial_prompt=original)
        mission_id, task_id = mission["id"], mission["initial_task_id"]
    root = str(Path(__file__).resolve().parents[1])
    python_path = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH", ""))))
    command = ["env", "-u", "NO_COLOR", "-u", "LLM_USAGE_NO_COLOR", f"LLM_BRIDGE_STATE_DIR={state}", "TERM=xterm-256color",
               "VISUAL=/bin/true", f"PYTHONPATH={python_path}"]
    # Forward only known hermetic fixture controls, never serialize ambient
    # credentials into tmux's private command/config metadata.
    for name in ("HOME", "PATH", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR",
                 "COVERAGE_PROCESS_START", "COVERAGE_FILE", "LLM_TOOLS_NO_INHIBIT",
                 "LLM_USAGE_NO_SERVICE", "LLM_USAGE_DISABLE_CODEX_APP_SERVER",
                 "LLM_USAGE_DISABLE_COPILOT_ADDON", "LLM_USAGE_DISABLE_COPILOT_MONTHLY",
                 "LLM_USAGE_LIVE_FETCH_RETRIES"):
        if name in env:
            command.append(f"{name}={env[name]}")
    if no_color:
        command.append("NO_COLOR=1")
    command.extend([sys.executable, "-m", "llm_tools.bridge", "control", "--mission", mission_id])
    backend = TmuxTerminalBackend()
    # Detached tmux has no outer status row: 31 - 6 control rows - 1 divider
    # leaves the same real80x24 provider PTY as the original direct-PTY test.
    handle = backend.start(provider_argv=command,
                           bridge_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                           cwd=tmp_path, socket_path=tmp_path / "editor.sock", session_name="editor", size=(80, 31))

    def wait_for(predicate) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.03)
        assert predicate(), "real rendered terminal did not reach the expected state: " + backend.capture(handle, pane="provider")

    try:
        geometry = subprocess.check_output(["tmux", "-S", str(handle.socket_path), "display-message", "-p",
                                            "-t", handle.provider_pane, "#{pane_width}x#{pane_height}"], text=True).strip()
        assert geometry == "80x24"
        wait_for(lambda: "Editor preservation" in backend.capture(handle, pane="provider"))
        backend.send_keys(handle, pane="provider", keys="\x1bOQ")

        def orders_visible() -> bool:
            lines = backend.capture(handle, pane="provider").splitlines()
            return (len(lines) > 1 and lines[0].rstrip().endswith(" Orders")
                    and lines[1].startswith("Orders: 1 total"))

        # Curses can retain the first 'O' from Objective and repaint only
        # 'rders:'. Assert rendered cells rather than matching an ANSI delta.
        wait_for(orders_visible)
        backend.send_keys(handle, pane="provider", keys="\x12")
        amended = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with BridgeStore(database) as store:
                task = store.get_task(task_id)
            if task["revision"] > 1:
                amended = task["pending"].get("prompt")
                break
            time.sleep(.03)
        assert amended is not None, "the real editor command did not persist an amendment"
        assert amended == original, "an unchanged edit lost task text or significant whitespace"
        # A commit can precede publication of the worker result. The UI
        # deliberately rejects exit while a durable command is still pending.
        wait_for(lambda: "Applied:" in backend.capture(handle, pane="provider"))
        backend.send_keys(handle, pane="provider", keys="C-d")
        wait_for(lambda: subprocess.check_output(
            ["tmux", "-S", str(handle.socket_path), "display-message", "-p", "-t", handle.provider_pane,
             "#{pane_dead}"], text=True).strip() == "1")
        assert subprocess.check_output(
            ["tmux", "-S", str(handle.socket_path), "display-message", "-p", "-t", handle.provider_pane,
             "#{pane_dead_status}"], text=True).strip() == "0"
    finally:
        backend.stop(handle)


def test_real_curses_stdin_file_request_keeps_control_input_available(tmp_path: Path, env: dict[str, str]) -> None:
    """A REPL command cannot consume the curses process's terminal stdin."""
    state = tmp_path / "state"
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("Stdin control", "Keep keyboard control", cwd=tmp_path, initial_prompt="Original")
        mission_id = mission["id"]
    env.update(LLM_BRIDGE_STATE_DIR=str(state), TERM="xterm-256color", VISUAL="/bin/true")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "llm_tools.bridge", "control", "--mission", mission["id"]],
        stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
    )
    os.close(slave)
    try:
        output = bytearray()

        def read_until(marker: bytes, timeout: float) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and process.poll() is None:
                readable, _, _ = select.select([master], [], [], 0.03)
                if readable:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
                if marker in output:
                    return
            assert marker in output, f"real control screen did not render {marker!r}"

        read_until(b"Stdin control", timeout=4)
        assert b"Stdin control" in output
        os.write(master, b"task add --file -\r")
        rejection = b"error: press Ctrl-E with these options"
        read_until(rejection, timeout=4)
        assert rejection in output
        with BridgeStore(state / "bridge.sqlite3") as store:
            assert len(store.list_tasks(mission_id)) == 1, "rejected stdin request must not create a task"

        # PTY EOF/EIO can arrive before Python has completed its final exit
        # work. Wait for the child itself after sending one control key instead
        # of treating a closed PTY as proof that the process has exited.
        os.write(master, b"\x04")
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("the Bridge control UI stopped accepting Ctrl-D after the stdin request") from exc
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        os.close(master)
