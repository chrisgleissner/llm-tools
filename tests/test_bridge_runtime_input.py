"""Deterministic control-loop ownership and asynchronous feedback contracts."""
import curses
from types import SimpleNamespace
import time

import pytest

from llm_tools import bridge, bridge_tui as tui, terminal


class Window:
    def __init__(self, keys):
        self.keys=iter(keys)
        self.rows={}
    def keypad(self,value): pass
    def timeout(self,value): pass
    def getmaxyx(self): return 24,80
    def erase(self): self.rows={}
    def clear(self): self.rows={}
    def refresh(self): pass
    def noutrefresh(self): pass
    def move(self,*args): pass
    def addnstr(self,y,x,text,width,*attrs): self.rows[y]=self.rows.get(y,'')[:x]+text[:width]
    def get_wch(self):
        key=next(self.keys)
        return key() if callable(key) else key


def run_control(monkeypatch,keys,*,snapshot=None,dispatch=None):
    window=Window(keys)
    monkeypatch.setattr(curses,'wrapper',lambda callback:callback(window))
    monkeypatch.setattr(curses,'curs_set',lambda _:None)
    monkeypatch.setattr(curses,'mousemask',lambda _:None)
    monkeypatch.setattr(curses,'doupdate',lambda:None)
    monkeypatch.setattr(tui,'initialize_colors',lambda:False)
    return tui.run_control('test',dispatch or (lambda *_:''),snapshot or (lambda *_args,**_kwargs:{}))


def test_status_publication_is_async_and_independent_of_ambient_tmux(monkeypatch):
    published=[]
    monkeypatch.setenv('TMUX_PANE','%owned-test')
    monkeypatch.setattr(bridge,'_load_handle',lambda _:SimpleNamespace(bridge_pane='%owned-test'))
    class Backend:
        def set_status_context(self,handle,context): published.append(context)
    monkeypatch.setattr(terminal,'TmuxTerminalBackend',Backend)
    snapshot={'mission':{'status':'RUNNING'},'tasks':[{'id':7,'status':'RUNNING'}]}
    def wait_and_exit():
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            if any('RUNNING' in item for item in published): return '\x04'
            time.sleep(.005)
        return None
    keys=[lambda:None]*4+[wait_and_exit]*20
    assert run_control(monkeypatch,keys,snapshot=lambda *_args,**_kwargs:snapshot)==0
    assert any('RUNNING' in item for item in published)


def test_status_publication_failure_cannot_break_control_input(monkeypatch):
    attempted=[]
    monkeypatch.setenv('TMUX_PANE','%owned-test')
    def unavailable(_):
        attempted.append(True)
        raise OSError('station vanished')
    monkeypatch.setattr(bridge,'_load_handle',unavailable)
    def exit_after_attempt():
        deadline=time.monotonic()+1
        while not attempted and time.monotonic()<deadline: time.sleep(.005)
        return '\x04'
    assert run_control(monkeypatch,[exit_after_attempt])==0
    assert attempted


def test_unknown_split_escape_sequence_does_not_mutate_retained_draft(monkeypatch):
    observed=[]
    original=tui._ConsoleState
    def state(*args,**kwargs):
        value=original(*args,**kwargs)
        value.command='retained  draft '
        value.command_cursor=3
        value.input_focused=True
        observed.append(value)
        return value
    monkeypatch.setattr(tui,'_ConsoleState',state)
    keys=['\x1b','[','4','2','~','\x15','\x04']
    def check_then_clear():
        assert observed[0].command=='retained  draft '
        return '\x15'
    keys[-2]=check_then_clear
    assert run_control(monkeypatch,keys)==0


@pytest.mark.parametrize('source',['multiline\r\n  中\nTAIL','x'*5000])
def test_complete_document_paste_routes_whole_source_without_dispatch(monkeypatch,source):
    composed=[]
    calls=[]
    monkeypatch.setattr(tui,'_compose_task_prompt',lambda window,state,**kwargs:composed.append(kwargs['initial_text']))
    packet='\x1b[200~'+source+'\x1b[201~'
    assert run_control(monkeypatch,[packet,'\x04'],dispatch=lambda *args:calls.append(args))==0
    assert composed==[source] and not calls


def test_atomic_short_paste_refuses_to_overflow_existing_command(monkeypatch):
    original=tui._ConsoleState
    observed=[]
    def state(*args,**kwargs):
        value=original(*args,**kwargs);value.command='z'*4090;value.command_cursor=4090;value.input_focused=True
        observed.append(value);return value
    monkeypatch.setattr(tui,'_ConsoleState',state)
    def inspect_then_clear():
        assert observed[0].command=='z'*4090
        assert any('previous input is unchanged' in line for line in observed[0].output)
        return '\x15'
    assert run_control(monkeypatch,['\x1b[200~'+'x'*20+'\x1b[201~',inspect_then_clear,'\x04'])==0
