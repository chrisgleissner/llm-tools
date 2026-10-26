#!/usr/bin/env python3
"""Zero-model-call mouse check on an explicitly supplied, running Bridge station.

Open the provider's /model picker first. The probe sends one SGR click to the
current model. Only a visible effort-picker response passes the Codex oracle.
No transcript is printed or saved. Other effects remain NOT VERIFIED.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import termios
import time


def mouse_result(before: str, after: str) -> str:
    if 'Select Model' in before and 'Select Reasoning Effort' in after:
        return 'PASS: native click opened effort picker'
    return 'NOT VERIFIED: no expected native mouse effect observed'


def probe(socket: Path, session: str, pane: str) -> dict:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
    def controlling_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    env = {key: value for key, value in os.environ.items() if key != 'TMUX'}
    env['TERM'] = 'xterm-256color'
    process = None
    def drain(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([master], [], [], .05)[0]:
                try:
                    if not os.read(master, 65536): break
                except OSError:
                    break
    def capture():
        return subprocess.check_output(['tmux', '-S', str(socket), 'capture-pane', '-p', '-t', pane], text=True, timeout=10)
    try:
        process = subprocess.Popen(['tmux', '-S', str(socket), 'attach-session', '-t', session],
            stdin=slave, stdout=slave, stderr=slave, env=env, preexec_fn=controlling_terminal)
        drain(.5)
        before = capture()
        row = next((i+1 for i, line in enumerate(before.splitlines()) if '(current)' in line), None)
        if row is None:
            return {'provider_mouse': 'NOT VERIFIED: open native /model picker first'}
        # Use pane's actual origin; never assume it occupies the whole terminal.
        geometry = subprocess.check_output(['tmux', '-S', str(socket), 'display-message', '-p', '-t', pane,
                                           '#{pane_left},#{pane_top}'], text=True, timeout=10)
        left, top = map(int, geometry.strip().split(','))
        os.write(master, f'\x1b[<0;{left+12};{top+row}M\x1b[<0;{left+12};{top+row}m'.encode())
        drain(.7)
        return {'provider_mouse': mouse_result(before, capture()), 'model_calls': 0}
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)
        os.close(master)
        os.close(slave)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--pane', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    result = probe(args.socket, args.session, args.pane)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))
    return 0 if result['provider_mouse'].startswith('PASS') else 1


if __name__ == '__main__':
    raise SystemExit(main())
