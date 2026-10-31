"""Exercise the curses control loop through a real pseudo-terminal."""

from __future__ import annotations

import fcntl
import ast
import hashlib
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

import pytest
from tests.conftest import scaled


def _read_until(
    master: int,
    process: subprocess.Popen[bytes],
    marker: bytes,
    timeout: float = 5.0,
    output_sink: bytearray | None = None,
) -> bytes:
    deadline = time.monotonic() + scaled(timeout)
    output = output_sink if output_sink is not None else bytearray()
    if marker in output:
        return bytes(output)
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


def _wait_for_record(
    master: int,
    process: subprocess.Popen[bytes],
    record: Path,
    timeout: float = 5.0,
    output_sink: bytearray | None = None,
) -> str:
    deadline = time.monotonic() + scaled(timeout)
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
                data = os.read(master, 65536)
                if output_sink is not None:
                    output_sink.extend(data)
            except OSError:
                break
    return record.read_text(encoding="utf-8") if record.exists() else ""


def _wait_for_record_count(process: subprocess.Popen[bytes], record: Path, count: int, timeout: float = 5.0) -> list[str]:
    deadline = time.monotonic() + scaled(timeout)
    while time.monotonic() < deadline:
        if record.exists():
            rows = record.read_text(encoding="utf-8").splitlines()
            if len(rows) >= count:
                return rows
        if process.poll() is not None:
            break
        time.sleep(0.01)
    return record.read_text(encoding="utf-8").splitlines() if record.exists() else []


def _wait_for_missing(path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + scaled(timeout)
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not path.exists(), f"private draft was not cleaned after durable acknowledgement: {path}"


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
        "    payload = open(argv[-1], encoding='utf-8', newline='').read() if argv and (argv[0] == 'add' or (argv[:2] == ['task', 'add'] and '--file' in argv)) else ''\n"
        "    with open(record, 'a', encoding='utf-8') as stream: stream.write(repr((mission, command, payload)) + '\\n')\n"
        "    if argv and argv[0] == 'priority':\n"
        "        return '{\"id\":\"private-command-uuid\",\"status\":\"APPLIED\",\"result\":{\"task_id\":42,\"priority\":7}}'\n"
        "    if argv[:3] == ['task', 'add', '--help']:\n"
        "        return 'usage: task add [options]\\n' + '\\n'.join('--option-' + str(i) for i in range(30))\n"
        "    if argv[:2] in (['task', 'add'], ['task', 'amend']):\n"
        "        return '{\"status\":\"APPLIED\",\"result\":{\"detail\":\"task source saved\"}}'\n"
        "    if argv and argv[0] == 'status': return 'status result: RUNNING'\n"
        "    return 'accepted ' + command\n"
        "def snapshot(mission):\n"
        "    return {'mission': {'id': mission, 'name': 'PTY Mission', 'status': 'RUNNING', 'objective': 'exercise actual curses'}}\n"
        f"raise SystemExit({'run_control' if full else 'run_console'}('pty-mission', dispatch, snapshot))\n"
    )
    env = dict(os.environ, TERM="xterm-256color", BRIDGE_TUI_RECORD=str(record),
               LLM_BRIDGE_STATE_DIR=str(tmp_path / "bridge-state"))
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
        output.extend(_read_until(master, process, b"RUNNING"))
        # Wait for the view title rather than the always-visible tab legend.
        os.write(master, b"\x1bOR")
        output.extend(_read_until(master, process, b"No providers recorded.", output_sink=output))
        os.write(master, b"\t")
        _read_until(master, process, b"[Activity]", output_sink=output)
        os.write(master, b"status\r")
        assert _wait_for_record(master, process, record, output_sink=output) == "('pty-mission', 'status', '')\n"
        if b"status result: RUNNING" not in output:
            _read_until(master, process, b"status result: RUNNING", output_sink=output)
        os.write(master, b"priority 42 7\r")
        _read_until(master, process, b"Applied: priority 42 7", output_sink=output)
        assert _wait_for_record(master, process, record, output_sink=output).splitlines()[-1] == "('pty-mission', 'priority 42 7', '')"
        os.write(master, b"\x04")
        try:
            process.wait(timeout=scaled(5))
        except subprocess.TimeoutExpired:
            os.write(master, b"\x04")
            process.wait(timeout=scaled(5))
        assert process.returncode == 0
        while True:
            ready, _, _ = select.select([master], [], [], 0)
            if not ready:
                break
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                break
        assert b"status result: RUNNING" in output
        assert b"Applied: priority 42 7" in output
        assert b"private-command-uuid" not in output
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=scaled(2))
        os.close(master)


def test_real_curses_multiline_command_response_scrolls_dismisses_and_accepts_next_command(tmp_path: Path) -> None:
    process, master, record = _start_console(tmp_path)
    try:
        output = bytearray()
        _read_until(master, process, b"RUNNING", output_sink=output)
        os.write(master, b"task add --help\r")
        recorded = _wait_for_record_count(process, record, 1)
        assert len(recorded) == 1
        _read_until(master, process, b"--option-0")
        os.write(master, b"\x1b[6~")  # Page Down in xterm.
        _read_until(master, process, b"--option-29")
        os.write(master, b"\x1b")
        time.sleep(0.15)  # Let curses resolve a standalone Escape key.
        os.write(master, b"status\r")
        _read_until(master, process, b"status result: RUNNING")
        records = _wait_for_record_count(process, record, 2)
        assert records[-2:] == [
            "('pty-mission', 'task add --help', '')",
            "('pty-mission', 'status', '')",
        ]
        os.write(master, b"\x04")
        process.wait(timeout=scaled(5))
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=scaled(2))
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
        _read_until(master, process, b"RUNNING")
        output = bytearray()
        os.write(master, b"\x05")
        _read_until(master, process, b"full source")
        os.write(master, b"\x0f")  # Shared composer exposes the external editor action.
        _read_until(master, process, b"second line")
        os.write(master, b"\x13")
        rendered = _wait_for_record(master, process, record, output_sink=output)
        assert rendered
        mission, command, payload = ast.literal_eval(rendered)
        if b"Task added" not in output:
            _read_until(master, process, b"Task added", output_sink=output)
        argv = shlex.split(command)
        assert mission == "pty-mission" and argv[:2] == ["task", "add"]
        assert Path(argv[-1]).parent.name == "drafts"
        assert payload == "first line\nsecond line\n"
        task_file = Path(argv[-1])
        _wait_for_missing(task_file)
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
            process.wait(timeout=scaled(2))
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
        _read_until(master, process, b"RUNNING")
        output = bytearray()
        os.write(master, b"task add --priority 5 --acceptance 'tests pass'")
        os.write(master, b"\x1b")  # Return from command entry to body shortcuts.
        os.write(master, b"\x05")
        _read_until(master, process, b"full source")
        os.write(master, b"\x0f")
        _read_until(master, process, b"prompt line two")
        os.write(master, b"\x13")
        rendered = _wait_for_record(master, process, record, output_sink=output)
        mission, command, prompt = ast.literal_eval(rendered)
        if b"Task added" not in output:
            _read_until(master, process, b"Task added", output_sink=output)
        argv = shlex.split(command)
        assert mission == "pty-mission"
        assert argv[:7] == ["task", "add", "--priority", "5", "--acceptance", "tests pass", "--file"]
        assert Path(argv[-1]).parent.name == "drafts"
        assert prompt == "prompt line one\nprompt line two"
        _wait_for_missing(Path(argv[-1]))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=scaled(2))
        os.close(master)


def test_real_curses_short_bracketed_command_paste_waits_for_enter(tmp_path: Path) -> None:
    process, master, record = _start_console(tmp_path)
    try:
        _read_until(master, process, b"RUNNING")
        frame = b"\x1b[200~status\x1b[201~"
        for offset in range(0, len(frame), 2):
            os.write(master, frame[offset:offset + 2])
        _read_until(master, process, b"status")
        assert not record.exists(), "a pasted command must stay editable until Enter"
        os.write(master, b"\r")
        assert _wait_for_record(master, process, record) == "('pty-mission', 'status', '')\n"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=scaled(2))
        os.close(master)


@pytest.mark.parametrize("size_kib", [64, 256])
def test_real_curses_bracketed_multiline_paste_preserves_source_sha256(tmp_path: Path, size_kib: int) -> None:
    source = ("# Unicode prompt 😀\r\n\tKeep exact source; combining e\u0301\n" + "x" * (size_kib * 1024) + "\nTAIL-EXACT-EOF\n")
    expected = hashlib.sha256(source.encode("utf-8")).hexdigest()
    process, master, record = _start_console(tmp_path)
    try:
        output = bytearray()
        _read_until(master, process, b"RUNNING", output_sink=output)
        os.write(master, b"\x05")
        _read_until(master, process, b"Add task prompt")
        payload = source.encode("utf-8")
        frame = b"\x1b[200~" + payload + b"\x1b[201~"
        for offset in range(0, len(frame), 4096):
            os.write(master, frame[offset:offset + 4096])
        _read_until(master, process, b"TAIL-EXACT-EOF")
        os.write(master, b"\x13")
        recorded = _wait_for_record(master, process, record, timeout=10, output_sink=output)
        mission, command, received = ast.literal_eval(recorded)
        assert mission == "pty-mission"
        assert shlex.split(command)[:2] == ["task", "add"]
        assert received == source
        assert hashlib.sha256(received.encode("utf-8")).hexdigest() == expected
        _read_until(master, process, b"Task added", timeout=scaled(5), output_sink=output)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=scaled(2))
        os.close(master)


def test_real_compact_console_pty_dispatches_and_exits(tmp_path: Path) -> None:
    process, master, record = _start_console(tmp_path, full=False)
    try:
        _read_until(master, process, b"RUNNING")
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
            process.wait(timeout=scaled(2))
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
        "    if argv[:2] == ['task', 'show']:\n"
        "        import json\n"
        "        return json.dumps({'id':73,'prompt':'keep this prompt\\nwith its second line'})\n"
        "    text = Path(argv[-1]).read_text(encoding='utf-8') if argv[:2] == ['task', 'amend'] else ''\n"
        "    record.write_text(repr((mission, command, text)), encoding='utf-8')\n"
        "    return '{\"status\":\"APPLIED\",\"result\":{\"detail\":\"amend queued\"}}'\n"
        "def snapshot(mission):\n"
        "    return {'mission': {'id': mission, 'name': 'PTY Mission', 'status': 'RUNNING'}, 'tasks': [{"
        "'id': 73, 'title': 'PTY order', 'status': 'READY', 'priority': 8, 'revision': 2,"
        "'prompt': 'keep this prompt\\nwith its second line', 'depends_on': [11],"
        "'acceptance': ['actual terminal test'], 'evidence': ['prior evidence'],"
        "'pending': {'priority': 9}}]}\n"
        "raise SystemExit(run_control('pty-order', dispatch, snapshot))\n"
    )
    env = dict(os.environ, TERM="xterm-256color", BRIDGE_TUI_RECORD=str(record), VISUAL=str(editor),
               LLM_BRIDGE_STATE_DIR=str(tmp_path / "bridge-state"))
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdin=slave, stdout=slave, stderr=slave,
        env=env, close_fds=True,
    )
    os.close(slave)
    try:
        _read_until(master, process, b"RUNNING")
        os.write(master, b"\t")  # Orders view.
        _read_until(master, process, b"READY  P8  after 11  PTY order")  # Wait until the async snapshot is visible.
        os.write(master, b"\r")  # Inspect the selected order.
        rendered = _read_until(master, process, b"Acceptance criteria")
        assert b"Dependencies: 11" in rendered
        os.write(master, b"\x12")  # Ctrl-R opens the shared amendment composer.
        _read_until(master, process, b"full source")
        os.write(master, b"\x0f")  # The external editor remains available from the composer.
        _read_until(master, process, b"changed by editor")
        os.write(master, b"\x13")
        output = bytearray()
        recorded = _wait_for_record(master, process, record, output_sink=output)
        mission, command, prompt = ast.literal_eval(recorded)
        assert mission == "pty-order"
        assert shlex.split(command)[:2] == ["task", "amend"]
        assert shlex.split(command)[3] == "73"
        assert prompt == "keep this prompt\nwith its second line\nchanged by editor"
        if b"Task 73 amended" not in output:
            _read_until(master, process, b"Task 73 amended", output_sink=output)
        _wait_for_missing(Path(shlex.split(command)[-1]))
        os.write(master, b"\x1b\x15\x1b")  # Dismiss result, clear any draft, return to body shortcuts.
        deadline = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < deadline:
            os.write(master, b"\x04")
            select.select([master], [], [], 0.05)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=scaled(2))
        os.close(master)
