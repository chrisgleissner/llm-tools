"""The hello-world path: a new user with a fresh state directory and no config.

Drives the real ``llm-bridge`` home screen in a private tmux server from an empty
git folder: ``doctor``, ``N``, the objective, the default provider choice, Start,
the provider writing ``hello.txt`` and exiting, ``/approve``, COMPLETED. The
provider is a fake ``claude`` on PATH, so no paid capacity is used. Every screen
the user passes must say what to do next.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_tools.bridge_store import BridgeStore

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None or shutil.which("git") is None, reason="tmux and git are required")

ROOT = Path(__file__).resolve().parents[1]

FAKE_CLAUDE = """#!{python}
import json, re, sys, time
from pathlib import Path
if '--help' in sys.argv:
    print('Usage: claude --session-id UUID --resume ID'); raise SystemExit(0)
# Like the real CLI, record the session under ~/.claude/projects so Bridge can identify it.
session_id = sys.argv[sys.argv.index('--session-id') + 1]
cwd = str(Path.cwd().resolve())
record = Path.home() / '.claude' / 'projects' / re.sub(r'[^a-zA-Z0-9]', '-', cwd) / (session_id + '.jsonl')
record.parent.mkdir(parents=True, exist_ok=True)
record.write_text(json.dumps({{'type': 'user', 'sessionId': session_id, 'cwd': cwd, 'isSidechain': False}}) + '\\n')
Path('hello.txt').write_text('hello\\n')
print('fake claude: wrote hello.txt. Please exit this CLI (/exit) so Bridge can verify.', flush=True)
time.sleep(0.3)
"""


class Terminal:
    """A private tmux server running one command, driven by key presses and screen captures."""

    def __init__(self, socket: Path, env: dict[str, str], cwd: Path, command: list[str]) -> None:
        self.socket = socket
        self.session = "firstrun"
        subprocess.run(["tmux", "-S", str(socket), "-f", "/dev/null", "new-session", "-d", "-s", self.session,
                        "-x", "120", "-y", "34", "-c", str(cwd), *command], env=env, check=True, timeout=20)

    def _tmux(self, *args: str) -> str:
        done = subprocess.run(["tmux", "-S", str(self.socket), *args], capture_output=True, text=True, timeout=20)
        return done.stdout

    def screen(self) -> str:
        return self._tmux("capture-pane", "-p", "-t", self.session)

    def keys(self, *keys: str) -> None:
        time.sleep(0.15)  # keys arriving within the launcher's paste-burst window are held back, as they would be for a real paste
        self._tmux("send-keys", "-t", self.session, *keys)

    def type(self, text: str) -> None:
        self._tmux("send-keys", "-t", self.session, "-l", text)

    def wait_for(self, text: str, timeout: float = 20.0) -> str:
        deadline = time.monotonic() + timeout
        screen = ""
        while time.monotonic() < deadline:
            screen = self.screen()
            if text in screen:
                return screen
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {text!r}; the screen shows:\n{screen}")

    def close(self) -> None:
        subprocess.run(["tmux", "-S", str(self.socket), "kill-server"], capture_output=True, timeout=20)


@pytest.fixture()
def fresh(tmp_path: Path, env: dict[str, str], fake_bin: Path, _private_runtime_dir: Path):
    """A brand-new user's machine: no state directory, no llm-tools config, one signed-in provider."""
    home = Path(env["HOME"])
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "token"}}))
    claude = fake_bin / "claude"
    claude.write_text(FAKE_CLAUDE.format(python=sys.executable))
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    for name in ("codex", "opencode"):  # installed but not signed in, like a half-set-up machine
        (fake_bin / name).write_text("#!/bin/sh\nexit 123\n")
        (fake_bin / name).chmod(0o700)
    repo = tmp_path / "hello-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=20)
    env.pop("LLM_BRIDGE_STATE_DIR", None)
    state_home = tmp_path / "xdg-state"  # does not exist yet: the first run must create it
    env.update({
        "XDG_STATE_HOME": str(state_home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "LLM_TOOLS_CONFIG": str(tmp_path / "no-config.toml"),
        "LLM_SCHEDULER_USAGE_JSON": json.dumps({"claude": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}}),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH", "")])),
        "TERM": "screen-256color",
        "LANG": "C.UTF-8",
        "LLM_BRIDGE_THEME": "dark",
    })
    terminals: list[Terminal] = []
    yield env, repo, state_home / "llm-tools" / "llm-bridge", terminals, _private_runtime_dir
    for terminal in terminals:
        terminal.close()


def test_new_user_reaches_a_completed_hello_world_mission(fresh) -> None:
    env, repo, state_dir, terminals, runtime = fresh

    # 1. doctor creates the private state directory and says what to do next.
    doctor = subprocess.run([sys.executable, "-m", "llm_tools.bridge", "doctor"], env=env, cwd=repo, text=True, capture_output=True, timeout=30)
    assert doctor.returncode == 0, doctor.stderr
    assert "claude: installed, logged in" in doctor.stdout
    assert "Next" in doctor.stdout and "Ready. In your project folder run llm-bridge, press N" in doctor.stdout
    assert "choose \"Yes, I trust this folder\"" in doctor.stdout  # the fake HOME has never trusted this folder
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700

    # 2. The home screen offers a new mission and names the key.
    terminal = Terminal(runtime / "drive.sock", env, repo, [sys.executable, "-m", "llm_tools.bridge"])
    terminals.append(terminal)
    home = terminal.wait_for("New mission in this folder")
    assert "N new mission" in home

    # 3. The form defaults to the provider that is installed and signed in, not to codex.
    terminal.keys("n")
    form = terminal.wait_for("Objective:")
    assert "[1] claude" in form and "[1] codex" not in form
    assert "Tab moves on to Start" in form
    terminal.type("Create a file named hello.txt containing the word hello, then stop.")
    terminal.keys("Tab", "Tab", "Tab", "Tab", "Tab")  # Providers, Folder, Mode, Save, Start
    start = terminal.wait_for("Starts claude now")
    assert "Claude asks once to trust this folder: choose Yes" in start
    assert "Native" in start

    # 4. Start runs the provider in its station; when it exits the station says how to continue.
    terminal.keys("Enter")
    exited = terminal.wait_for("is waiting for your review. Ctrl-] then B, then /approve or /reject.")
    assert "Provider exited (status 0)" in exited
    assert (repo / "hello.txt").read_text() == "hello\n"

    # 5. Bridge controls point at /approve; the confirmation shows what is approved.
    terminal.keys("C-]")
    terminal.keys("B")
    controls = terminal.wait_for("type /approve 1")
    assert "VERIFYING" in controls
    terminal.type("/approve")
    terminal.keys("Enter")
    confirm = terminal.wait_for("Y approve")
    assert "Approve verification and mark task 1 complete?" in confirm
    terminal.keys("y")
    done = terminal.wait_for("mission completed")
    assert "COMPLETED" in done and "1/1 tasks completed" in done

    # 6. The saved state agrees with the screen.
    with BridgeStore(state_dir / "bridge.sqlite3") as store:
        mission = store.list_missions()[0]
    assert mission["status"] == "COMPLETED"


def test_doctor_next_steps_cover_the_unready_machines(tmp_path: Path) -> None:
    from llm_tools import bridge_doctor

    claude_ready = {"name": "claude", "launch_cli": "claude", "installed": True, "logged_in": True, "detail": ""}
    codex_signed_out = {"name": "codex", "launch_cli": "codex", "installed": True, "logged_in": False, "detail": "run: codex login"}
    missing = {"name": "opencode", "launch_cli": "opencode", "installed": False, "logged_in": None, "detail": "opencode is not on PATH"}
    env = {"HOME": str(tmp_path)}

    no_tmux = bridge_doctor.next_steps(tmux_available=False, providers=[claude_ready], folder=tmp_path, env=env)
    assert "Install tmux" in no_tmux[0] and "--headless" in no_tmux[0]
    assert any(step.startswith("Ready.") for step in no_tmux)

    signed_out = bridge_doctor.next_steps(tmux_available=True, providers=[codex_signed_out, missing], folder=tmp_path, env=env)
    assert signed_out == ["Sign in to a provider first: codex · run: codex login"]

    nothing = bridge_doctor.next_steps(tmux_available=True, providers=[missing], folder=tmp_path, env=env)
    assert nothing == ["Install and sign in to a provider CLI (Claude Code or Codex), then run llm-bridge doctor again."]

    (tmp_path / ".claude.json").write_text(json.dumps({"projects": {str(tmp_path): {"hasTrustDialogAccepted": True}}}))
    trusted = bridge_doctor.next_steps(tmux_available=True, providers=[claude_ready], folder=tmp_path / "child", env=env)
    assert trusted == ["Ready. In your project folder run llm-bridge, press N, type the task, then choose Start."]
