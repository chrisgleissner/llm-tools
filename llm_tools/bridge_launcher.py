"""Interactive entry point for choosing or creating a durable mission."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import curses
from dataclasses import dataclass, field
import io
import json
from pathlib import Path
import sys
from typing import Any, Callable

from .bridge_store import BridgeStore, default_state_dir
from .common import KNOWN_PROVIDERS


@dataclass
class Launcher:
    cwd: str
    missions: list[dict[str, Any]] = field(default_factory=list)
    selected: int = 0
    creating: bool = False
    focus: int = 0
    objective: str = ""
    providers: str = "codex"
    directory: str = ""
    native: bool = False
    error: str = ""

    def validate(self) -> None:
        if not self.objective.strip():
            raise ValueError("Enter a mission objective.")
        providers = [item.strip() for item in self.providers.split(",") if item.strip()]
        if not providers or any(item not in KNOWN_PROVIDERS - {"zai"} for item in providers):
            raise ValueError("Choose provider CLIs such as codex, claude or copilot.")
        directory = Path(self.directory or self.cwd).expanduser().resolve(strict=True)
        if not directory.is_dir():
            raise ValueError("The working directory must be a folder.")
        self.directory = str(directory)
        self.providers = ",".join(providers)

    def key(self, key: object) -> str | None:
        """Return an explicit action, leaving persistence outside curses."""
        if key in {"\x03", "\x04"}:
            return "quit"
        if not self.creating:
            if key in {"q", "Q", "\x1b"}:
                return "quit"
            if key in {"n", "N"}:
                self.creating = True
            elif key in {curses.KEY_DOWN, "j", "\t"}:
                self.selected = min(len(self.missions), self.selected + 1)
            elif key in {curses.KEY_UP, "k"}:
                self.selected = max(0, self.selected - 1)
            elif key in {"\n", "\r", curses.KEY_ENTER}:
                if self.selected:
                    return "open"
                self.creating = True
            return None
        if key == "\x1b":
            self.creating = False
            self.error = ""
        elif key in {"\t", curses.KEY_DOWN}:
            self.focus = min(4, self.focus + 1)
        elif key in {curses.KEY_BTAB, curses.KEY_UP}:
            self.focus = max(0, self.focus - 1)
        elif self.focus == 3 and key in {" ", curses.KEY_LEFT, curses.KEY_RIGHT}:
            self.native = not self.native
        elif key in {"\n", "\r", curses.KEY_ENTER}:
            if self.focus < 4:
                self.focus += 1
            else:
                try:
                    self.validate()
                    return "create"
                except (ValueError, OSError, RuntimeError) as exc:
                    self.error = str(exc)
        elif self.focus < 3:
            name = ("objective", "providers", "directory")[self.focus]
            value = str(getattr(self, name))
            if key in {curses.KEY_BACKSPACE, "\x7f", "\b"}:
                setattr(self, name, value[:-1])
            elif key == "\x15":
                setattr(self, name, "")
            elif isinstance(key, str) and key.isprintable() and len(value) < 16000:
                setattr(self, name, value + key)
        return None

    def click(self, y: int, *, height: int = 24, activate: bool = False) -> str | None:
        if self.creating:
            if 7 <= y <= 15 and (y - 7) % 2 == 0:
                self.focus = (y - 7) // 2
                if self.focus == 4:
                    return self.key("\n")
        else:
            visible = max(1, height - 10)
            start = max(0, self.selected - visible + 1)
            count = min(visible, len(self.missions) + 1 - start)
            if 7 <= y < 7 + count:
                self.selected = start + y - 7
                if activate:
                    return self.key("\n")
        return None


def _draw(window: curses.window, state: Launcher) -> None:
    from .bridge_tui import role_attr, _text

    height, width = window.getmaxyx()
    window.erase()

    def line(y: int, text: str, role: str = "info") -> None:
        if 0 <= y < height:
            try:
                window.addnstr(y, 0, _text(text, max(0, width - 1)), max(0, width - 1), role_attr(role))
            except curses.error:
                pass

    line(0, "LLM BRIDGE", "brand")
    if height < 20 or width < 60:
        line(2, "Enlarge the terminal to at least 60 columns and 20 rows.", "warn")
        line(height - 1, "Q / Ctrl-D quit", "dim")
        window.refresh()
        return
    line(1, "Durable missions and native provider terminals", "dim")
    line(3, f"Working directory: {state.cwd}", "heading")
    if state.creating:
        line(5, "New mission", "heading")
        def field(label: str, value: str) -> str:
            return label + value[-max(1, width - len(label) - 4):]

        fields = (
            field("Objective: ", state.objective),
            field("Providers: ", state.providers),
            field("Folder: ", state.directory or state.cwd),
            "Mode: " + ("Native supervised" if state.native else "Autonomous"),
            "Create mission and open controls",
        )
        for index, text in enumerate(fields):
            focused = index == state.focus
            line(7 + index * 2, ("> " if focused else "  ") + text, "selected" if focused else "info")
        line(height - 3, state.error or "Creation saves the mission; use start in its controls when ready.", "error" if state.error else "dim")
        line(height - 1, "Tab/Enter next  Space change mode  Ctrl-U clear field  Esc back  Ctrl-D quit", "dim")
    else:
        line(5, "Choose a mission", "heading")
        choices = ["New mission in this folder", *[
            f"{mission['name']}  [{mission['status']}]  {mission['cwd']}" for mission in state.missions
        ]]
        visible = max(1, height - 10)
        start = max(0, state.selected - visible + 1)
        for offset, text in enumerate(choices[start:start + visible]):
            selected = start + offset == state.selected
            line(7 + offset, ("> " if selected else "  ") + text, "selected" if selected else "info")
        if not state.missions:
            line(height - 3, "No missions yet. Create one to add your first task.", "dim")
        line(height - 1, "Up/Down or click select  Enter/double-click open  N new  Q quit", "dim")
    window.refresh()


def run_launcher(dispatch: Callable[[list[str]], int]) -> int:
    """Open the home screen without launching a provider or inferring a session."""
    from .bridge_tui import initialize_colors

    with BridgeStore(default_state_dir() / "bridge.sqlite3") as store:
        missions = store.list_missions()
    cwd = str(Path.cwd().resolve())
    state = Launcher(cwd, missions=list(reversed(missions)), directory=cwd)

    def loop(window: curses.window) -> str:
        window.keypad(True)
        initialize_colors()
        try:
            curses.curs_set(0)
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        while True:
            _draw(window, state)
            key = window.get_wch()
            height, width = window.getmaxyx()
            if height < 20 or width < 60:
                if key in {"q", "Q", "\x04", "\x03"}:
                    return "quit"
                continue
            if key == curses.KEY_MOUSE:
                try:
                    _, _, y, _, buttons = curses.getmouse()
                    left_click = curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED | curses.BUTTON1_DOUBLE_CLICKED
                    action = state.click(y, height=height, activate=bool(buttons & curses.BUTTON1_DOUBLE_CLICKED)) if buttons & left_click else None
                except curses.error:
                    continue
            else:
                action = state.key(key)
            if action:
                return action

    while True:
        try:
            action = curses.wrapper(loop)
        except (KeyboardInterrupt, EOFError):
            return 0
        except curses.error as exc:
            print(f"llm-bridge: terminal unavailable ({exc}); use llm-bridge --help for command-line options", file=sys.stderr)
            return 2
        if action == "quit":
            return 0
        if action == "open":
            mission_id = str(state.missions[state.selected - 1]["id"])
            break
        args = ["mission", "create", "--prompt=" + state.objective, "--providers", state.providers,
                "--cwd", state.directory, "--json"]
        if state.native:
            args.append("--native")
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = dispatch(args)
        if status:
            state.error = errors.getvalue().strip() or output.getvalue().strip() or "Mission creation failed."
            continue
        mission_id = str(json.loads(output.getvalue())["mission"]["id"])
        break
    return dispatch(["control", "--mission", mission_id])
