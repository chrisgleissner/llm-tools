"""Lossless form fields and durable launcher recovery using guarded dispatch."""
import json
from pathlib import Path
import curses

from llm_tools import bridge, bridge_launcher as launcher, bridge_tui as tui


class Window:
    def __init__(self, keys=(), size=(24,80)):
        self.keys=iter(keys); self.size=size; self.rows={}; self.cursor=None
    def keypad(self, value): pass
    def timeout(self, value): pass
    def getmaxyx(self): return self.size
    def get_wch(self): return next(self.keys)
    def erase(self): self.rows={}
    def refresh(self): pass
    def addnstr(self,y,x,text,count,*attrs): self.rows[y]=self.rows.get(y,'')[:x]+text[:count]
    def move(self,y,x): self.cursor=y,x


def test_form_field_cells_preserve_spaces_tabs_and_caret(monkeypatch,tmp_path):
    monkeypatch.setattr(curses,'curs_set',lambda _: None)
    state=launcher.Launcher(str(tmp_path), creating=True, objective='a  中e\u0301🙂\t ', directory=str(tmp_path))
    state.cursors[0]=3
    window=Window()
    launcher._draw(window,state)
    assert window.rows[7].startswith('> Objective: a  中')
    assert window.cursor==(7,16)
    for key in (curses.KEY_HOME,'X',curses.KEY_RIGHT,curses.KEY_BACKSPACE,curses.KEY_END,'\x15'):
        state.key(key)
    assert state.objective==''
    state.objective='kept'
    state.cursors[0]=4
    before=state.objective,state.providers,state.focus
    assert state.paste('中'*400000) is None
    assert (state.objective,state.providers,state.focus)==before
    assert 'received' in state.error
    assert state.paste('first\nsecond\n')=='compose-objective'
    assert state.objective.endswith('first\nsecond\n') and state.providers=='codex'
    state.focus=1
    assert state.paste('ignored\n') is None and state.providers=='codex'
    launcher._draw(Window(size=(12,60)),state)


def test_failed_save_retains_source_then_confirmed_retry_cleans_it(monkeypatch,tmp_path):
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(curses,'curs_set',lambda _: None)
    monkeypatch.setattr(curses,'mousemask',lambda _: None)
    monkeypatch.setattr(tui,'initialize_colors',lambda: False)
    source='FIRST\n\t  中 source\r\n'+'x'*65536+'TAIL\n'
    monkeypatch.setattr(tui,'compose_document',lambda *_args,**_kwargs: source)
    window=Window(['n','\x05',*('\t'*5),'\n','\n'])
    monkeypatch.setattr(curses,'wrapper',lambda callback: callback(window))
    failures=[]
    calls=[]
    def dispatch(args):
        calls.append(args[0])
        assert args[0] not in {'start','attach'}, 'save-only must never launch provider work'
        if args[0]=='mission' and not failures:
            failures.append(True)
            print('database busy')
            return 2
        if args[0]=='control': return 0
        drafts=list((tmp_path/'state'/'drafts').glob('*.md'))
        assert len(drafts)==1 and drafts[0].read_bytes()==source.encode()
        assert drafts[0].stat().st_mode & 0o777 == 0o600
        return bridge.main(args)
    assert launcher.run_launcher(dispatch)==0
    assert calls==['mission','mission','control']
    assert not list((tmp_path/'state'/'drafts').glob('*.md'))
    with bridge.open_store() as store:
        mission=store.list_missions()[0]
        assert store.list_tasks(mission['id'])[0]['prompt']==source
        assert mission['status']=='CREATED'


def test_launcher_start_is_explicit_and_start_failure_retains_saved_mission(monkeypatch,tmp_path):
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(curses,'curs_set',lambda _:None)
    monkeypatch.setattr(curses,'mousemask',lambda _:None)
    monkeypatch.setattr(tui,'initialize_colors',lambda:False)
    window=Window(['n',*'Objective',*('\t'*4),'\n','\x04'])
    monkeypatch.setattr(curses,'wrapper',lambda callback:callback(window))
    calls=[]
    def dispatch(args):
        calls.append(args[0])
        if args[0]=='start': return 2  # deterministic failure; never call real start
        return bridge.main(args)
    assert launcher.run_launcher(dispatch)==0
    assert calls==['mission','start']
    with bridge.open_store() as store:
        assert store.list_missions()[0]['status']=='CREATED'
