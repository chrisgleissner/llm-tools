"""Task drafts live in the private drafts directory, are removed after dispatch, and old ones are pruned."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_tui
from llm_tools.bridge_store import BridgeStore, prune_old_drafts

DAY = 24 * 3600


@pytest.fixture()
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(path))
    return path


class Window:
    def getmaxyx(self) -> tuple[int, int]:
        return 24, 80

    def __getattr__(self, name: str):
        return lambda *args, **kwargs: None


def fake_editor(monkeypatch: pytest.MonkeyPatch, seen: list[Path], text: str = "task text\nmore") -> None:
    def run(argv: list[str], *, check: bool = False, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        path = Path(argv[-1])
        seen.append(path)
        path.write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(bridge_tui.subprocess, "run", run)
    monkeypatch.setattr(bridge_tui.curses, "def_prog_mode", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "endwin", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "reset_prog_mode", lambda: None)


def test_task_editor_file_lives_in_the_private_drafts_directory(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []
    modes: list[int] = []
    fake_editor(monkeypatch, seen)
    original_run = bridge_tui.subprocess.run

    def run_and_record(argv, **kwargs):
        modes.append(stat.S_IMODE(os.stat(argv[-1]).st_mode))
        return original_run(argv, **kwargs)

    monkeypatch.setattr(bridge_tui.subprocess, "run", run_and_record)
    state = bridge_tui._ConsoleState("M", lambda _m, _c: json.dumps({"status": "APPLIED", "result": {}}), lambda _i: {}, full=False)
    bridge_tui._edit_task_prompt(Window(), state)  # type: ignore[arg-type]
    deadline = time.monotonic() + 5
    while state.command_pending and time.monotonic() < deadline:
        state.refresh()
        time.sleep(0.02)
    assert seen and seen[0].parent == state_dir / "drafts"
    assert modes == [0o600]
    assert stat.S_IMODE((state_dir / "drafts").stat().st_mode) == 0o700


def test_editor_draft_is_removed_after_a_successful_dispatch(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []
    fake_editor(monkeypatch, seen)
    state = bridge_tui._ConsoleState("M", lambda _m, _c: json.dumps({"status": "APPLIED", "result": {}}), lambda _i: {}, full=False)
    bridge_tui._edit_task_prompt(Window(), state)  # type: ignore[arg-type]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and seen and seen[0].exists():
        state.refresh()
        time.sleep(0.02)
    assert seen and not seen[0].exists()
    assert list((state_dir / "drafts").iterdir()) == []


def test_prune_removes_only_regular_files_older_than_fourteen_days(state_dir: Path, tmp_path: Path) -> None:
    drafts = state_dir / "drafts"
    drafts.mkdir(parents=True, mode=0o700)
    now = time.time()
    old, fresh, edge = drafts / "old.md", drafts / "fresh.md", drafts / "thirteen-days.md"
    for path, age in ((old, 15 * DAY), (fresh, 1 * DAY), (edge, 13 * DAY)):
        path.write_text("draft", encoding="utf-8")
        os.utime(path, (now - age, now - age))
    outside = tmp_path / "outside.md"
    outside.write_text("keep", encoding="utf-8")
    os.utime(outside, (now - 30 * DAY, now - 30 * DAY))
    link = drafts / "link.md"
    link.symlink_to(outside)
    folder = drafts / "folder"
    folder.mkdir()
    os.utime(folder, (now - 30 * DAY, now - 30 * DAY))

    assert prune_old_drafts(now=now) == 1
    assert not old.exists() and fresh.exists() and edge.exists()
    assert outside.exists() and link.is_symlink() and folder.is_dir()


def test_prune_without_a_drafts_directory_is_a_no_op(state_dir: Path) -> None:
    assert prune_old_drafts() == 0


def test_console_and_control_startup_prune_old_drafts(state_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with BridgeStore(state_dir / "bridge.sqlite3") as store:
        mission_id = store.create_mission("drafts", "Prune", cwd=tmp_path)["id"]
    drafts = state_dir / "drafts"
    drafts.mkdir(parents=True, mode=0o700, exist_ok=True)
    stale = drafts / "composer-recovery-old.md"
    stale.write_text("old", encoding="utf-8")
    ancient = time.time() - 15 * DAY
    os.utime(stale, (ancient, ancient))
    monkeypatch.setattr(bridge_tui, "run_control", lambda *a, **k: 0)
    monkeypatch.setattr(bridge.bridge_theme, "prepare", lambda: None)
    assert bridge.main(["control", "--mission", mission_id]) == 0
    assert not stale.exists()
