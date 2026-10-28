from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time

from llm_tools.bridge_store import BridgeStore


def test_real_launcher_paste_saves_64kib_objective_verbatim_without_start(tmp_path, env):
    state_dir = tmp_path / "state"
    workdir = tmp_path / "unrelated work"
    workdir.mkdir()
    prefix = "Objective 😀\r\n\tIndented e\u0301\n"
    tail = "\nTAIL-OBJECTIVE-EXACT\n"
    total_bytes = 64 * 1024
    source = prefix + "x" * (total_bytes - len(prefix.encode()) - len(tail.encode())) + tail
    expected_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    root = str(Path(__file__).resolve().parents[1])
    env = dict(env, LLM_BRIDGE_STATE_DIR=str(state_dir), TERM="xterm-256color",
               PYTHONPATH=os.pathsep.join(filter(None, (root, env.get("PYTHONPATH", "")))))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    process = subprocess.Popen([sys.executable, "-m", "llm_tools.bridge"], cwd=workdir, env=env,
                               stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    output = bytearray()

    def wait_for(marker: bytes, timeout: float = 8) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process.poll() is None:
            if marker in output:
                return
            if select.select([master], [], [], 0.05)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
        assert marker in output, f"launcher did not render {marker!r}; exit={process.poll()} output={bytes(output[-1000:])!r}"

    try:
        wait_for(b"New mission in this folder")
        os.write(master, b"n")
        wait_for(b"Objective:")
        frame = b"\x1b[200~" + source.encode("utf-8") + b"\x1b[201~"
        for offset in range(0, len(frame), 4096):
            os.write(master, frame[offset:offset + 4096])
        wait_for(f"{total_bytes}/1048576 bytes".encode("ascii"), timeout=12)
        os.write(master, b"\x13")  # Save the complete composer source to the form.
        wait_for(b"Save mission only")
        os.write(master, b"\t\t\t\t\t\r")
        deadline = time.monotonic() + 10
        mission = None
        while time.monotonic() < deadline:
            if (state_dir / "bridge.sqlite3").exists():
                with BridgeStore(state_dir / "bridge.sqlite3") as store:
                    missions = store.list_missions()
                    if missions:
                        mission = missions[0]
                        tasks = store.list_tasks(mission["id"])
                        if tasks:
                            source_saved = tasks[0]["original_prompt"]
                            if source_saved == source:
                                break
                            if len(source_saved) >= total_bytes - 1:
                                break
            if select.select([master], [], [], 0.02)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
        assert mission is not None
        with BridgeStore(state_dir / "bridge.sqlite3") as store:
            saved = store.list_tasks(mission["id"])[0]["original_prompt"]
            assert len(saved.encode("utf-8")) == total_bytes
            assert hashlib.sha256(saved.encode("utf-8")).hexdigest() == expected_hash
            assert not store.db.execute("SELECT 1 FROM operations").fetchone()
        assert process.poll() is None  # Save-only must not start paid provider work.
        wait_for(b"Pending commands:")  # Durable commit precedes control-screen readiness.
        os.write(master, b"\x04")
        deadline = time.monotonic()+5
        while process.poll() is None and time.monotonic()<deadline:
            if select.select([master], [], [], .03)[0]:
                try: output.extend(os.read(master,65536))
                except OSError: break
        process.wait(timeout=5)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        os.close(master)
