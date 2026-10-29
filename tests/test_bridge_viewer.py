"""Standalone artifact reading and explicit follow behavior."""
from __future__ import annotations

import curses
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_viewer


class Window:
    def __init__(self, actions, *, size=(24, 80)):
        self.actions = iter(actions)
        self.size = size

    def keypad(self, _enabled): pass
    def timeout(self, _milliseconds): pass
    def getmaxyx(self): return self.size
    def get_wch(self):
        action = next(self.actions)
        if callable(action):
            action()
            raise curses.error()
        return action


def _run(monkeypatch, path: Path, actions, *, follow=True):
    captures = []
    clock = [0.0]

    def monotonic():
        clock[0] += .3
        return clock[0]

    def draw(_window, state):
        viewer = state.document_viewer
        captures.append((viewer.source, viewer.top, state.document_notice))

    monkeypatch.setattr(bridge_viewer.time, "monotonic", monotonic)
    monkeypatch.setattr(bridge_viewer.curses, "wrapper", lambda callback: callback(Window(actions)))
    monkeypatch.setattr(bridge_viewer, "signature", bridge_viewer.signature)
    monkeypatch.setattr("llm_tools.bridge_tui.initialize_colors", lambda: None)
    monkeypatch.setattr("llm_tools.bridge_tui._draw_document", draw)
    result = bridge_viewer.run_document(bridge_viewer.read_source(path), title="Artifact", path=path, follow=follow)
    assert result == 0
    return captures


def test_non_tty_view_prints_original_utf8_crlf_without_creating_db(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    source = "# heading\r\n  中 🙂\ttext\r\n"
    artifact = tmp_path / "artifact.md"
    artifact.write_bytes(source.encode())
    monkeypatch.setattr(bridge.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(bridge.sys.stdout, "isatty", lambda: False)
    assert bridge.main(["view", "--file", str(artifact)]) == 0
    assert capsys.readouterr().out == source
    assert artifact.read_bytes() == source.encode()
    assert not (tmp_path / "state").exists()


def test_non_tty_follow_fails_explicitly_without_mutating_source(tmp_path, monkeypatch, capsys):
    artifact = tmp_path / "artifact.log"
    artifact.write_bytes(b"first\r\n")
    monkeypatch.setattr(bridge.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(bridge.sys.stdout, "isatty", lambda: False)
    assert bridge.main(["view", "--file", str(artifact), "--follow"]) == 2
    assert "interactive terminal" in capsys.readouterr().err
    assert artifact.read_bytes() == b"first\r\n"


def test_follow_shows_new_output_then_up_pauses_until_end_resumes(tmp_path, monkeypatch):
    artifact = tmp_path / "run.log"
    artifact.write_text("one\ntwo\n")
    def append(): artifact.write_text("one\ntwo\nthree\n")
    def append_again(): artifact.write_text("one\ntwo\nthree\nfour\n")
    captures = _run(monkeypatch, artifact, [append, "KEY_UP", append_again, "KEY_END", "q"])
    assert any("three" in source for source, _, _ in captures)
    assert any("Paused" in notice and "new output" in notice for _, _, notice in captures)
    assert captures[-1][0].endswith("four\n")
    assert artifact.read_text().endswith("four\n")


def test_follow_truncation_replaces_visible_source_and_labels_history_loss(tmp_path, monkeypatch):
    artifact = tmp_path / "run.log"
    artifact.write_text("older content\n" * 5)
    def truncate(): artifact.write_text("new\n")
    captures = _run(monkeypatch, artifact, [truncate, "q"])
    assert any(source == "new\n" and "rotated/truncated" in notice for source, _, notice in captures)


def test_follow_read_error_retains_last_source_then_recovers(tmp_path, monkeypatch):
    artifact = tmp_path / "run.log"
    artifact.write_text("retained\n")
    def disappear(): artifact.unlink()
    def restore(): artifact.write_text("restored\n")
    captures = _run(monkeypatch, artifact, [disappear, restore, "q"])
    assert any(source == "retained\n" and "retained source" in notice for source, _, notice in captures)
    assert captures[-1][0] == "restored\n"


def test_no_follow_keeps_opened_source_after_file_changes(tmp_path, monkeypatch):
    artifact = tmp_path / "run.log"
    artifact.write_text("original\n")
    def change(): artifact.write_text("changed\n")
    captures = _run(monkeypatch, artifact, [change, "q"], follow=False)
    assert captures[-1][0] == "original\n"
    assert artifact.read_text() == "changed\n"


def test_static_view_does_not_offer_unavailable_resume_action(tmp_path, monkeypatch):
    artifact = tmp_path / "static.log"
    artifact.write_text("opened source\n")
    def append(): artifact.write_text("opened source\nnew output\n")
    captures = _run(monkeypatch, artifact, [append, "KEY_END", "q"], follow=False)
    assert captures[-1][0] == "opened source\n"
    assert all("End resumes" not in notice for _, _, notice in captures)


def test_standalone_viewer_ignores_unknown_escape_without_changing_source(tmp_path,monkeypatch):
    artifact=tmp_path/'source.md';artifact.write_text('first\nsecond\nTAIL\n')
    monkeypatch.setattr(curses,'keyname',lambda key:b'KEY_DOWN')
    captures=_run(monkeypatch,artifact,[curses.KEY_DOWN,'\x1b','[','9','9','9','~','q'],follow=False)
    assert len(captures)>=3
    assert all(source=='first\nsecond\nTAIL\n' for source,_,_ in captures)


def test_standalone_lone_escape_closes_and_invalid_key_is_ignored(tmp_path,monkeypatch):
    artifact=tmp_path/'source.md';artifact.write_text('source\n')
    monkeypatch.setattr(curses,'keyname',lambda key:(_ for _ in ()).throw(ValueError('unknown key')))
    captures=_run(monkeypatch,artifact,[99999,'\x1b',lambda:None],follow=False)
    assert len(captures)>=2 and artifact.read_text()=='source\n'


def test_standalone_escape_replays_next_ordinary_key_for_the_caller(tmp_path,monkeypatch):
    artifact=tmp_path/'source.md';artifact.write_text('source\n')
    replay=[]
    monkeypatch.setattr(curses,'unget_wch',replay.append)
    _run(monkeypatch,artifact,['\x1b','x'],follow=False)
    assert replay==['x']


def test_public_tty_view_opens_only_the_requested_document(tmp_path,monkeypatch):
    artifact=tmp_path/'source.md';artifact.write_bytes('source 中\r\nTAIL\n'.encode())
    state=tmp_path/'must-not-exist'
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(state))
    monkeypatch.setattr(bridge.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(bridge.sys.stdout,'isatty',lambda:True)
    opened=[]
    monkeypatch.setattr(bridge_viewer,'run_document',lambda source,**kwargs:opened.append((source,kwargs)) or 0)
    assert bridge.main(['view','--file',str(artifact),'--follow'])==0
    assert opened[0][0].encode()==artifact.read_bytes()
    assert opened[0][1]['path']==artifact and opened[0][1]['follow']
    assert not state.exists()


def test_follow_does_not_spawn_replacement_layouts_while_rendering(tmp_path,monkeypatch):
    from llm_tools.bridge_documents import DocumentViewer
    artifact=tmp_path/'source.md';artifact.write_text('old source\n')
    monkeypatch.setattr(DocumentViewer,'rendering',property(lambda self:True))
    captures=_run(monkeypatch,artifact,[lambda:artifact.write_text('old source\nnew output\n'),'q'])
    assert captures[-1][0]=='old source\n'
    assert 'pending' in captures[-1][2]
