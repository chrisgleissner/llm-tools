"""Assert final visible cells and real terminal caret, not ANSI output deltas."""
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

from llm_tools.bridge_input import cell_width
from llm_tools.terminal import TmuxTerminalBackend


@pytest.mark.skipif(shutil.which('tmux') is None, reason='tmux required')
def test_attached_control_caret_and_exact_source_cells(tmp_path: Path, env):
    driver = tmp_path / 'cursor_driver.py'
    driver.write_text('import json,os\nfrom pathlib import Path\nimport llm_tools.bridge_tui as tui\n'
                      'original=tui._draw_full\n'
                      'def draw(window,state):\n'
                      '    original(window,state)\n'
                      '    Path("cursor.json").write_text(json.dumps([state.command,state.command_cursor,window.getyx(),window.getmaxyx(),list(os.get_terminal_size()),os.environ.get("LINES"),os.environ.get("COLUMNS")]))\n'
                      'tui._draw_full=draw\n'
                      'tui.run_control("caret", lambda *_: "ok", lambda *_args, **_kwargs: '
                      '{"mission": {"id": "caret", "name": "Caret acceptance", "status": "CREATED"}})\n')
    env = dict(env, PYTHONPATH=str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH', ''))
    backend = TmuxTerminalBackend()
    handle = backend.start(provider_argv=['env', *[f'{key}={value}' for key, value in env.items()
                            if key in {'HOME','PATH','PYTHONPATH','COVERAGE_PROCESS_START','COVERAGE_FILE','TERM'}],
                            sys.executable, str(driver)],
                           bridge_argv=[sys.executable, '-c', 'import time; time.sleep(60)'],
                           cwd=tmp_path, socket_path=tmp_path/'cursor.sock', session_name='cursor')
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
    client = subprocess.Popen(['tmux', '-S', str(handle.socket_path), 'attach-session', '-t', handle.session_name],
                              stdin=slave, stdout=slave, stderr=slave, env=dict(env, TERM='xterm-256color'), close_fds=True)
    os.close(slave)

    def wait_for(predicate):
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            if select.select([master], [], [], .01)[0]:
                try: os.read(master, 65536)
                except OSError: pass
            if predicate(): return
        assert predicate(), repr(backend.capture(handle, pane='provider')) + ' cursor=' + caret() + ' client=' + str(client.poll()) + ' state=' + (tmp_path/'cursor.json').read_text()

    def caret():
        return subprocess.check_output(['tmux','-S',str(handle.socket_path),'display-message','-p','-t',handle.provider_pane,
                                        '#{cursor_x} #{cursor_y} #{cursor_flag} #{pane_width} #{pane_height}'], text=True).strip()
    try:
        wait_for(lambda: 'Caret acceptance' in backend.capture(handle, pane='provider'))
        source = 'a  中e\u0301🙂 '
        backend.send_keys(handle, pane='provider', keys=source)
        wait_for(lambda: source.rstrip() in backend.capture(handle, pane='provider'))
        wait_for(lambda: caret() == f'{2+cell_width(source)} 21 1 80 23')
        backend.send_keys(handle, pane='provider', keys='\x01')
        wait_for(lambda: caret() == '2 21 1 80 23')
        backend.send_keys(handle, pane='provider', keys='X')
        wait_for(lambda: ('X'+source).rstrip() in backend.capture(handle, pane='provider'))
        wait_for(lambda: caret() == '3 21 1 80 23')
        backend.send_keys(handle, pane='provider', keys='C-u')
        backend.send_keys(handle, pane='provider', keys='C-d')
    finally:
        backend.stop(handle)
        if client.poll() is None: client.terminate()
        client.wait(timeout=5)
        os.close(master)
