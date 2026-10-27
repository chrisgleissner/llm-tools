"""First-run navigation, shared dispatch and real terminal startup contracts."""

import curses
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time

import pytest

from llm_tools import bridge, bridge_launcher, bridge_tui
from llm_tools.bridge_launcher import Launcher
from llm_tools.bridge_store import BridgeStore


def test_home_selection_creation_and_form_validation(tmp_path):
    state = Launcher(str(tmp_path), missions=[{"id": "existing"}])
    state.key(curses.KEY_DOWN)
    assert state.key("\n") == "open"
    state.key(curses.KEY_UP)
    state.key("n")
    state.key("\n")
    state.key("\t")
    state.key("\t")
    state.key(" ")
    assert state.native
    state.key("\n")
    state.key("\n")
    assert "objective" in state.error
    state.key(curses.KEY_UP)
    state.key(curses.KEY_UP)
    state.key(curses.KEY_UP)
    state.key(curses.KEY_UP)
    for char in "first objective":
        state.key(char)
    state.key(curses.KEY_BACKSPACE)
    state.key("e")
    for _ in range(4):
        state.key("\t")
    assert state.key("\n") == "create"
    assert state.directory == str(tmp_path)
    state.key("\x1b")
    assert not state.creating
    assert state.key("q") == "quit"
    assert state.key("\x04") == "quit"
    state.key("n")
    state.focus = 0
    state.key("\x15")
    assert not state.objective


@pytest.mark.parametrize("providers,directory", [("zai", ""), ("unknown", ""), ("", ""), ("codex", "absent")])
def test_new_mission_rejects_invalid_provider_or_folder(tmp_path, providers, directory):
    state = Launcher(str(tmp_path), objective="objective", providers=providers,
                     directory=str(tmp_path / directory) if directory else "")
    with pytest.raises((ValueError, OSError)):
        state.validate()


@pytest.mark.parametrize("objective", ["A local mission", "--help"])
def test_launcher_uses_shared_create_and_control_handlers_without_start(tmp_path, monkeypatch, objective):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    work = tmp_path / "work folder"
    work.mkdir()
    monkeypatch.chdir(work)
    calls = []

    def wrapper(callback):
        # Drive the actual UI reducer through a fake terminal's key stream.
        class Window:
            keys = iter(["n", *objective, "\n", "\n", "\n", " ", "\n", "\n"])
            def keypad(self, value): pass
            def getmaxyx(self): return (24, 100)
            def get_wch(self): return next(self.keys)
        return callback(Window())

    monkeypatch.setattr(curses, "wrapper", wrapper)
    monkeypatch.setattr(curses, "curs_set", lambda value: None)
    monkeypatch.setattr(curses, "mousemask", lambda value: None)
    monkeypatch.setattr(bridge_launcher, "_draw", lambda *_: None)
    monkeypatch.setattr(bridge_tui, "initialize_colors", lambda: False)

    def dispatch(args):
        calls.append(args)
        return 0 if args[0] == "control" else bridge.main(args)

    assert bridge_launcher.run_launcher(dispatch) == 0
    assert [args[0] for args in calls] == ["mission", "control"]
    with bridge.open_store() as store:
        mission = store.list_missions()[0]
        assert mission["cwd"] == str(work)
        assert mission["status"] == "CREATED" and mission["config"]["native_tui"]
        assert mission["config"]["providers"] == "codex"
        assert store.list_tasks(mission["id"])[0]["original_prompt"] == objective


def test_launcher_opens_explicit_existing_mission_or_quits(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge.open_store() as store:
        mission = store.create_mission("Existing", "objective", cwd=tmp_path)
    monkeypatch.setattr(bridge_tui, "initialize_colors", lambda: False)
    monkeypatch.setattr(bridge_launcher, "_draw", lambda *_: None)
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "mousemask", lambda value: None)
    class Window:
        keys = iter([curses.KEY_DOWN, "\n"])
        def keypad(self, value): pass
        def getmaxyx(self): return (24, 100)
        def get_wch(self): return next(self.keys)
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(Window()))
    calls = []
    assert bridge_launcher.run_launcher(lambda args: calls.append(args) or 0) == 0
    assert calls == [["control", "--mission", mission["id"]]]
    monkeypatch.setattr(curses, "wrapper", lambda _: "quit")
    assert bridge_launcher.run_launcher(lambda _: pytest.fail("quit dispatched work")) == 0


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_bare_command_runs_from_unrelated_folder_and_creates_without_provider(tmp_path, env, theme):
    work = tmp_path / "unrelated folder"
    work.mkdir()
    env.update(LLM_BRIDGE_STATE_DIR=str(tmp_path / "state"), TERM="xterm-256color", LLM_BRIDGE_THEME=theme)
    env.pop("NO_COLOR", None)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH", ""))))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    process = subprocess.Popen([sys.executable, "-m", "llm_tools.bridge"], cwd=work, env=env,
                               stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()

    def wait_for(marker):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker in output:
                return
            if select.select([master], [], [], .03)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
        pytest.fail(f"launcher did not show {marker!r}; exit={process.poll()}; output={output!r}")

    try:
        wait_for(b"New mission in this folder")
        assert b"Working directory:" in output
        assert str(work).encode()[:60] in output
        assert (b"38;5;39m" if theme == "dark" else b"38;5;25m") in output
        os.write(master, b"n")
        wait_for(b"Objective:")
        os.write(master, b"Any folder mission\r\r\r\r\r")
        wait_for(b"CREATED")
        with BridgeStore(tmp_path / "state" / "bridge.sqlite3") as store:
            mission = store.list_missions()[0]
            assert mission["cwd"] == str(work)
            assert mission["status"] == "CREATED"
            assert not store.db.execute("SELECT 1 FROM operations").fetchone()
        os.write(master, b"\x04")
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        os.close(master)


def test_bare_noninteractive_command_prints_help_without_creating_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    assert bridge.main([]) == 0
    assert "llm-bridge" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_scrolled_home_mouse_targets_visible_mission_and_form_submit(tmp_path):
    state = Launcher(str(tmp_path), missions=[{"id": str(i)} for i in range(30)], selected=30)
    assert state.click(7, height=24, activate=True) == "open"
    assert state.selected == 17
    state.creating = True
    state.objective = "objective"
    assert state.click(15) == "create"
    state.click(9)
    assert state.focus == 1
    assert state.click(2) is None


def test_launcher_creation_error_keeps_form_and_terminal_failure_is_actionable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    calls = []
    actions = iter(["create", "quit"])
    monkeypatch.setattr(curses, "wrapper", lambda _: next(actions))
    def rejected(args):
        calls.append(args)
        print("llm-bridge: worktree already active", file=sys.stderr)
        return 2
    assert bridge_launcher.run_launcher(rejected) == 0
    assert len(calls) == 1
    assert not capsys.readouterr().err
    monkeypatch.setattr(curses, "wrapper", lambda _: (_ for _ in ()).throw(curses.error("unsupported TERM")))
    assert bridge_launcher.run_launcher(rejected) == 2
    assert "terminal unavailable" in capsys.readouterr().err
