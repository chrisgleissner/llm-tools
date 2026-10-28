"""Interactive entry point for choosing or creating a durable mission."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import curses
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable

from .bridge_store import BridgeStore, default_state_dir
from .common import KNOWN_PROVIDERS
from .bridge_input import cell_width
from .bridge_input import BracketedPaste

_MAX_OBJECTIVE_BYTES = 1_048_576


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
    native: bool = True
    error: str = ""
    cursors: list[int] = field(default_factory=lambda: [0, 0, 0])
    recovery_draft: Path | None = None
    recovery_source: str | None = None
    paste_decoder: BracketedPaste = field(default_factory=BracketedPaste, repr=False)
    escape_pending: bool = False
    escape_at: float = 0.0

    def validate(self) -> None:
        if not self.objective.strip():
            raise ValueError("Enter a mission objective.")
        if len(self.objective.encode("utf-8")) > _MAX_OBJECTIVE_BYTES:
            raise ValueError("Objective exceeds the interactive 1 MiB UTF-8 limit; import a file or use an editor.")
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
            if key == "?":
                return "help"
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
        elif key == "\x05" and self.focus == 0:
            return "compose-objective"
        elif key in {"\t", curses.KEY_DOWN}:
            self.focus = min(5, self.focus + 1)
            if self.focus < 3:
                name = ("objective", "providers", "directory")[self.focus]
                self.cursors[self.focus] = len(str(getattr(self, name)))
        elif key in {curses.KEY_BTAB, curses.KEY_UP}:
            self.focus = max(0, self.focus - 1)
            if self.focus < 3:
                name = ("objective", "providers", "directory")[self.focus]
                self.cursors[self.focus] = len(str(getattr(self, name)))
        elif self.focus == 3 and key in {" ", curses.KEY_LEFT, curses.KEY_RIGHT}:
            self.native = not self.native
        elif key in {"\n", "\r", curses.KEY_ENTER}:
            if self.focus < 4:
                self.focus += 1
            elif self.focus == 4:
                try:
                    self.validate()
                    return "start"
                except (ValueError, OSError, RuntimeError) as exc:
                    self.error = str(exc)
            else:
                try:
                    self.validate()
                    return "create"
                except (ValueError, OSError, RuntimeError) as exc:
                    self.error = str(exc)
        elif self.focus < 3:
            name = ("objective", "providers", "directory")[self.focus]
            value = str(getattr(self, name))
            cursor = min(len(value), self.cursors[self.focus])
            if key in {curses.KEY_BACKSPACE, "\x7f", "\b"}:
                if cursor:
                    value = value[:cursor - 1] + value[cursor:]
                    cursor -= 1
                    setattr(self, name, value)
            elif key in {curses.KEY_LEFT}:
                cursor = max(0, cursor - 1)
            elif key in {curses.KEY_RIGHT}:
                cursor = min(len(value), cursor + 1)
            elif key in {curses.KEY_HOME, "\x01"}:
                cursor = 0
            elif key in {curses.KEY_END, "\x05"}:
                cursor = len(value)
            elif key == "\x15":
                setattr(self, name, "")
                cursor = 0
            elif isinstance(key, str) and len(key) > 1:
                # curses may return a complete bracketed paste as one value.
                # Treat its newlines as objective source, never form navigation.
                if self.focus == 0:
                    candidate = value[:cursor] + key + value[cursor:]
                    if len(candidate.encode("utf-8")) <= _MAX_OBJECTIVE_BYTES:
                        setattr(self, name, candidate)
                        cursor += len(key)
                    else:
                        self.error = f"Paste rejected ({len(key.encode('utf-8'))} bytes); interactive limit is 1 MiB. Use --file or an editor."
                    return "compose-objective"
            elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                setattr(self, name, value[:cursor] + key + value[cursor:])
                cursor += 1
            self.cursors[self.focus] = cursor
        return None

    def paste(self, payload: str) -> str | None:
        if not self.creating or self.focus != 0:
            self.error = "Paste targets the focused Objective field. Select it and paste again."
            return None
        cursor = min(len(self.objective), self.cursors[0])
        candidate = self.objective[:cursor] + payload + self.objective[cursor:]
        if len(candidate.encode("utf-8")) > _MAX_OBJECTIVE_BYTES:
            self.error = f"Paste rejected: received {len(payload.encode('utf-8'))} bytes; limit is 1 MiB. Previous objective is unchanged."
            return None
        self.objective = candidate
        self.cursors[0] = cursor + len(payload)
        self.error = ""
        return "compose-objective"

    def click(self, y: int, *, height: int = 24, activate: bool = False) -> str | None:
        if self.creating:
            if 7 <= y <= 17 and (y - 7) % 2 == 0:
                self.focus = (y - 7) // 2
                if self.focus in {4, 5}:
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


def _discard_incomplete_escape(state: Launcher) -> bool:
    """Reset a timed-out non-paste escape prefix so later input stays usable."""
    decoder = state.paste_decoder
    if decoder.waiting and not decoder.active and not state.escape_pending:
        decoder.reset()
        state.error = "Unknown or incomplete escape sequence ignored."
        return True
    return False


def _draw(window: curses.window, state: Launcher) -> None:
    from .bridge_tui import role_attr, _text, _put, sync_window_size
    from .bridge_input import input_view

    sync_window_size(window)
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
        field_carets: list[int] = []
        def field(label: str, value: str) -> str:
            preview = value.replace("\n", " ↵ ")
            index = len(field_carets)
            cursor = min(len(value), state.cursors[index])
            insertion = len(value[:cursor].replace("\n", " ↵ "))
            visible, caret = input_view(preview, insertion, max(1, width-cell_width(label)-2))
            field_carets.append(2+cell_width(label)+caret)
            return label + visible

        fields = (
            field("Objective: ", state.objective),
            field("Providers: ", state.providers),
            field("Folder: ", state.directory or state.cwd),
            "Mode: " + ("Native interactive — full provider terminal stays primary" if state.native else "Autonomous — Bridge runs headless provider work"),
            "Start and open provider" if state.native else "Start headless work",
            "Save mission only",
        )
        for index, text in enumerate(fields):
            focused = index == state.focus
            _put(window, 7+index*2, 0, ("> " if focused else "  ")+text, width,
                 role="selected" if focused else "info", preserve_spacing=True)
        size = f"{len(state.objective.encode('utf-8'))} bytes · {state.objective.count(chr(10)) + 1} lines"
        line(height - 3, state.error or (size if state.objective else "Objective: Enter text; Ctrl-E opens the full editor."), "error" if state.error else "dim")
        line(height - 1, "Tab next · Space mode · Ctrl-E compose · Enter action · Esc back", "dim")
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
    if state.creating and state.focus < 3:
        try:
            curses.curs_set(1)
            window.move(7 + state.focus * 2, min(width-1, field_carets[state.focus]))
        except (curses.error, AttributeError):
            pass
    else:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
    window.refresh()


def run_launcher(dispatch: Callable[[list[str]], int]) -> int:
    """Open the home screen without launching a provider or inferring a session."""
    from .bridge_tui import initialize_colors, sync_window_size
    curses.use_env(False)

    with BridgeStore(default_state_dir() / "bridge.sqlite3") as store:
        missions = store.list_missions()
    cwd = str(Path.cwd().resolve())
    state = Launcher(cwd, missions=list(reversed(missions)), directory=cwd)

    def loop(window: curses.window) -> str:
        window.keypad(True)
        try:
            curses.nonl()
        except curses.error:
            pass
        initialize_colors()
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        try:
            window.timeout(30)
        except AttributeError:
            pass
        while True:
            if not state.paste_decoder.active:
                _draw(window, state)
            try:
                key = window.get_wch()
            except curses.error:
                _discard_incomplete_escape(state)
                if state.escape_pending and time.monotonic() - state.escape_at >= 0.06:
                    state.escape_pending = False
                    if state.paste_decoder.active:
                        state.paste_decoder.reset()
                        state.error = "Incomplete paste discarded; objective unchanged."
                    else:
                        action = state.key("\x1b")
                        if action:
                            return action
                continue
            sync_window_size(window)
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
                if key == "\x1b" and not state.paste_decoder.active:
                    state.escape_pending = True
                    state.escape_at = time.monotonic()
                    continue
                if state.escape_pending:
                    state.paste_decoder.feed(b"\x1b")
                    state.escape_pending = False
                if state.paste_decoder.active or state.paste_decoder.waiting or (isinstance(key, str) and key.startswith(("[200~", "\x1b[200~"))):
                    pasted = state.paste_decoder.feed(key.encode("utf-8") if isinstance(key, str) else b"")
                    if pasted is not None:
                        action = state.paste(pasted)
                    elif state.paste_decoder.done and state.paste_decoder.error:
                        state.error = state.paste_decoder.error + "; objective unchanged"
                        state.paste_decoder.reset()
                        action = None
                    else:
                        action = None
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
        if action == "help":
            from .bridge_help import topic_text
            from .bridge_viewer import run_document
            run_document(topic_text("quickstart"), title="Bridge help · quickstart")
            continue
        if action == "compose-objective":
            from . import bridge_tui

            initial = state.objective
            if state.recovery_draft is not None and state.recovery_draft.is_file():
                with state.recovery_draft.open(encoding="utf-8", newline="") as stream:
                    initial = stream.read()
            result = bridge_tui.compose_document(initial, title="New mission objective")
            if result is not None:
                state.objective = result
                state.recovery_source = result if state.recovery_draft is not None else None
                state.cursors[0] = len(result)
                state.error = ""
            elif bridge_tui.LAST_RECOVERY_DRAFT is not None:
                state.recovery_draft = bridge_tui.LAST_RECOVERY_DRAFT
                state.error = f"Objective draft retained privately: {bridge_tui.LAST_RECOVERY_DRAFT}"
            continue
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
            if state.recovery_draft is None:
                draft_dir = default_state_dir() / "drafts"
                draft_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(draft_dir, 0o700)
                fd, name = tempfile.mkstemp(prefix="objective-", suffix=".md", dir=draft_dir)
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                    stream.write(state.objective)
                os.chmod(name, 0o600)
                state.recovery_draft = Path(name)
                state.recovery_source = state.objective
            state.error += f" Source retained at {state.recovery_draft}."
            continue
        mission_id = str(json.loads(output.getvalue())["mission"]["id"])
        if state.recovery_draft is not None and state.recovery_draft.is_file():
            try:
                if state.recovery_draft.read_bytes() == state.objective.encode("utf-8"):
                    state.recovery_draft.unlink()
            except OSError:
                pass
        if action != "start":
            break
        started = dispatch(["start", "--mission", mission_id, *( ["--native"] if state.native else ["--headless"] )])
        if started:
            state.error = errors.getvalue().strip() or "Mission was saved but could not start. Use start from its controls to retry."
            continue
        if state.native:
            # start attaches to the validated owned provider station. Once the
            # operator returns with Ctrl-] B/P or detaches, reopen controls.
            dispatch(["control", "--mission", mission_id])
            return 0
        break
    return dispatch(["control", "--mission", mission_id])
