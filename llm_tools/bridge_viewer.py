"""Standalone Bridge documents and explicit local text-artifact following."""
from __future__ import annotations

import curses
import errno
from pathlib import Path
import time

from . import bridge_hangup
from .bridge_mouse import enable_mouse, read_mouse, wheel_delta
from .fileio import open_regular_file

# Lines scrolled per vertical wheel notch; horizontal notches use the 8-column
# step of the Left/Right keys.
WHEEL_LINES = 3


MAX_SOURCE_BYTES = 64 * 1024 * 1024


def read_source(path: Path) -> str:
    """The complete text of a regular file of at most ``MAX_SOURCE_BYTES``.

    A FIFO, device or directory is refused instead of being read, so ``view``
    on one cannot hang or exhaust memory.
    """
    try:
        stream = open_regular_file(path)
    except OSError as exc:
        if exc.errno == errno.EINVAL:
            raise ValueError(f"not a regular file: {path}") from exc
        raise
    with stream:
        data = stream.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f"file is larger than {MAX_SOURCE_BYTES // (1024 * 1024)} MiB: {path}")
    return data.decode('utf-8')


def signature(path: Path) -> tuple[int, int, int, int]:
    info=path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def wheel_keys(buttons: int, *, wrap: bool, editing: bool) -> list[str]:
    """Key names equivalent to a wheel event, so scrolling shares the key handlers.

    Vertical notches scroll ``WHEEL_LINES`` lines (one entry in the heading
    list, which selects rather than scrolls). Horizontal notches (Shift+wheel)
    pan only when wrapping is off, and nothing happens while a search or
    export path is being typed.
    """
    wheel=wheel_delta(buttons)
    if wheel is None or editing:
        return []
    vertical,horizontal=wheel
    if vertical:
        return ['KEY_UP' if vertical<0 else 'KEY_DOWN']*WHEEL_LINES
    if horizontal and not wrap:
        return ['KEY_LEFT' if horizontal<0 else 'KEY_RIGHT']
    return []


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
        bridge_hangup.set_timeout(window, 100)
        enable_mouse()
        checked=0.0
        if following:
            state.document_viewer.end(max(1,window.getmaxyx()[0]-2))  # type: ignore[union-attr]  # set before this loop
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
                key=bridge_hangup.read_wch(window)
            except curses.error:
                continue
            if key == curses.KEY_MOUSE:
                event=read_mouse()
                viewer=state.document_viewer
                if event is None or viewer is None:
                    continue
                editing=state.document_search_editing or state.document_export_editing
                keys=wheel_keys(event[2],wrap=viewer.wrap,editing=editing)
                if state.document_heading_list:
                    keys=keys[:1]
            else:
                keys=None
            if keys is not None:
                for name in keys:
                    if follow and name=='KEY_UP':
                        following=False
                        state.document_notice='Paused · End resumes'
                    state.handle_key(name)
                continue
            if key == '\x1b':
                bridge_hangup.set_timeout(window, 25)
                try:
                    extra=bridge_hangup.read_wch(window)
                    if extra in {'[','O'}:
                        for _ in range(32):
                            part=bridge_hangup.read_wch(window)
                            if isinstance(part,str) and len(part)==1 and '@'<=part<='~':
                                break
                        continue
                    curses.unget_wch(extra)
                except curses.error:
                    pass
                finally:
                    bridge_hangup.set_timeout(window, 100)
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
        return bridge_hangup.run_curses(loop)
    except bridge_hangup.TerminalGone:
        return bridge_hangup.abandon_terminal()
    finally:
        state.close_document()
