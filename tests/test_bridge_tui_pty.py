"""Exercise the curses control loop through a real pseudo-terminal."""

from __future__ import annotations

import fcntl
import ast
import os
import pty
import select
import shlex
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path


def _read_until(master: int, process: subprocess.Popen[bytes], marker: bytes, timeout: float = 5.0) -> bytes:
    deadline = time.monotonic() + timeout
    output = bytearray()
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        ready, _, _ = select.select([master], [], [], 0.05)
        if ready:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                break
            if marker in output:
                return bytes(output)
    assert marker in output, f"terminal did not render {marker!r}; output={bytes(output)[-2000:]!r}"
    return bytes(output)


def _wait_for_record(master: int, process: subprocess.Popen[bytes], record: Path, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if record.exists():
            content = record.read_text(encoding="utf-8")
            if content:
                return content
        if process.poll() is not None:
            break
        ready, _, _ = select.select([master], [], [], 0.05)
        if ready:
            try:
                os.read(master, 65536)
            except OSError:
                break
    return record.read_text(encoding="utf-8") if record.exists() else ""


def _start_console(
    tmp_path: Path,
    *,
    editor: Path | None = None,
    full: bool = True,
) -> tuple[subprocess.Popen[bytes], int, Path]:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24 if full else 6, 80 if full else 100, 0, 0))
    record = tmp_path / "dispatch.txt"
    script = (
        "import os\n"
        "import shlex\n"
        "from llm_tools.bridge_tui import run_console, run_control\n"
        "record = os.environ['BRIDGE_TUI_RECORD']\n"
        "def dispatch(mission, command):\n"
        "    argv = shlex.split(command)\n"
        "    payload = open(argv[-1], encoding='utf-8').read() if argv and (argv[0] == 'add' or argv[:2] == ['task', 'add']) else ''\n"
        "    with open(record, 'a', encoding='utf-8') as stream: stream.write(repr((mission, command, payload)) + '\\n')\n"
        "    if argv and argv[0] == 'priority':\n"
        "        return '{\"id\":\"private-command-uuid\",\"status\":\"APPLIED\",\"result\":{\"task_id\":42,\"priority\":7}}'\n"
        "    return 'accepted ' + command\n"
        "def snapshot(mission):\n"
        "    return {'mission': {'id': mission, 'name': 'PTY Mission', 'status': 'RUNNING', 'objective': 'exercise actual curses'}}\n"
        f"raise SystemExit({'run_control' if full else 'run_console'}('pty-mission', dispatch, snapshot))\n"
    )
    env = dict(os.environ, TERM="xterm-256color", BRIDGE_TUI_RECORD=str(record))
    if editor is not None:
        env["VISUAL"] = str(editor)
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    return process, master, record


def test_real_curses_control_navigation_dispatch_and_exit(tmp_path: Path) -> None:
    process, master, record = _start_console(tmp_path)
    output = bytearray()
    try:
        output.extend(_read_until(master, process, b"PTY Mission"))
        # xterm's F3 sequence selects Stations; Tab advances to Systems.
        os.write(master, b"\x1bOR\t")
        output.extend(_read_until(master, process, b"Systems"))
        os.write(master, b"help\r")
        assert _wait_for_record(master, process, record) == "('pty-mission', 'help', '')\n"
        output.extend(_read_until(master, process, b"accepted help"))
        os.write(master, b"priority 42 7\r")
        output.extend(_read_until(master, process, b"Applied: priority 42 7"))
        assert _wait_for_record(master, process, record).splitlines()[-1] == "('pty-mission', 'priority 42 7', '')"
        os.write(master, b"\x04")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.write(master, b"\x04")
            process.wait(timeout=5)
        assert process.returncode == 0
        while True:
            ready, _, _ = select.select([master], [], [], 0)
            if not ready:
                break
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                break
        assert b"accepted help" in output
        assert b"Applied: priority 42 7" in output
        assert b"private-command-uuid" not in output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master)


def test_real_curses_editor_roundtrip_dispatches_and_removes_private_file(tmp_path: Path) -> None:
    editor = tmp_path / "fake-editor"
    editor.write_text(
        "#!" + sys.executable + "\n"
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[-1]).write_text('first line\\nsecond line\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    editor.chmod(0o700)
    process, master, record = _start_console(tmp_path, editor=editor)
    try:
        _read_until(master, process, b"PTY Mission")
        os.write(master, b"\x05")
        rendered = _wait_for_record(master, process, record)
        assert rendered
        mission, command, payload = ast.literal_eval(rendered)
        argv = shlex.split(command)
        assert mission == "pty-mission" and argv[0] == "add"
        assert argv[1].startswith("/tmp/llm-bridge-task-")
        assert payload == "first line\nsecond line\n"
        task_file = Path(argv[1])
        assert not task_file.exists()
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            os.write(master, b"\x04")
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    os.read(master, 65536)
                except OSError:
                    pass
        assert process.poll() is not None
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master)


def test_real_curses_ctrl_e_preserves_typed_task_options(tmp_path: Path) -> None:
    editor = tmp_path / "fake-editor"
    editor.write_text(
        "#!" + sys.executable + "\n"
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[-1]).write_text('prompt line one\\nprompt line two', encoding='utf-8')\n",
        encoding="utf-8",
    )
    editor.chmod(0o700)
    process, master, record = _start_console(tmp_path, editor=editor)
    try:
        _read_until(master, process, b"PTY Mission")
        os.write(master, b"task add --priority 5 --acceptance 'tests pass'")
        os.write(master, b"\x05")
        rendered = _wait_for_record(master, process, record)
        mission, command, prompt = ast.literal_eval(rendered)
        argv = shlex.split(command)
        assert mission == "pty-mission"
        assert argv[:7] == ["task", "add", "--priority", "5", "--acceptance", "tests pass", "--file"]
        assert prompt == "prompt line one\nprompt line two"
        assert not Path(argv[-1]).exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master)


def test_real_compact_console_pty_dispatches_and_exits(tmp_path: Path) -> None:
    process, master, record = _start_console(tmp_path, full=False)
    try:
        _read_until(master, process, b"PTY Mission")
        os.write(master, b"status\r")
        assert _wait_for_record(master, process, record) == "('pty-mission', 'status', '')\n"
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            os.write(master, b"\x04")
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    os.read(master, 65536)
                except OSError:
                    pass
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master)


def test_real_curses_orders_inspection_and_ctrl_r_prefilled_amendment(tmp_path: Path) -> None:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    record = tmp_path / "order-dispatch.txt"
    editor = tmp_path / "editor"
    editor.write_text(
        "#!" + sys.executable + "\n"
        "from pathlib import Path\n"
        "import sys\n"
        "path = Path(sys.argv[-1])\n"
        "path.write_text(path.read_text(encoding='utf-8') + '\\nchanged by editor', encoding='utf-8')\n",
        encoding="utf-8",
    )
    editor.chmod(0o700)
    script = (
        "import os, shlex\n"
        "from pathlib import Path\n"
        "from llm_tools.bridge_tui import run_control\n"
        "record = Path(os.environ['BRIDGE_TUI_RECORD'])\n"
        "def dispatch(mission, command):\n"
        "    argv = shlex.split(command)\n"
        "    text = Path(argv[-1]).read_text(encoding='utf-8') if argv and argv[0] == 'amend' else ''\n"
        "    record.write_text(repr((mission, command, text)), encoding='utf-8')\n"
        "    return 'amend queued'\n"
        "def snapshot(mission):\n"
        "    return {'mission': {'id': mission, 'name': 'PTY Mission', 'status': 'RUNNING'}, 'tasks': [{"
        "'id': 73, 'title': 'PTY order', 'status': 'READY', 'priority': 8, 'revision': 2,"
        "'prompt': 'keep this prompt\\nwith its second line', 'depends_on': [11],"
        "'acceptance': ['actual terminal test'], 'evidence': ['prior evidence'],"
        "'pending': {'priority': 9}}]}\n"
        "raise SystemExit(run_control('pty-order', dispatch, snapshot))\n"
    )
    env = dict(os.environ, TERM="xterm-256color", BRIDGE_TUI_RECORD=str(record), VISUAL=str(editor))
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdin=slave, stdout=slave, stderr=slave,
        env=env, close_fds=True,
    )
    os.close(slave)
    try:
        _read_until(master, process, b"PTY Mission")
        os.write(master, b"\t")  # Orders view.
        _read_until(master, process, b"73 READY PTY order")  # Wait until the async snapshot is visible.
        os.write(master, b"\r")  # Inspect the selected order.
        rendered = _read_until(master, process, b"Acceptance criteria")
        assert b"Dependencies: 11" in rendered
        os.write(master, b"\x12")  # Ctrl-R opens the editor with the current prompt.
        recorded = _wait_for_record(master, process, record)
        mission, command, prompt = ast.literal_eval(recorded)
        assert mission == "pty-order"
        assert shlex.split(command)[:2] == ["amend", "73"]
        assert prompt == "keep this prompt\nwith its second line\nchanged by editor"
        assert not Path(shlex.split(command)[-1]).exists()
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            os.write(master, b"\x04")
            select.select([master], [], [], 0.05)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master)
