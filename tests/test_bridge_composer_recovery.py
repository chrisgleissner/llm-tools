from __future__ import annotations

import curses
import time
from pathlib import Path

import pytest

from llm_tools import bridge_tui
from llm_tools.bridge_tui import _ConsoleState, compose_document


class _Window:
    def __init__(self, keys=()):
        self.keys = iter(keys)
        self.timeout_ms = 0
        self.position = (0, 0)
        self.rows: list[tuple[int, int, str]] = []

    def keypad(self, _enabled): pass
    def timeout(self, milliseconds): self.timeout_ms = milliseconds
    def getmaxyx(self): return 24, 80
    def erase(self): self.rows.clear()
    def clear(self): self.rows.clear()
    def addnstr(self, y, x, text, _width, *_attrs): self.rows.append((y, x, text))
    def move(self, y, x): self.position = (y, x)
    def refresh(self): pass
    def get_wch(self): return next(self.keys)


def test_composer_crash_keeps_private_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))

    class CrashWindow(_Window):
        calls = 0

        def get_wch(self):
            self.calls += 1
            if self.calls == 1:
                return "A"
            if self.calls == 2:
                time.sleep(0.82)
                raise curses.error()
            raise RuntimeError("simulated terminal disconnect")

    with pytest.raises(RuntimeError, match="terminal disconnect"):
        compose_document(window=CrashWindow())

    recovery = bridge_tui.LAST_RECOVERY_DRAFT
    assert recovery is not None and recovery.read_bytes() == b"A"
    assert recovery.stat().st_mode & 0o777 == 0o600
    assert recovery.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("reply", [
    '{"status":"REJECTED","result":{"error":"mission completed"}}',
    "not a durable acknowledgement",
])
def test_rejected_or_unknown_composer_save_retains_source_file(tmp_path, reply):
    draft = tmp_path / "composer.md"
    draft.write_text("verbatim\nsource\r\n", encoding="utf-8", newline="")
    state = _ConsoleState("mission", lambda *_: reply, lambda *_args, **_kwargs: {}, True)

    assert state._dispatch("task add --file composer.md", cleanup_paths=(draft,))
    deadline = time.monotonic() + 2
    while state.command_pending and time.monotonic() < deadline:
        state.refresh(force=True)
        time.sleep(0.01)
    assert not state.command_pending
    assert draft.read_bytes() == b"verbatim\nsource\r\n"
    assert "Draft retained for recovery" in "\n".join(state.output)


def test_positive_durable_acknowledgement_removes_private_source(tmp_path):
    draft = tmp_path / "composer.md"
    draft.write_text("accepted source", encoding="utf-8")
    state = _ConsoleState(
        "mission",
        lambda *_: '{"status":"APPLIED","result":{"id":7}}',
        lambda *_args, **_kwargs: {},
        True,
    )

    assert state._dispatch("task add --file composer.md", cleanup_paths=(draft,))
    deadline = time.monotonic() + 2
    while state.command_pending and time.monotonic() < deadline:
        state.refresh(force=True)
        time.sleep(0.01)
    assert not state.command_pending
    assert not draft.exists()


def test_composer_editing_keys_preserve_newlines_and_retain_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    window = _Window([curses.KEY_HOME, "X", curses.KEY_LEFT, curses.KEY_DC,
                      curses.KEY_BACKSPACE, "\x15", "\n", "x", "\x13"])

    assert compose_document("ab\ncd", window=window) == "\nx"
    recovery = bridge_tui.LAST_RECOVERY_DRAFT
    assert recovery is not None and recovery.read_text(encoding="utf-8") == "\nx"


def test_composer_discards_source_only_on_explicit_ctrl_d(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    assert compose_document("private", window=_Window(["\x04"])) is None
    assert bridge_tui.LAST_RECOVERY_DRAFT is None
    assert not list((tmp_path / "state" / "drafts").glob("composer-recovery-*.md"))


def test_overlimit_paste_is_atomic_and_reports_actual_received_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    payload = "p" * (1024 * 1024 + 1)
    window = _Window(["\x1b[200~" + payload + "\x1b[201~", "\x13"])

    assert compose_document("keep exactly", window=window) == "keep exactly"
    assert any("paste received 1048577 bytes" in row[2] for row in window.rows)


def test_incomplete_paste_times_out_without_changing_source(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    clock = Clock()
    monkeypatch.setattr(bridge_tui.time, "monotonic", clock.monotonic)

    class InterruptedPaste(_Window):
        calls = 0

        def get_wch(self):
            self.calls += 1
            if self.calls == 1:
                return "\x1b[200~partial"
            if self.calls == 2:
                clock.now += 2.1
                raise curses.error()
            return "\x13"

    window = InterruptedPaste()
    assert compose_document("keep source", window=window) == "keep source"
    assert any("Incomplete paste discarded" in row[2] for row in window.rows)


def test_external_editor_failure_keeps_original_and_private_editor_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    editor = tmp_path / "failing-editor"
    editor.write_text("#!/bin/sh\nexit 17\n", encoding="utf-8")
    editor.chmod(0o700)
    monkeypatch.setenv("VISUAL", str(editor))
    monkeypatch.setattr(curses, "def_prog_mode", lambda: None)
    monkeypatch.setattr(curses, "endwin", lambda: None)
    monkeypatch.setattr(curses, "reset_prog_mode", lambda: None)
    window = _Window(["\x0f", "\x13"])

    assert compose_document("preserve this", window=window) == "preserve this"
    assert any("Editor exited 17" in row[2] for row in window.rows)
    recovery_files = list((tmp_path / "state" / "drafts").glob("*.md"))
    assert len(recovery_files) == 2
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in recovery_files)
    assert any(path.read_text(encoding="utf-8") == "preserve this" for path in recovery_files)
