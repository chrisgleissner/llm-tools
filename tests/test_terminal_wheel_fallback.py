"""Wheel ownership follows mouse requests, including alternate-screen children."""
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
from llm_tools.terminal import TmuxTerminalBackend
from tests.conftest import scaled


@pytest.mark.skipif(shutil.which('tmux') is None,reason='tmux required')
def test_alternate_screen_without_mouse_request_uses_copy_mode_then_returns_native_keys(tmp_path:Path,env):
    received=tmp_path/'received'
    driver=tmp_path/'native.py'
    driver.write_text('import os,tty\n'
                      'tty.setraw(0)\n'
                      'os.write(1,b"\\x1b[?1049hWHEEL_READY")\n'
                      f'with open({str(received)!r},"ab",buffering=0) as stream:\n'
                      '    while True: stream.write(os.read(0,65536))\n')
    backend=TmuxTerminalBackend()
    handle=backend.start(provider_argv=[sys.executable,str(driver)],bridge_argv=[sys.executable,'-c','import time;time.sleep(30)'],cwd=tmp_path,socket_path=tmp_path/'wheel.sock',session_name='wheel')
    master,slave=pty.openpty()
    fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,80,0,0))
    client=subprocess.Popen(['tmux','-S',str(handle.socket_path),'attach-session','-t',handle.session_name],stdin=slave,stdout=slave,stderr=slave,env=dict(env,TERM='xterm-256color'),close_fds=True)
    os.close(slave)
    def mode():
        return subprocess.check_output(['tmux','-S',str(handle.socket_path),'display-message','-p','-t',handle.provider_pane,'#{alternate_on} #{mouse_any_flag} #{pane_in_mode}'],text=True).strip()
    def wait(predicate):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if select.select([master],[],[],.02)[0]:
                try: os.read(master,65536)
                except OSError: pass
            if predicate(): return
        assert predicate(),mode()
    try:
        wait(lambda:mode()=='1 0 0')
        os.write(master,b'\x1b[<64;4;4M')
        wait(lambda:mode()=='1 0 1')
        os.write(master,b'q')
        wait(lambda:mode()=='1 0 0')
        assert received.read_bytes()==b''
        os.write(master,b'q')
        wait(lambda:received.read_bytes()==b'q')
    finally:
        backend.stop(handle)
        if client.poll() is None: client.terminate()
        client.wait(timeout=scaled(5))
        os.close(master)
