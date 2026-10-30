"""The external editor must not run with the mission repository (agent-writable) as its working directory."""
from __future__ import annotations

import curses
import json
import os
import sys
import time
from pathlib import Path

import pytest

from llm_tools import bridge_tui
from llm_tools.bridge_tui import compose_document
from tests.test_bridge_composer_recovery import _Window


@pytest.fixture()
def cwd_recording_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An editor that records its working directory and leaves the text unchanged."""
    record = tmp_path / "editor-cwd.txt"
    editor = tmp_path / "recording-editor"
    editor.write_text(f"#!{sys.executable}\nimport os\nopen({str(record)!r}, 'w').write(os.getcwd())\n", encoding="utf-8")
    editor.chmod(0o700)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VISUAL", str(editor))
    monkeypatch.setattr(curses, "def_prog_mode", lambda: None)
    monkeypatch.setattr(curses, "endwin", lambda: None)
    monkeypatch.setattr(curses, "reset_prog_mode", lambda: None)
    return record


def test_composer_editor_runs_in_the_private_drafts_directory(tmp_path: Path, cwd_recording_editor: Path) -> None:
    assert compose_document("text", window=_Window(["\x0f", "\x13"])) == "text"
    assert Path(cwd_recording_editor.read_text(encoding="utf-8")).resolve() == (tmp_path / "state" / "drafts").resolve()


def test_task_editor_runs_in_the_private_drafts_directory(tmp_path: Path, cwd_recording_editor: Path) -> None:
    class Window(_Window):
        def __getattr__(self, name: str):
            return lambda *args, **kwargs: None

    state = bridge_tui._ConsoleState("M", lambda _m, _c: json.dumps({"status": "APPLIED", "result": {}}), lambda _i: {}, full=False)
    bridge_tui._edit_task_prompt(Window(), state, initial_text="text")  # type: ignore[arg-type]
    deadline = time.monotonic() + 5
    while state.command_pending and time.monotonic() < deadline:
        state.refresh()
        time.sleep(0.02)
    assert Path(cwd_recording_editor.read_text(encoding="utf-8")).resolve() == (tmp_path / "state" / "drafts").resolve()
    assert os.getcwd() == str(tmp_path / "repo")
