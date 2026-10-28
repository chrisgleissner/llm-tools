"""Authoritative source acquisition must never turn failures into amendments."""
import json
import time
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_tui as tui


def settle(state, predicate):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        state.refresh()
        if predicate(): return
        time.sleep(.005)
    assert predicate()


@pytest.mark.parametrize('reply',[None,'not-json','{}',json.dumps({'prompt':123})])
def test_failed_full_read_never_opens_an_edit_with_error_as_source(reply):
    def dispatch(*_):
        if reply is None: raise OSError('source unavailable')
        return reply
    state=tui._ConsoleState('mission',dispatch,lambda *_: {},full=True)
    state.request_task_document('1',for_edit=True)
    settle(state,lambda:not state.document_pending)
    assert not state.edit_document_requested
    assert state.edit_initial_text=='' and state.document_viewer is None
    assert any('unavailable' in line.lower() or 'could not read' in line.lower() for line in state.output)


def test_full_objective_is_authoritative_and_failure_does_not_create_source(tmp_path,monkeypatch):
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(tmp_path/'state'))
    source='# Full objective\r\n'+'中  source\n'*20000+'TAIL\n'
    with bridge.open_store() as store:
        mission=store.create_mission('Read',source,cwd=tmp_path)
    state=tui._ConsoleState(mission['id'],lambda *_:'',lambda *_:{},full=True)
    state.snapshot={'mission':{'objective':'clipped preview'}}
    state.handle_key('o')
    settle(state,lambda:state.document_viewer is not None)
    assert state.document_viewer.source==source
    state.handle_key('e')
    assert not state.edit_document_requested
    assert state.document_viewer.source==source
    state.handle_key('q')
    state.mission_id='missing'
    state.handle_key('o')
    settle(state,lambda:not state.document_pending)
    assert state.document_viewer is None and not state.edit_document_requested
    assert 'Could not read full objective' in state.output[-1]


@pytest.mark.parametrize('helper',['wl-copy','xclip','xsel','pbcopy',None])
def test_clipboard_helpers_are_explicit_local_argv_and_missing_is_supported(monkeypatch,helper):
    monkeypatch.setattr(tui.shutil,'which',lambda name: '/bin/'+name if name==helper else None)
    argv=tui._clipboard_argv()
    assert argv is None if helper is None else argv[0]==helper
    if helper=='xclip': assert argv==['xclip','-selection','clipboard']
    if helper=='xsel': assert argv==['xsel','--clipboard','--input']


def test_export_permission_failure_keeps_path_source_and_read_only_context(monkeypatch,tmp_path):
    from llm_tools.bridge_documents import DocumentViewer
    source='complete  source\r\nTAIL\n'
    state=tui._ConsoleState('mission',lambda *_:None,lambda *_:{},full=True)
    state.snapshot={'mission':{'status':'COMPLETED'}}
    state.document_task_id='1'
    state.document_viewer=DocumentViewer('Task 1 r2',source)
    target=tmp_path/'forbidden.md'
    real_open=tui.os.open
    def deny(path,*args,**kwargs):
        if Path(path)==target: raise PermissionError('permission denied')
        return real_open(path,*args,**kwargs)
    monkeypatch.setattr(tui.os,'open',deny)
    state.handle_key('s')
    for char in str(target): state.handle_key(char)
    state.handle_key('\n')
    assert state.document_export_editing and state.document_entry==str(target)
    assert 'permission denied' in state.document_notice
    assert state.document_viewer.source==source
    state.handle_key('\x1b')
    state.handle_key('e')
    assert not state.edit_document_requested
    assert 'terminal mission' in state.document_notice
    assert state.document_viewer.source==source


def test_failed_task_file_creation_retains_complete_checkpoint_and_reports_it(monkeypatch,tmp_path):
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(tmp_path/'state'))
    checkpoint=tmp_path/'checkpoint.md'
    source='complete\r\n'+'x'*65536+'TAIL\n'
    checkpoint.write_bytes(source.encode())
    monkeypatch.setattr(tui,'LAST_RECOVERY_DRAFT',checkpoint)
    monkeypatch.setattr(tui,'compose_document',lambda *args,**kwargs:source)
    monkeypatch.setattr(tui.tempfile,'mkstemp',lambda *args,**kwargs: (_ for _ in ()).throw(OSError('disk full')))
    class Window:
        def clear(self): pass
        def refresh(self): pass
    state=tui._ConsoleState('mission',lambda *_:pytest.fail('must not dispatch failed save'),lambda *_:{},full=True)
    tui._compose_task_prompt(Window(),state)
    assert checkpoint.read_bytes()==source.encode()
    assert state.edit_initial_text==source
    assert 'disk full' in state.output[-1] and str(checkpoint) in state.output[-1]


@pytest.mark.parametrize('first',['abcd','中中','e\u0301abc'])
def test_composer_vertical_moves_keep_preferred_display_column(tmp_path,monkeypatch,first):
    import curses
    from tests.test_bridge_composer_recovery import _Window
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(tmp_path/'state'))
    source=first+'\nx\nabcdef'
    keys=[curses.KEY_HOME,curses.KEY_UP,curses.KEY_UP,curses.KEY_END,curses.KEY_DOWN,curses.KEY_DOWN,'X','\x13']
    assert tui.compose_document(source,window=_Window(keys))==first+'\nx\nabcdXef'


def test_composer_page_navigation_reaches_other_lines_without_changing_source(tmp_path,monkeypatch):
    import curses
    from tests.test_bridge_composer_recovery import _Window
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(tmp_path/'state'))
    assert tui.compose_document('a\nbbbb\ncccc',window=_Window([curses.KEY_PPAGE,'X','\x13']))=='aX\nbbbb\ncccc'
    keys=[curses.KEY_HOME,curses.KEY_UP,curses.KEY_UP,curses.KEY_NPAGE,'X','\x13']
    assert tui.compose_document('a\nbbbb\ncccc',window=_Window(keys))=='a\nbbbb\nXcccc'


@pytest.mark.parametrize('keys,expected',[
    (['HOME','DELETE'],'abc\nef'),
    (['HOME','RIGHT','BACKSPACE'],'abc\nef'),
    (['HOME','RIGHT','KILL'],'abc\nd'),
    (['HOME','UP','RIGHT','KILL'],'a\ndef'),
    (['CLEAR','TAB','x'],'\tx'),
    (['HOME','LEFT','X'],'abcX\ndef'),
    (['HOME','ENTER','X'],'abc\n\nXdef'),
])
def test_composer_adjacent_edits_preserve_exact_document(tmp_path,monkeypatch,keys,expected):
    import curses
    from tests.test_bridge_composer_recovery import _Window
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(tmp_path/'state'))
    mapping={'HOME':curses.KEY_HOME,'DELETE':curses.KEY_DC,'RIGHT':curses.KEY_RIGHT,'LEFT':curses.KEY_LEFT,'UP':curses.KEY_UP,'BACKSPACE':curses.KEY_BACKSPACE,'KILL':'\x0b','CLEAR':'\x15','TAB':'\t','ENTER':'\n'}
    result=tui.compose_document('abc\ndef',window=_Window([mapping.get(key,key) for key in keys]+['\x13']))
    assert result==expected


def test_reopening_pending_amendment_edits_latest_desired_source(tmp_path,monkeypatch):
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(tmp_path/'state'))
    with bridge.open_store() as store:
        mission=store.create_mission('Pending','Original objective',cwd=tmp_path,initial_prompt='original source')
        task_id=mission['initial_task_id']
        store.amend_task(task_id,prompt='new requirements\r\nTAIL\n')
        record=store.get_task(task_id)
    state=tui._ConsoleState(mission['id'],lambda *_:json.dumps(record),lambda *_:{},full=True)
    state.request_task_document(str(task_id),for_edit=True)
    settle(state,lambda:not state.document_pending)
    assert state.edit_document_requested
    assert state.edit_initial_text=='new requirements\r\nTAIL\n'
