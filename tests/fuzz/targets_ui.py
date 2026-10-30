"""Fuzz targets for the curses screens: control console, document viewer, home screen, mission picker."""

from __future__ import annotations

import copy
import curses
import io
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable

from llm_tools import bridge_launcher, bridge_switcher, bridge_tui, bridge_viewer
from llm_tools.bridge_documents import DocumentViewer
from llm_tools.bridge_input import normalize_paste_newlines

from . import generators as gen
from .harness import FakeClock, FakeWindow, FuzzFailure, Patcher, Sandbox, StreamExhausted, fake_curses, note, quiet_stdout

CI_CASES: dict[str, int] = {}

ENTER_KEYS = {"\n", "\r", "KEY_ENTER", curses.KEY_ENTER}


def _ui_case(cases: int) -> Callable[[Callable[..., None]], Callable[..., None]]:
    def register(function: Callable[..., None]) -> Callable[..., None]:
        CI_CASES[function.__name__] = cases
        return function

    return register


# ------------------------------------------------------------------ audit helpers

class Audit:
    """Records every command the console dispatches and which key caused it."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.ctx: dict[str, Any] = {"key": None, "paste": False, "confirm": None, "mouse": False}
        self.dispatches: list[str] = []  # commands that reached _ConsoleState._dispatch
        self.executed: list[str] = []  # commands that reached the injected dispatch function
        self.violations: list[str] = []
        self.keys = 0

    # -- the injected dispatch function ---------------------------------------
    def dispatch(self, mission_id: str, command: str) -> str:
        self.executed.append(command)
        if command.startswith("task show"):
            return '{"prompt": "full prompt text", "revision": 2, "pending": {}}'
        kind = self.rng.random()
        if kind < 0.4:
            return '{"status": "APPLIED", "result": {"task_id": 1}}'
        if kind < 0.55:
            return "error: refused"
        if kind < 0.65:
            return '{"status": "REJECTED", "result": {"error": "no"}}'
        if kind < 0.75:
            return "\n".join(gen.plain_text(self.rng, 30) for _ in range(self.rng.randint(2, 60)))
        if kind < 0.8:
            return gen.chaotic_text(self.rng, 80)
        return "ok"

    # -- the dispatch audit ---------------------------------------------------
    def check(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        self.dispatches.append(text)
        key, ctx = self.ctx["key"], self.ctx
        label = f"{text!r} dispatched by key {key!r}"
        if key == "composer":
            window = ctx["window"]
            last = getattr(window, "last", None)
            if isinstance(last, gen.PastedStr) or last not in {"\x13", curses.KEY_F2}:
                self.violations.append(f"{label}: the composer saved after {last!r}, not an explicit Ctrl-S/F2")
        elif ctx["paste"]:
            self.violations.append(f"{label} that came from paste content")
        elif isinstance(key, str) and key.lower() == "y" and ctx["confirm"] is not None:
            if line != ctx["confirm"]:
                self.violations.append(f"{label}: confirmation was for {ctx['confirm']!r}")
        elif key in ENTER_KEYS:
            reason = bridge_tui._confirmation_reason(bridge_tui._strip_slash(text))
            if reason:
                self.violations.append(f"{label} without the confirmation ({reason})")
        else:
            self.violations.append(f"{label}: only Enter or a confirming Y may dispatch a command")

    def finish(self) -> None:
        if self.violations:
            raise FuzzFailure("; ".join(self.violations[:3]))
        unexplained = [c for c in self.executed if not c.startswith("task show")]
        if len(unexplained) > len(self.dispatches):
            raise FuzzFailure(f"a command reached the dispatcher without going through _dispatch: {unexplained[len(self.dispatches):][:3]}")


def instrument(patch: Patcher, audit: Audit) -> None:
    """Wrap handle_key/_dispatch so each dispatch knows which key caused it."""
    original_key = bridge_tui._ConsoleState.handle_key
    original_dispatch = bridge_tui._ConsoleState._dispatch

    def handle_key(self: bridge_tui._ConsoleState, key: object) -> bool:
        audit.ctx = {"key": key, "paste": isinstance(key, gen.PastedStr), "confirm": self.confirm_command}
        audit.keys += 1
        return original_key(self, key)

    def _dispatch(self: bridge_tui._ConsoleState, line: str, **kwargs: Any) -> bool:
        audit.check(line)
        return original_dispatch(self, line, **kwargs)

    patch.setattr(bridge_tui._ConsoleState, "handle_key", handle_key)
    patch.setattr(bridge_tui._ConsoleState, "_dispatch", _dispatch)

    original_compose = bridge_tui._compose_task_prompt

    def compose(window: Any, state: bridge_tui._ConsoleState, **kwargs: Any) -> None:
        saved = audit.ctx
        audit.ctx = {"key": "composer", "paste": False, "confirm": None, "window": window}
        try:
            return original_compose(window, state, **kwargs)
        finally:
            audit.ctx = saved

    patch.setattr(bridge_tui, "_compose_task_prompt", compose)


def screen_patches(patch: Patcher, window: FakeWindow | None = None) -> None:
    """Silence the pieces of the curses screens that touch a real terminal."""
    patch.setattr(bridge_tui, "sync_window_size", lambda _window: None)
    patch.setattr(bridge_tui, "initialize_colors", lambda *a, **k: False)
    patch.setattr(bridge_launcher, "_stdin_pending", lambda: False)
    patch.setattr(bridge_launcher, "_set_bracketed_paste", lambda _enabled: None)


def settle_snapshot(state: bridge_tui._ConsoleState) -> None:
    state.refresh(force=True)
    thread = state._snapshot_thread
    if thread is not None:
        thread.join(10)
    state.refresh()


def settle_command(state: bridge_tui._ConsoleState) -> None:
    deadline = time.monotonic() + 5
    while state.command_pending and time.monotonic() < deadline:
        state.refresh()
        time.sleep(0.0005)
    if state.command_pending:
        raise FuzzFailure("a dispatched command never completed")
    # let a document / objective read thread land
    state.refresh()


def build_console(rng: random.Random, audit: Audit, *, full: bool, snapshot: dict[str, Any] | None = None) -> tuple[bridge_tui._ConsoleState, dict[str, Any]]:
    holder: dict[str, Any] = {"snapshot": gen.random_snapshot(rng) if snapshot is None else snapshot}

    def reader(mission_id: str, **_kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(holder["snapshot"])

    state = bridge_tui._ConsoleState("M1", audit.dispatch, reader, full=full)
    settle_snapshot(state)
    return state, holder


def draw_state(state: bridge_tui._ConsoleState, height: int, width: int, *, full_mode: bool = True) -> FakeWindow:
    """Draw the screen the way ``_run`` would for the current state and return the window."""
    window = FakeWindow(height, width)
    state.full = full_mode or height > 8
    if state.response_overlay:
        bridge_tui._draw_response_overlay(window, state)
    elif state.document_viewer is not None:
        bridge_tui._draw_document(window, state)
    elif state.full:
        bridge_tui._draw_full(window, state)
    else:
        bridge_tui._draw_compact(window, state)
    window.check(f"draw (view={state.view}, {height}x{width}, overlay={state.response_overlay}, document={state.document_viewer is not None})")
    return window


def check_state(state: bridge_tui._ConsoleState) -> None:
    if not 0 <= state.command_cursor <= len(state.command):
        raise FuzzFailure(f"command caret {state.command_cursor} outside the {len(state.command)}-character command line")
    if not 0 <= state.view_index < len(bridge_tui.VIEWS):
        raise FuzzFailure(f"view index {state.view_index} out of range")
    for name in ("selected", "task_offset", "event_offset", "detail_scroll", "response_scroll"):
        if getattr(state, name) < 0:
            raise FuzzFailure(f"{name} is negative: {getattr(state, name)}")


# ------------------------------------------------------------------ console: keys

@_ui_case(40)
def target_console_keys(rng: random.Random, sb: Sandbox) -> None:
    """Random key streams, mouse and snapshots drive the console state; every frame stays in its window; only Enter/confirmed Y dispatches."""
    audit = Audit(rng)
    full_mode = rng.random() < 0.7
    with Patcher() as patch:
        instrument(patch, audit)
        with fake_curses(patch):
            screen_patches(patch)
            state, holder = build_console(rng, audit, full=full_mode)
            for step in range(rng.randint(5, 100)):
                action = rng.random()
                if action < 0.80:
                    key = gen.semantic_key(rng)
                    note("key", key)
                    alive = state.handle_key(key)
                    if alive is not True and alive is not False:
                        raise FuzzFailure(f"handle_key({key!r}) returned {alive!r}")
                    settle_command(state)
                elif action < 0.84:
                    holder["snapshot"] = gen.random_snapshot(rng)
                    settle_snapshot(state)
                elif action < 0.88:
                    height, width = gen.terminal_size(rng)
                    state.handle_mouse(rng.randint(-2, height + 2), rng.randint(-2, width + 2), height, width)
                    bridge_tui._handle_mouse_event(state, rng.randint(0, height), rng.randint(0, width), height, width, activate=rng.random() < 0.5)
                    settle_command(state)
                elif action < 0.92:
                    state.wheel(rng.choice([-1, 1]))
                    state.hscroll(rng.choice([-1, 1]))
                elif action < 0.94:
                    state.choose_view(rng.randint(-3, 15))
                elif action < 0.96:
                    state.open_help(rng.choice(["console", "view:Tasks", "view:" + gen.plain_text(rng, 5), gen.plain_text(rng, 8), "quickstart"]))
                else:
                    state.dismiss_status()
                    state.status_message()
                    state.rows()
                check_state(state)
                if rng.random() < 0.25:
                    height, width = gen.terminal_size(rng)
                    draw_state(state, height, width, full_mode=full_mode)
            for height, width in gen.resize_storm(rng, 6):
                draw_state(state, height, width, full_mode=full_mode)
            audit.finish()


# ------------------------------------------------------------------ console: draw

@_ui_case(40)
def target_console_draw(rng: random.Random, sb: Sandbox) -> None:
    """Every view, overlay and the document viewer draw inside the window at any size for any snapshot."""
    audit = Audit(rng)
    with Patcher() as patch:
        instrument(patch, audit)
        with fake_curses(patch):
            screen_patches(patch)
            state, _holder = build_console(rng, audit, full=True)
            for _ in range(rng.randint(3, 8)):
                height, width = gen.terminal_size(rng)
                state.view_index = rng.randrange(len(bridge_tui.VIEWS))
                state.selected = rng.choice([0, 0, 1, 5, 40, 400])
                state.task_detail = rng.random() < 0.3
                state.detail_scroll = rng.choice([0, 0, 3, 99])
                state.usage_hscroll = rng.choice([0, 0, 5, 500])
                state.event_offset = rng.choice([0, 0, 20, 400])
                state.response_overlay = False
                state.document_viewer = None
                state.confirm_command = None
                state.search_query = rng.choice(["", "", gen.plain_text(rng, 6)])
                state.command = rng.choice(["", "", "status", gen.plain_text(rng, 90)])
                state.command_cursor = rng.randint(0, len(state.command))
                state.input_focused = rng.random() < 0.4
                state.palette_filter_editing = rng.random() < 0.1
                state.palette_filter = rng.choice(["", "pa", gen.plain_text(rng, 5)])
                if rng.random() < 0.3:
                    state.output.extend(gen.chaotic_text(rng, 40) for _ in range(rng.randint(1, 30)))
                if rng.random() < 0.2:
                    state.snapshot_error = gen.plain_text(rng, 100)
                overlay = rng.random()
                if overlay < 0.2:
                    state.response_overlay = True
                    state.response_scroll = rng.choice([0, 3, 999])
                    if rng.random() < 0.5:
                        state.confirm_command = rng.choice(["cancel", "task verify --task 1 --approve", "finish", "task reject --task 2 --reason=" + gen.plain_text(rng, 30)])
                elif overlay < 0.4:
                    source = gen.markdownish(rng) if rng.random() < 0.5 else gen.valid_text(rng, 100)
                    state.document_viewer = DocumentViewer("doc " + gen.plain_text(rng, 8), source)
                    state.document_viewer.rendered = rng.random() < 0.4
                    state.document_viewer.wrap = rng.random() < 0.6
                    state.document_heading_list = rng.random() < 0.2
                    state.document_search_editing = rng.random() < 0.2
                    state.document_export_editing = rng.random() < 0.1 and not state.document_search_editing
                    state.document_entry = gen.plain_text(rng, 20)
                    state.document_notice = rng.choice(["", gen.plain_text(rng, 60)])
                    if rng.random() < 0.5:
                        state.document_viewer.search(gen.plain_text(rng, 3))
                note("draw", (height, width, state.view, state.response_overlay, state.document_viewer is not None))
                draw_state(state, height, width, full_mode=rng.random() < 0.7)
                state.row_segments()
                state.rows()
            audit.finish()


# ------------------------------------------------------------------ console: whole loop

def _install_loop_fakes(patch: Patcher, window: FakeWindow, clock: FakeClock) -> None:
    patch.setattr(curses, "wrapper", lambda callback, *a, **k: callback(window))
    patch.setattr(bridge_tui, "time", clock.module())
    patch.setattr(bridge_launcher, "time", clock.module())
    screen_patches(patch)


@_ui_case(30)
def target_console_loop(rng: random.Random, sb: Sandbox) -> None:
    """The real control-console loop (paste decoder, escape handling, mouse, resize) over a hostile key stream."""
    audit = Audit(rng)
    clock = FakeClock()
    height, width = gen.terminal_size(rng)
    stream = gen.wch_stream(rng, rng.choice([30, 80, 200]))
    window = FakeWindow(height, width, feed=stream, clock=clock)
    holder = {"snapshot": gen.random_snapshot(rng)}

    def reader(mission_id: str, **_kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(holder["snapshot"])

    with Patcher() as patch:
        with fake_curses(patch, window=window):
            _install_loop_fakes(patch, window, clock)
            instrument(patch, audit)
            run = bridge_tui.run_control if rng.random() < 0.7 else bridge_tui.run_console
            with quiet_stdout():
                try:
                    run("M1", audit.dispatch, reader)
                except StreamExhausted:
                    pass
            window.check("console loop")
            audit.finish()


@_ui_case(25)
def target_viewer_loop(rng: random.Random, sb: Sandbox) -> None:
    """The standalone document viewer loop survives arbitrary keys, mouse events and resizes."""
    clock = FakeClock()
    height, width = gen.terminal_size(rng)
    source = gen.markdownish(rng) if rng.random() < 0.5 else gen.valid_text(rng, 150)
    window = FakeWindow(height, width, feed=gen.wch_stream(rng, rng.choice([30, 100])), clock=clock)
    with Patcher() as patch:
        with fake_curses(patch, window=window):
            _install_loop_fakes(patch, window, clock)
            patch.setattr(bridge_viewer, "time", clock.module())
            patch.setattr(bridge_tui.curses, "keyname", lambda n: str(n).encode() if rng.random() < 0.9 else b"\xff")
            with quiet_stdout():
                try:
                    bridge_viewer.run_document(source, title="fuzz " + gen.plain_text(rng, 8))
                except StreamExhausted:
                    pass
            window.check("document viewer loop")


# ------------------------------------------------------------------ launcher

def _mission(rng: random.Random, number: int) -> dict[str, Any]:
    status = rng.choice(["CREATED", "RUNNING", "PAUSED", "STOPPING", "STOPPED", "COMPLETED", "FAILED", "CANCELLED", "weird", ""])
    return {
        "id": "".join(rng.choice("0123456789abcdef") for _ in range(32)), "name": gen.plain_text(rng, 40), "objective": gen.valid_text(rng, 60),
        "cwd": rng.choice(["/", "/tmp", "/home/user/dev/" + gen.plain_text(rng, 30), gen.plain_text(rng, 80), ""]), "status": status,
        "created_at": rng.choice(["2026-10-30T06:55:35+00:00", "", "garbage"]), "updated_at": rng.choice(["2026-10-30T07:55:35+00:00", None, "x"]),
        "config": rng.choice([{}, {"providers": "codex,claude"}, {"routes": "kilo-zai,codex", "colour": 3}, {"providers": gen.plain_text(rng, 30)}, "notadict", None]),
    }


def build_launcher(rng: random.Random, sb: Sandbox, *, burst: float = 0.0) -> bridge_launcher.Launcher:
    missions = [_mission(rng, number) for number in range(rng.choice([0, 0, 1, 3, 12, 60]))]
    stats = {m["id"]: bridge_launcher.MissionStats(rng.randint(0, 5), rng.randint(0, 9), rng.choice(["", "codex", gen.plain_text(rng, 8)]), rng.choice([None, 2])) for m in missions if rng.random() < 0.8}
    installed = tuple(rng.sample(["codex", "claude", "copilot", "kilo", "opencode"], rng.randint(0, 4)))
    routes = tuple(bridge_launcher.RouteChoice(f"route{n}", rng.choice(installed or ("codex",)), gen.plain_text(rng, 10)) for n in range(rng.randint(0, 3))) if rng.random() < 0.4 else ()
    state = bridge_launcher.Launcher(
        str(sb.work), missions=missions, stats=stats, installed=installed, routes=routes, directory=rng.choice(["", str(sb.work), gen.plain_text(rng, 40), "/nonexistent"]),
        burst_window=burst, probe=lambda _mission_id: rng.choice([True, False, None]),
    )
    return state


_LAUNCHER_ACTIONS = {None, "quit", "help", "open", "attach", "start-selected", "create", "start", "compose-objective"}


def _launcher_key(rng: random.Random) -> object:
    roll = rng.random()
    if roll < 0.45:
        return rng.choice("nNqQjksSoOx/?\x06 abc123")
    if roll < 0.60:
        return rng.choice(gen._CTRL)
    if roll < 0.75:
        return rng.choice([curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_HOME, curses.KEY_END, curses.KEY_BTAB, curses.KEY_BACKSPACE, curses.KEY_ENTER, curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_RESIZE, curses.KEY_DC])
    if roll < 0.85:
        return rng.choice(["\n", "\r", "\t", "\x1b"])
    if roll < 0.95:
        return gen.plain_text(rng, 3) or "é"
    return rng.choice(gen._CURSES_INTS)


@_ui_case(120)
def target_launcher_keys(rng: random.Random, sb: Sandbox) -> None:
    """Home screen keys, paste, click and wheel: no exception, valid actions only, bursts and pastes never create or start."""
    clock = FakeClock()
    burst = rng.choice([0.0, 0.5, 2.0])
    with Patcher() as patch:
        with fake_curses(patch):
            screen_patches(patch)
            patch.setattr(bridge_launcher, "time", clock.module())
            state = build_launcher(rng, sb, burst=burst)
            last_plain_key = None  # clock reading of the latest non-Enter key
            for _ in range(rng.randint(5, 120)):
                action = rng.random()
                result: str | None = None
                deliberate = False
                now = clock.module().monotonic()
                if action < 0.70:
                    key = _launcher_key(rng)
                    note("key", key)
                    enter = key in {"\n", "\r", curses.KEY_ENTER}
                    result = state.key(key)
                    if not enter:
                        last_plain_key = now
                elif action < 0.78:
                    payload = gen.valid_text(rng, 60)
                    result = state.paste(payload)
                    if result not in {None, "compose-objective"}:
                        raise FuzzFailure(f"paste returned {result!r}")
                    if result == "compose-objective" and payload not in state.objective:
                        raise FuzzFailure("accepted paste is missing from the objective")
                elif action < 0.84:
                    height, width = gen.terminal_size(rng)
                    state.wheel(rng.choice([-1, 1]), y=rng.choice([None, rng.randint(-3, height + 3)]), height=height)
                elif action < 0.90:
                    height, width = gen.terminal_size(rng)
                    result = state.click(rng.randint(-3, height + 3), height=height, activate=rng.random() < 0.5, x=rng.choice([None, rng.randint(-3, width + 3)]), toggle=rng.random() < 0.5)
                    deliberate = True
                elif action < 0.95:
                    clock.advance(rng.choice([0.01, 0.6, 3.0]))
                else:
                    height, width = gen.terminal_size(rng)
                    window = FakeWindow(height, width)
                    bridge_launcher._draw(window, state)
                    window.check(f"launcher draw {height}x{width} creating={state.creating} focus={state.focus}")
                if result not in _LAUNCHER_ACTIONS:
                    raise FuzzFailure(f"unknown action {result!r}")
                if result in {"create", "start"} and not deliberate and burst > 0 and last_plain_key is not None and clock.module().monotonic() - last_plain_key < burst:
                    raise FuzzFailure(f"{result!r} returned inside a typing burst ({clock.module().monotonic() - last_plain_key:.2f}s after a key, window {burst}s)")
                if not 0 <= state.selected <= len(state.view()):
                    raise FuzzFailure(f"selection {state.selected} outside the list of {len(state.view())}")
                if not 0 <= state.focus <= 5:
                    raise FuzzFailure(f"focus {state.focus} outside the form")
            for height, width in gen.resize_storm(rng, 6):
                window = FakeWindow(height, width)
                bridge_launcher._draw(window, state)
                window.check(f"launcher draw {height}x{width} creating={state.creating} focus={state.focus}")


@_ui_case(40)
def target_launcher_loop(rng: random.Random, sb: Sandbox) -> None:
    """The real home-screen loop over a hostile key stream (escape sequences, pastes, mouse, resize) never raises or starts work."""
    clock = FakeClock()
    height, width = rng.choice([(24, 100), (40, 120), gen.terminal_size(rng)])
    feed: list[Any] = [("resize", height, width)]
    feed.extend(gen.wch_stream(rng, rng.choice([30, 80, 200])))
    window = FakeWindow(height, width, feed=feed, clock=clock)
    composed: list[str] = []

    def compose(initial: str = "", **_kwargs: Any) -> str | None:
        composed.append(initial)
        return None

    dispatched: list[list[str]] = []

    def dispatch(args: list[str]) -> int:
        dispatched.append(args)
        return 1  # as if the command failed: the screen must show it and carry on

    with Patcher() as patch:
        with fake_curses(patch, window=window):
            _install_loop_fakes(patch, window, clock)
            patch.setattr(bridge_tui, "compose_document", compose)
            with quiet_stdout():
                try:
                    bridge_launcher.run_launcher(dispatch)
                except StreamExhausted:
                    pass
    window.check("launcher loop")
    for args in dispatched:
        if args and args[0] in {"start", "attach"} and not any(isinstance(item, tuple) for item in ()):
            # Starting provider work is allowed only through an explicit key on a mission; the stream has no saved mission.
            raise FuzzFailure(f"the home screen started or attached work without a saved mission: {args}")


@_ui_case(40)
def target_launcher_paste(rng: random.Random, sb: Sandbox) -> None:
    """A bracketed paste in the new-mission form arrives in the composer exactly (CR normalised to LF)."""
    clock = FakeClock()
    payload = gen.valid_text(rng, 100).replace("\x1b[201~", "")
    if not payload:
        payload = "x"
    start, end = gen.PASTE_START.decode(), gen.PASTE_END.decode()
    block = start + payload + end
    pieces = [block] if rng.random() < 0.25 else list(block)
    prefix = list("n") + ([("pause", 0.2)] if rng.random() < 0.5 else [])
    window = FakeWindow(24, 100, feed=[*prefix, *(gen.PastedStr(p) for p in pieces), ("pause", 0.3), ("pause", 3.0)], clock=clock)
    composed: list[str] = []

    def compose(initial: str = "", **_kwargs: Any) -> str | None:
        composed.append(initial)
        return None

    with Patcher() as patch:
        with fake_curses(patch, window=window):
            _install_loop_fakes(patch, window, clock)
            patch.setattr(bridge_tui, "compose_document", compose)
            with quiet_stdout():
                try:
                    bridge_launcher.run_launcher(lambda args: 1)
                except StreamExhausted:
                    pass
    expected = normalize_paste_newlines(payload)
    if composed != [expected]:
        raise FuzzFailure(f"paste did not reach the composer verbatim: got {composed!r}")


# ------------------------------------------------------------------ mission picker

def _entry(rng: random.Random) -> bridge_switcher.MissionEntry:
    return bridge_switcher.MissionEntry(
        mission_id="".join(rng.choice("0123456789abcdef") for _ in range(rng.choice([32, 32, 3, 0]))), name=gen.plain_text(rng, 40),
        status=rng.choice(["CREATED", "RUNNING", "PAUSED", "STOPPING", "STOPPED", "COMPLETED", "FAILED", "CANCELLED", "weird", "", "running"]),
        cwd=rng.choice(["/", "/tmp", "/home/user/dev/" + gen.plain_text(rng, 30), gen.plain_text(rng, 80), ""]), provider=rng.choice(["-", "codex", gen.plain_text(rng, 12)]),
        task_id=rng.choice([None, 1, 99999]), task_ordinal=rng.choice([None, 3]), task_status=rng.choice(["", "RUNNING", gen.plain_text(rng, 8)]), task_title=gen.plain_text(rng, 40),
        station=rng.random() < 0.4, controller=rng.random() < 0.4, updated_at=rng.choice(["", "2026-10-30", gen.plain_text(rng, 10)]), rotation=rng.choice(["", "codex,claude", gen.plain_text(rng, 30)]),
        colour=rng.choice([None, 3, "x", 99]), review_ordinal=rng.choice([None, 2]))


@_ui_case(80)
def target_picker(rng: random.Random, sb: Sandbox) -> None:
    """Mission picker keys, mouse and drawing at any size; a station is closed only after Y."""
    closed: list[str] = []
    entries = [_entry(rng) for _ in range(rng.choice([0, 1, 5, 40]))]
    picker = bridge_switcher.MissionPicker(entries, current=rng.choice([None, entries[0].mission_id if entries else None]), unicode=rng.random() < 0.7,
                                           closer=lambda entry: closed.append(entry.mission_id) or "closed")
    with Patcher() as patch:
        with fake_curses(patch):
            screen_patches(patch)
            confirmed = 0
            for _ in range(rng.randint(5, 80)):
                action = rng.random()
                if action < 0.7:
                    key = gen.semantic_key(rng) if rng.random() < 0.6 else rng.choice(gen._CURSES_INTS)
                    note("key", key)
                    was_confirming = picker.confirming is not None
                    count_before = len(closed)
                    result = picker.key(key)
                    if result not in {None, "select", "cancel"}:
                        raise FuzzFailure(f"picker returned {result!r}")
                    if len(closed) != count_before and not (was_confirming and key in {"y", "Y"}):
                        raise FuzzFailure(f"a station was closed by key {key!r} without a confirming Y")
                    if result == "select" and picker.chosen is None:
                        raise FuzzFailure("select without a chosen entry")
                elif action < 0.8:
                    picker.wheel(rng.choice([-1, 1]))
                elif action < 0.9:
                    picker.click(rng.randint(-3, 40), double=rng.random() < 0.5)
                    bridge_switcher.mouse_outcome(picker, rng.randint(-3, 40), rng.choice([0, 1, 2, 4, 8, 0x10000, 0x200000, 1 << 30, getattr(curses, "BUTTON1_CLICKED", 4)]))
                else:
                    height, width = gen.terminal_size(rng)
                    window = FakeWindow(height, width)
                    for y, (segments, style) in enumerate(picker.segment_lines(height, width)):
                        if style == "sel":
                            bridge_tui._put(window, y, 0, "".join(text for text, _ in segments), width, role="selected")
                        else:
                            bridge_tui._put_segments(window, y, 0, segments, width)
                    window.check(f"picker draw {height}x{width}")
                    if len(picker.segment_lines(height, width)) > max(0, height):
                        raise FuzzFailure(f"picker returned more than {height} rows")
                if picker.visible() and not 0 <= picker.selected < len(picker.visible()) and picker.selected != 0:
                    raise FuzzFailure(f"picker selection {picker.selected} outside {len(picker.visible())} rows")
            picker.plain_text(rng.choice([1, 20, 80, 200]))
            for entry in entries:
                entry.as_json()
            bridge_switcher.format_missions(entries, width=rng.choice([10, 80, 300]))
            bridge_switcher.missions_json(entries)
