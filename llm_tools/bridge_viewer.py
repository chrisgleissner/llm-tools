"""Standalone Bridge documents and explicit local text-artifact following."""
from __future__ import annotations

import curses
from pathlib import Path
import time


def read_source(path: Path) -> str:
    with path.open(encoding='utf-8', newline='') as stream:
        return stream.read()


def signature(path: Path) -> tuple[int, int, int, int]:
    info=path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def run_document(source: str, *, title: str, path: Path | None = None, follow: bool = False) -> int:
    """Read-only document ownership; opening never starts mission/provider work."""
    from .bridge_documents import DocumentViewer
    from .bridge_tui import _ConsoleState, _draw_document, initialize_colors
    curses.use_env(False)
    seen=signature(path) if path is not None else None
    if path is not None:
        source=read_source(path)
    state=_ConsoleState('document', lambda *_: '', lambda _: {}, full=True)
    state.document_viewer=DocumentViewer(title, source)
    following=follow
    def loop(window: curses.window) -> int:
        nonlocal following, seen
        initialize_colors()
        window.keypad(True)
        window.timeout(100)
        checked=0.0
        if following:
            state.document_viewer.end(max(1,window.getmaxyx()[0]-2))
        while state.document_viewer is not None:
            viewer=state.document_viewer
            now=time.monotonic()
            if path is not None and now-checked>=.25:
                checked=now
                try:
                    observed=signature(path)
                    if observed!=seen:
                        if following and viewer.rendering:
                            state.document_notice='Following · rendering; newer output pending'
                        elif following:
                            current=read_source(path)
                            rotated=seen is not None and (observed[:2]!=seen[:2] or observed[2]<seen[2])
                            replacement=DocumentViewer(title,current,rendered=viewer.rendered,wrap=viewer.wrap)
                            viewer.release()
                            state.document_viewer=viewer=replacement
                            viewer.end(max(1,window.getmaxyx()[0]-2))
                            seen=observed
                            state.document_notice='Following · file rotated/truncated; older content unavailable' if rotated else 'Following · Up pauses, End resumes'
                        elif follow:
                            state.document_notice='Paused · new output available · End resumes'
                        else:
                            state.document_notice='File changed · close and reopen to refresh; source snapshot retained'
                    elif follow:
                        state.document_notice='Following · Up pauses' if following else 'Paused · End resumes'
                except (OSError,UnicodeError) as exc:
                    state.document_notice=f'File unavailable; retained source · {exc}'
            _draw_document(window,state)
            try:
                key=window.get_wch()
            except curses.error:
                continue
            if key == '\x1b':
                window.timeout(25)
                try:
                    extra=window.get_wch()
                    if extra in {'[','O'}:
                        for _ in range(32):
                            part=window.get_wch()
                            if isinstance(part,str) and len(part)==1 and '@'<=part<='~':
                                break
                        continue
                    curses.unget_wch(extra)
                except curses.error:
                    pass
                finally:
                    window.timeout(100)
            if isinstance(key,int):
                try: key=curses.keyname(key).decode('ascii')
                except (ValueError,UnicodeError): continue
            if follow and not state.document_search_editing and not state.document_export_editing:
                if key in {'KEY_UP','KEY_PPAGE','KEY_HOME','k','g','/'}:
                    following=False
                    state.document_notice='Paused · End resumes'
                elif key in {'KEY_END','G'}:
                    following=True
                    checked=0.0
            state.handle_key(key)
        return 0
    try:
        return curses.wrapper(loop)
    finally:
        state.close_document()
