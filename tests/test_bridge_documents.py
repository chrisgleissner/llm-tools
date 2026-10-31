"""Full-source viewer correctness and bounded asynchronous rendering."""
from threading import Event
import time

from llm_tools import bridge_documents as documents
from llm_tools.bridge_input import cell_slice
from tests.conftest import wait_for


def test_large_render_stays_navigable_and_close_releases_worker_result(monkeypatch):
    entered, finish = Event(), Event()
    def slow(source, width, wrap):
        entered.set()
        finish.wait(2)
        return ((("rendered result", "brand"),),)
    monkeypatch.setattr(documents, 'render_markdown_segments', slow)
    viewer = documents.DocumentViewer('Task', 'source\n' * 9000, rendered=True)
    assert 'Rendering Markdown' in viewer.visible(10, 80)[0]
    assert entered.wait(1)
    viewer.end(10)
    assert viewer.visible(10, 80)
    viewer.release()
    finish.set()
    deadline = time.monotonic()+2
    while viewer._rich_pending and time.monotonic()<deadline:
        time.sleep(.005)
    assert not viewer._rich_pending
    assert not viewer.source and not viewer._rich_cache and not viewer._line_cache


def test_source_export_anchor_search_unicode_and_wrapping_are_independent():
    source = '# Heading\n' + '中 e\u0301 🙂  ' * 200 + '\nTAIL_MARKER\n'
    viewer = documents.DocumentViewer('Task revision 2', source)
    viewer.visible(10, 79)
    viewer.search('TAIL_MARKER')
    assert any('TAIL_MARKER' in cell_slice(line, 0, 79) for line in viewer.visible(10, 79))
    viewer.toggle_rendered()
    assert any('TAIL_MARKER' in cell_slice(line, 0, 79) for line in viewer.visible(10, 79))
    viewer.toggle_rendered()
    viewer.resize(60)
    assert any('TAIL_MARKER' in cell_slice(line, 0, 60) for line in viewer.visible(10, 60))
    assert viewer.search_count == 1
    assert viewer.source == source
    viewer.toggle_wrap()
    viewer.home()
    viewer.scroll(1, 1)
    viewer.horizontal = 200
    assert viewer.visible(1, 60)[0] != source.splitlines()[1]
    assert viewer.source == source


def test_malformed_markdown_and_unknown_languages_remain_readable():
    source = '# Heading\n\n> quote\n\n- nested\n  - item\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n[link](https://example.invalid/no-fetch)\n\n```unknown-language\nTAIL_MARKER'
    rows = documents.render_markdown_segments(source, 79)
    text = ''.join(span for row in rows for span, role in row)
    assert 'TAIL_MARKER' in text and 'example.invalid/no-fetch' in text
    assert any(role != 'info' for row in rows for _, role in row)


def test_viewer_controls_search_headings_and_source_export(tmp_path):
    from llm_tools.bridge_tui import _ConsoleState
    source = '# One\n\n' + ('long  中 code\t' * 20) + '\n## Two\nTAIL_MARKER\n'
    state = _ConsoleState('mission', lambda *_: '', lambda _: {}, full=True)
    state.document_viewer = documents.DocumentViewer('Task revision 2', source)
    viewer = state.document_viewer
    viewer.visible(8, 60)
    state.handle_key('h')
    state.handle_key('KEY_DOWN')
    state.handle_key('\n')
    assert viewer.top > 0 and not state.document_heading_list
    state.handle_key('/')
    for char in 'TAIL_MARKER': state.handle_key(char)
    state.handle_key('\n')
    assert viewer.search_count == 1
    assert 'TAIL_MARKER' in ''.join(viewer.visible(8, 60))
    state.handle_key('n')
    state.handle_key('N')
    for key in ('KEY_UP','KEY_DOWN','KEY_PPAGE','KEY_NPAGE','KEY_HOME','KEY_END','m','w','KEY_RIGHT','KEY_LEFT','m','w'):
        state.handle_key(key)
    assert viewer.source == source
    state.handle_key('/')
    state.handle_key('x')
    state.handle_key('\x1b')
    assert viewer.query == 'TAIL_MARKER'
    target = tmp_path / 'prompt with spaces.md'
    for attempt in range(2):
        state.handle_key('s')
        for char in str(target): state.handle_key(char)
        state.handle_key('\n')
        assert target.read_bytes() == source.encode()
        if attempt:
            assert state.document_export_editing
            assert state.document_entry == str(target)
            assert 'exists' in state.document_notice
            state.handle_key('\x1b')
    state.handle_key('q')
    assert state.document_viewer is None and not viewer.source


def test_copy_uses_complete_source_or_explicit_export_fallback(monkeypatch):
    from llm_tools import bridge_tui as tui
    source = 'source\n' * 10000 + 'TAIL\n'
    state = tui._ConsoleState('mission', lambda *_: '', lambda _: {}, full=True)
    state.document_viewer = documents.DocumentViewer('Prompt', source)
    monkeypatch.setattr(tui, '_clipboard_argv', lambda: None)
    state.handle_key('c')
    assert 'export' in state.document_notice
    monkeypatch.setattr(tui, '_clipboard_argv', lambda: ['fake-copy'])
    received = []
    monkeypatch.setattr(tui.subprocess, 'run', lambda argv, **kwargs: received.append((argv, kwargs['input'])))
    state.handle_key('c')
    assert received == [(['fake-copy'], source.encode())]


def test_broken_renderer_falls_back_without_losing_tail(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'rich.console', None)
    rows = documents.render_markdown_segments('# Heading\n\nTAIL\n', 60)
    text = ''.join(text for row in rows for text, role in row)
    assert 'unavailable' in text and 'TAIL' in text


def test_document_surface_places_search_caret_and_retains_export_feedback(monkeypatch):
    from llm_tools import bridge_tui as tui
    class Window:
        rows = {}
        cursor = None
        def getmaxyx(self): return 24, 80
        def erase(self): self.rows = {}
        def addnstr(self, y, x, value, count, *attrs):
            self.rows[y] = self.rows.get(y, '')[:x] + value[:count]
        def move(self, y, x): self.cursor = y, x
        def noutrefresh(self): pass
    visible = []
    monkeypatch.setattr(tui.curses, 'doupdate', lambda: None)
    monkeypatch.setattr(tui.curses, 'curs_set', visible.append)
    state = tui._ConsoleState('mission', lambda *_: '', lambda _: {}, full=True)
    state.document_viewer = documents.DocumentViewer('Task 1 r2 prompt', '# Heading\n\nsource  text\n')
    window = Window()
    tui._draw_document(window, state)
    assert 'read-only' in window.rows[0] and 'source' in window.rows[0]
    assert window.rows[23].startswith('q close') and visible[-1] == 0
    state.document_search_editing = True
    state.document_viewer.query = 'a  b '
    tui._draw_document(window, state)
    assert window.rows[23].startswith('/a  b ')
    assert window.cursor == (23, 6) and visible[-1] == 1
    state.document_search_editing = False
    state.document_heading_list = True
    tui._draw_document(window, state)
    assert 'Heading' in window.rows[1]
    state.document_heading_list = False
    state.document_export_editing = True
    state.document_entry = '/path/' + 'x' * 100
    state.document_notice = 'Export target exists; choose a new path'
    tui._draw_document(window, state)
    assert 'exists' in window.rows[22]
    assert window.cursor[0] == 23 and 0 <= window.cursor[1] < 80
    state.document_export_editing = False
    state.document_notice = ''
    state.document_viewer.rendered = True
    tui._draw_document(window, state)
    assert 'rendered' in window.rows[0]


def test_wrapped_resize_and_horizontal_toggle_keep_the_reading_anchor():
    source='\n'.join(f'line-{i} '+('x'*120)+' ANCHOR-'+str(i)+' '+('y'*120) for i in range(12))
    viewer=documents.DocumentViewer('Prompt',source,wrap=False,top=5,horizontal=120)
    assert 'ANCHOR-5' in viewer.visible(4,60)[0]
    viewer.toggle_wrap()
    assert 'ANCHOR-5' in ''.join(viewer.visible(4,60))
    viewer.resize(80)
    assert 'ANCHOR-5' in ''.join(viewer.visible(4,80))
    viewer.toggle_wrap()
    assert 'ANCHOR-5' in viewer.visible(4,80)[0]
    viewer.home()
    viewer.horizontal=0
    assert viewer.visible(1,80)[0].startswith('line-0')
    viewer.end(3)
    assert any('line-11' in row for row in viewer.visible(3,80))


def test_casefold_global_search_repeats_and_reverse_keep_original_unicode_offsets():
    source='Straße FIRST\n'+('irrelevant\n'*500)+'Straße SECOND\nTAIL'
    viewer=documents.DocumentViewer('Prompt',source)
    viewer.visible(5,60)
    viewer.search('STRASSE')
    assert viewer.visible(5,60)[0].startswith('Straße FIRST')
    viewer.search('STRASSE')
    assert viewer.visible(5,60)[0].startswith('Straße SECOND')
    viewer.search('STRASSE',reverse=True)
    assert viewer.visible(5,60)[0].startswith('Straße FIRST')
    assert viewer.search('not present') is None
    assert viewer.search('') is None and viewer.search_count==0
    viewer.rendered=True
    viewer.visible(5,60)  # starts the background Markdown render of this 500-line source
    wait_for(lambda: not viewer.rendering, 30, 0.01, message='the Markdown render did not finish')
    viewer.search('SECOND')
    assert 'SECOND' in ''.join(viewer.visible(5,60))
    viewer.search('SECOND',reverse=True)
    assert 'SECOND' in ''.join(viewer.visible(5,60))


def test_large_renderer_completes_once_and_reuses_cached_pages(monkeypatch):
    ready=Event(); calls=[]
    def render(source,width,wrap):
        calls.append((width,wrap))
        ready.set()
        return tuple(((f'row {i} TAIL' if i==99 else f'row {i}', 'info'),) for i in range(100))
    monkeypatch.setattr(documents,'render_markdown_segments',render)
    viewer=documents.DocumentViewer('Prompt','source '*9000,rendered=True)
    viewer.visible(8,80)
    assert ready.wait(1)
    deadline=time.monotonic()+2
    while viewer._rich_pending and time.monotonic()<deadline: time.sleep(.005)
    for _ in range(10): viewer.page(1,8);viewer.visible(8,80)
    viewer.end(8)
    assert 'TAIL' in ''.join(viewer.visible(8,80))
    assert calls==[(80,True)]
    viewer.release()
    assert not viewer.source
