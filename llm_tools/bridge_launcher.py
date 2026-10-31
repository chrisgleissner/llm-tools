"""Interactive entry point for choosing or creating a durable mission."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import curses
from dataclasses import dataclass, field
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import select
import shutil
import sqlite3
import sys
import tempfile
import textwrap
import time
from typing import Any, Callable

from . import bridge_hangup, bridge_redraw
from .bridge_store import BridgeStore, default_state_dir, store_path
from .bridge_store import controller_running as _controller_running
from .common import KNOWN_PROVIDERS
from .bridge_format import printable
from .bridge_input import cell_slice, cell_width, without_flow_control
from .bridge_input import BracketedPaste
from .bridge_mouse import enable_mouse, read_mouse, wheel_delta
from .bridge_glyphs import (  # noqa: F401 - several names are re-exported
    FINISHED_STATES, GLYPHS, _locale_codeset, ascii_mode, glyph, localize, provider_role, state_look, state_text,
)
from .bridge_theme import mission_role
from .bridge_states import CONTROLLED_MISSION_STATUSES, FINISHED_MISSION_STATUSES

_MAX_OBJECTIVE_BYTES = 1_048_576
# Primary signal for an unbracketed paste is input already queued behind an
# Enter. This short gap is the fallback: pasted keys arrive within a few
# milliseconds, while even fast typists leave 60 ms or more before Enter.
_BURST_SECONDS = 0.03
_BURST_HINT = "Press Enter again to "

# Declared (display) order of the provider CLIs a mission may use. ``zai`` is a
# capacity-only provider and has no launch binary of its own.
PROVIDER_ORDER: tuple[str, ...] = ("codex", "claude", "copilot", "kilo", "opencode", "minimax")
_PROVIDER_BINARIES = {"minimax": "mmx"}
_TERMINAL_STATUSES = FINISHED_MISSION_STATUSES
_LIVE_STATUSES = CONTROLLED_MISSION_STATUSES
_STATUS_WIDTH = 9
_STATE_CELLS = _STATUS_WIDTH + 2  # state glyph, a space, then the status word
_CHIP_CELLS = 2  # mission chip and a space
_FOLDER_MARK = 2  # "here" marker and a space in front of the folder
_AGE_WIDTH = 8
_ID_WIDTH = 8  # the short mission id
_MIN_COLUMNS, _MIN_ROWS = 60, 20
_PROVIDERS_LABEL = "Providers: "
_STALE_MARKER = "stale"
_STALE_WIDTH = len(_STALE_MARKER) + 1
# How long one controller-liveness probe is reused. The probe takes a lock
# briefly, so it must not run on every draw; a visible live mission is redrawn
# at this interval so a controller that stopped shows as stale.
_PROBE_TTL = 2.0
# How long the first frame waits for the mission list before it shows
# "Loading missions" and lets keys through.
_FIRST_LOAD_WAIT = 0.1
# A pending lone Esc is taken as a key after this long; an unfinished escape
# sequence is dropped after the same short gap a key loop would have polled.
_ESCAPE_SECONDS = 0.06
_SEQUENCE_SECONDS = 0.03
_PASTE_STALL_SECONDS = 2.0
_EMPTY_STATE = ("A mission is a goal plus ordered tasks run by Codex/Claude in this folder"
                " \u00b7 N to create one")
_SUBTITLE = "Run AI coding agents on your tasks \u00b7 switch as limits run out"
_DISPLAY_NAMES = {"codex": "Codex", "claude": "Claude Code", "copilot": "Copilot", "kilo": "Kilo", "opencode": "OpenCode",
                  "minimax": "MiniMax"}
# Screen rows: 0 title and folder, 1 subtitle, 3 section heading, 4 column
# header (list only), body from 5. The footer rows count from the bottom.
_HEADING_Y = 3
_BODY_Y = 5
# The list is re-read only after another connection committed: the probe runs
# every _RELOAD_FAST seconds while the operator is active and
# every _RELOAD_SLOW seconds after _RELOAD_IDLE_AFTER idle seconds.
_RELOAD_FAST = 0.25
_RELOAD_SLOW = 1.0
_RELOAD_IDLE_AFTER = 10.0
_NO_TASKS = "no tasks"
_ESCDELAY_MS = 50
_THEME_HINT_GRACE = 1.0  # terminal start-up replies arriving as keys must not dismiss the note
_FILTER_HINT_MIN = 5  # the footer advertises / filter once the list is longer than this
_COLUMN_WIDTH_MAX = 12
_NAME_WIDTH_MIN = 12
_FOLDER_WIDTH_MIN = 14
_GROUP_LIVE, _GROUP_WAITING, _GROUP_FINISHED = 0, 1, 2


@dataclass(frozen=True)
class RouteChoice:
    """A declared ``[routes.<id>]`` launch path offered next to bare providers."""

    route_id: str
    provider: str
    model: str | None = None

    @property
    def detail(self) -> str:
        return f"{self.provider} {glyph('sep')} {self.model}" if self.model else self.provider


def provider_binary(name: str) -> str:
    """Return the executable a provider launches (``minimax`` runs ``mmx``)."""
    return _PROVIDER_BINARIES.get(name, name)


def provider_installed(name: str) -> bool:
    return shutil.which(provider_binary(name)) is not None


def disabled_launch_clis() -> frozenset[str]:
    """Launch CLIs that `[usage].providers` / `LLM_USAGE_PROVIDERS` disable (empty when all are enabled)."""
    try:
        from .bridge_rotation import current_enabled

        enabled = current_enabled().names
    except (SystemExit, Exception):  # a broken config must never block the home screen
        return frozenset()
    return frozenset() if enabled is None else frozenset(PROVIDER_ORDER) - enabled


def theme_guess_hint() -> str:
    """Footer note shown once when the terminal background could not be detected."""
    try:
        from . import bridge_theme

        resolved = bridge_theme.resolve_cached()
    except Exception:  # advisory only
        return ""
    if not resolved.guessed:
        return ""
    return f"theme: {resolved.polarity} (guessed) {glyph('sep')} set LLM_BRIDGE_THEME=light|dark"


def detect_installed() -> tuple[str, ...]:
    """Provider CLIs found on PATH, in declared order."""
    return tuple(name for name in PROVIDER_ORDER if provider_installed(name))


def subtitle(installed: tuple[str, ...]) -> str:
    """The home screen subtitle, naming only the provider CLIs that are installed and enabled."""
    names = [_DISPLAY_NAMES.get(name, name) for name in installed]
    if not names:
        return _SUBTITLE
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    text = f"Run {listed} on your tasks"
    return text + (f" {glyph('sep')} switch as limits run out" if len(names) > 1 else "")


def default_providers(installed: tuple[str, ...], config: dict[str, Any] | None = None,
                      routes: tuple[RouteChoice, ...] = ()) -> str:
    """Pick the initial provider selection.

    Preference: the available members of the user's configured rotation
    (``[ralph].routes``, then ``[ralph].providers``, then
    ``[defaults].providers``), else the first installed provider that is also
    signed in (claude, codex, opencode, copilot, kilo), else ``codex`` when
    installed, else the first installed provider. With nothing installed the
    selection stays ``codex`` and validation explains that no CLI was found.
    """
    route_ids = {choice.route_id for choice in routes}
    for table, key in (("ralph", "routes"), ("ralph", "providers"), ("defaults", "providers")):
        section = (config or {}).get(table)
        names = section.get(key) if isinstance(section, dict) else None
        if isinstance(names, list):
            chosen = [str(n) for n in names if str(n) in installed or (key == "routes" and str(n) in route_ids)]
            if chosen:
                return ",".join(dict.fromkeys(chosen))
    from . import bridge_doctor

    signed_in = bridge_doctor.preferred_first_provider(installed)
    if signed_in is not None:
        return signed_in
    if "codex" in installed or not installed:
        return "codex"
    return installed[0]


def _load_config() -> dict[str, Any]:
    try:
        from . import config as config_module
        return config_module.load_config()
    except (SystemExit, Exception):  # a broken config must never block the home screen
        return {}


def detect_routes(config: dict[str, Any] | None) -> tuple[RouteChoice, ...]:
    """Declared routes whose launch CLI is installed, in declaration order.

    A route that shares its id with a bare provider is skipped: the bare name
    already resolves to that declared route.
    """
    try:
        from . import config as config_module
        declared = config_module.parse_routes(config or {})
    except Exception:  # a malformed table must never block the home screen
        return ()
    choices = []
    for route_id, policy in declared.items():
        if route_id in PROVIDER_ORDER or policy.provider not in PROVIDER_ORDER or not provider_installed(policy.provider):
            continue
        choices.append(RouteChoice(str(route_id), str(policy.provider), policy.model or None))
    return tuple(choices)


def controller_running(mission_id: str) -> bool | None:
    """Whether a controller holds the mission lock; ``None`` when unknown.

    A shared, non-blocking probe never creates the lock file, and concurrent
    probes (another launcher, ``status``) cannot mask each other.
    """
    return _controller_running(mission_id, on_error=None)


def _flat(text: object) -> str:
    """Single-line, control-free text that keeps ordinary spacing."""
    return localize(printable(str(text).expandtabs(8)))


def _clean(text: object) -> str:
    return " ".join(_flat(text).split())


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - cell_width(text))


def truncate(text: str, width: int) -> str:
    """Clip to ``width`` terminal cells, marking the cut with an ellipsis."""
    text = _clean(text)
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    mark = glyph("ellipsis")
    keep = width - cell_width(mark)
    return cell_slice(text, 0, keep) + mark if keep > 0 else cell_slice(mark, 0, width)


def middle_ellipsis(text: str, width: int) -> str:
    """Clip the middle of ``text`` so both its start and its end stay visible."""
    text = _clean(text)
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    mark = glyph("ellipsis")
    room = width - cell_width(mark)
    if room <= 0:
        return cell_slice(mark, 0, width)
    head = room * 2 // 5
    tail_room = room - head
    tail: list[str] = []
    used = 0
    for char in reversed(text):
        amount = cell_width(char)
        if used + amount > tail_room:
            break
        tail.append(char)
        used += amount
    while tail and cell_width(tail[-1]) == 0:
        tail.pop()
    return cell_slice(text, 0, head) + mark + "".join(reversed(tail))


def short_path(path: str, width: int, home: str | None = None) -> str:
    """Show ``~/…`` for the home folder and a middle ellipsis when too long."""
    home = home if home is not None else os.path.expanduser("~")
    shown = str(path)
    base = home.rstrip(os.sep)
    if base:
        if shown == home or shown == base:
            shown = "~"
        elif shown.startswith(base + os.sep):
            shown = "~" + shown[len(base):]
    return middle_ellipsis(shown, width)


def relative_age(stamp: object, now: float | None = None) -> str:
    """Compact age such as ``2m ago``; ``-`` when the timestamp is unusable."""
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        seconds = int((time.time() if now is None else now) - moment.timestamp())
    except (ValueError, OSError, OverflowError):
        return "-"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 86400 * 365:
        return f"{seconds // 86400}d ago"
    return f"{seconds // (86400 * 365)}y ago"


def age_changes_in(stamp: object, now: float) -> float | None:
    """Seconds until :func:`relative_age` of ``stamp`` shows different text; ``None`` for an unusable stamp."""
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        elapsed = now - moment.timestamp()
    except (ValueError, OSError, OverflowError):
        return None
    if elapsed < 60:
        return 60 - elapsed + 0.01
    year = 86400 * 365
    unit = 60 if elapsed < 3600 else 3600 if elapsed < 86400 else 86400 if elapsed < year else year
    return unit - elapsed % unit + 0.01


def status_group(status: object) -> int:
    """0 running/paused/stopping, 1 waiting (created, stopped, failed), 2 finished."""
    value = str(status)
    if value in _LIVE_STATUSES:
        return _GROUP_LIVE
    return _GROUP_FINISHED if value in _TERMINAL_STATUSES else _GROUP_WAITING


def order_missions(missions: list[dict[str, Any]], cwd: str) -> list[dict[str, Any]]:
    """Order the home list.

    Live missions come first, then waiting ones (created, stopped, failed),
    then finished ones (completed, cancelled). Inside each group, missions in
    the current folder come first. Remaining ties are broken by most recent
    update.
    """
    newest = sorted(missions, key=lambda m: str(m.get("updated_at") or m.get("created_at") or ""), reverse=True)
    return sorted(newest, key=lambda m: (status_group(m.get("status")), str(m.get("cwd")) != cwd))


@dataclass(frozen=True)
class MissionStats:
    """Cheap per-mission facts shown in the home list."""

    done: int = 0
    total: int = 0
    route: str = ""  # provider or route of the mission's latest operation
    review: int | None = None  # number of the lowest-numbered task awaiting the operator's review


def progress_text(stats: MissionStats | None) -> str:
    """``2/5 done``; ``no tasks`` for an empty mission; ``-`` when unknown."""
    if stats is None:
        return "-"
    return f"{stats.done}/{stats.total} done" if stats.total else _NO_TASKS


def mission_matches(mission: dict[str, Any], needle: str) -> bool:
    """Case-insensitive match on name or folder text, or on the start of the id."""
    needle = needle.strip().lower()
    if not needle:
        return True
    cwd = str(mission.get("cwd", ""))
    haystacks = (str(mission.get("name", "")), cwd, short_path(cwd, 4096))
    return any(needle in text.lower() for text in haystacks) or str(mission.get("id", "")).lower().startswith(needle)


def objective_summary(objective: str, room: int) -> str:
    """First meaningful line (heading markers removed) plus ``(+N lines)``, clipped to ``room`` cells."""
    lines = objective.split("\n")
    first = next((index for index, text in enumerate(lines) if text.strip()), None)
    if first is None:
        return ""
    head = re.sub(r"^\s*#{1,6}\s+", "", lines[first]).strip() or lines[first].strip()
    more = sum(1 for text in lines[first + 1:] if text.strip())
    suffix = f" (+{more} line{'s' if more != 1 else ''})" if more else ""
    return truncate(head, max(1, room - cell_width(suffix))) + suffix


def status_role(status: object) -> str:
    """Theme role of a mission status word (its state colour)."""
    look = state_look(status)
    return look[1] if look else "info"


def list_rows(height: int) -> int:
    """Rows available to the mission list (including the new-mission entry)."""
    return max(1, height - _BODY_Y - 4)


def window_start(selected: int, visible: int) -> int:
    """First visible list row: the selection stays on screen."""
    return max(0, selected - visible + 1)


def mission_providers(mission: dict[str, Any]) -> str:
    """The mission's rotation (routes when it uses them, else providers)."""
    config = mission.get("config")
    if not isinstance(config, dict):
        return "-"
    value = config.get("routes") or config.get("providers")
    names = [item.strip() for item in str(value or "").split(",") if item.strip()]
    return f" {glyph('arrow')} ".join(names) if names else "-"


def mission_detail(mission: dict[str, Any], width: int) -> str:
    """Facts that the list row may have truncated; only the folder is ellipsized."""
    tail = localize(f" · {str(mission.get('id', ''))[:8]} · {mission_providers(mission)}")
    return short_path(str(mission.get("cwd", "")), max(8, width - cell_width(tail))) + tail


@dataclass
class FormLayout:
    """Screen rows of the new-mission form."""

    ys: list[int]                      # row of each of the six fields
    vertical: bool                     # providers/routes shown as a checklist under the label
    items: list[tuple[int, int]]       # (choice index, row) of each visible checklist item
    total: int = 0                     # number of choices in the checklist


@dataclass
class Launcher:
    cwd: str
    missions: list[dict[str, Any]] = field(default_factory=list)
    selected: int = 0
    creating: bool = False
    focus: int = 0
    objective: str = ""
    # Selected providers, comma separated, in rotation order (the order in
    # which they were toggled on).
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
    paste_activity: float = 0.0
    paste_interrupted: bool = False
    # Provider CLIs found on PATH; ``None`` means "detect on first use".
    installed: tuple[str, ...] | None = None
    # Declared routes whose launch CLI is installed; offered after the providers.
    routes: tuple[RouteChoice, ...] = ()
    provider_cursor: int = 0
    # Set by the last draw: whether the picker is a vertical checklist, and the
    # terminal width it was laid out for.
    vertical: bool | None = None
    width: int = 0
    probe: Callable[[str], bool | None] = field(default=controller_running, repr=False)
    probe_ttl: float = _PROBE_TTL
    # Burst guard for terminals without bracketed paste. ``burst_window`` is
    # zero for a bare ``Launcher`` (direct key calls) and set by ``run_launcher``.
    burst_window: float = 0.0
    last_input: float = -1e9
    input_pending: Callable[[], bool] = field(default=lambda: False, repr=False)
    _probes: dict[str, tuple[float, bool | None]] = field(default_factory=dict, repr=False)
    # Task counts and active provider per mission id, loaded with the list.
    stats: dict[str, MissionStats] = field(default_factory=dict)
    # Type-to-filter text; ``filtering`` is true while typed characters edit it.
    filter_text: str = ""
    filtering: bool = False
    # One-time footer note when the terminal background was guessed; cleared by the first key.
    theme_hint: str = ""
    theme_hint_since: float = field(default_factory=time.monotonic)
    # Provider launch CLIs the llm-tools config disables; they are not offered.
    disabled: tuple[str, ...] = ()
    # Confirmation of the last save, shown while that mission stays selected.
    notice: str = ""
    notice_id: str | None = None
    # Reads the list on a worker thread when the database changed; ``loading``
    # is true until the first list has arrived.
    reloader: bridge_redraw.BackgroundReload | None = field(default=None, repr=False)
    loading: bool = False
    _view_cache: tuple[Any, ...] | None = field(default=None, repr=False)
    _columns_cache: tuple[Any, ...] | None = field(default=None, repr=False)

    def view(self) -> list[dict[str, Any]]:
        """The missions shown in the list: all of them, or the ones matching the filter."""
        if not self.filter_text.strip():
            return self.missions
        key = (len(self.missions), self.filter_text)
        cached = self._view_cache
        if cached is not None and cached[0] == key and cached[1] is self.missions:
            return cached[2]
        shown = [m for m in self.missions if mission_matches(m, self.filter_text)]
        self._view_cache = (key, self.missions, shown)
        return shown

    def columns(self, width: int, extra: int) -> "ListColumns":
        """The list column plan for the whole list; reused until the list, its stats or the width change."""
        key = (len(self.missions), len(self.stats), width, extra)
        cached = self._columns_cache
        if cached is not None and cached[0] == key and cached[1] is self.missions and cached[2] is self.stats:
            return cached[3]
        plan = plan_columns(self.missions, self.stats, width, extra)
        self._columns_cache = (key, self.missions, self.stats, plan)
        return plan

    def selected_id(self) -> str | None:
        shown = self.view()
        return str(shown[self.selected - 1].get("id")) if 1 <= self.selected <= len(shown) else None

    def select_id(self, ident: str | None) -> None:
        """Select the mission ``ident`` if it is shown, else clamp to the list."""
        shown = self.view()
        ids = [str(m.get("id")) for m in shown]
        if ident is not None and ident in ids:
            self.selected = ids.index(ident) + 1
        else:
            self.selected = min(self.selected, len(shown))

    def reload(self, missions: list[dict[str, Any]], stats: dict[str, MissionStats]) -> None:
        """Replace the list contents; the selection follows the same mission."""
        ident = self.selected_id()
        self.missions, self.stats = missions, stats
        self.loading = False
        self.select_id(ident)

    def _filter_changed(self) -> None:
        self.selected = 1 if self.view() else 0

    def clear_filter(self) -> None:
        ident = self.selected_id()
        self.filter_text, self.filtering = "", False
        self.select_id(ident)

    def _filter_key(self, key: object) -> str | None:
        """Keys while the filter line is being edited."""
        shown = self.view()
        if key == "\x1b":
            self.clear_filter()
        elif key in {"\n", "\r", curses.KEY_ENTER}:
            self.filtering = False
            if self.selected:
                return self.enter_action()
        elif key in {curses.KEY_DOWN, "\t"}:
            self.selected = min(len(shown), self.selected + 1)
        elif key == curses.KEY_UP:
            self.selected = max(0, self.selected - 1)
        elif key in {curses.KEY_BACKSPACE, "\x7f", "\b"}:
            if self.filter_text:
                self.filter_text = self.filter_text[:-1]
                self._filter_changed()
            else:
                self.filtering = False
        elif key == "\x15":
            self.filter_text = ""
            self._filter_changed()
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.filter_text += key
            self._filter_changed()
        return None

    def controller_state(self, mission: dict[str, Any]) -> str | None:
        """``live``/``stale`` for a mission that should have a controller, else ``None``.

        The answer is reused for ``probe_ttl`` seconds so redrawing never
        probes the controller lock more than once per interval and mission.
        """
        if str(mission.get("status")) not in _LIVE_STATUSES:
            return None
        ident = str(mission.get("id", ""))
        now = time.monotonic()
        cached = self._probes.get(ident)
        if cached is None or now - cached[0] >= self.probe_ttl:
            try:
                alive = self.probe(ident)
            except Exception:  # liveness is advisory; never break the list
                alive = None
            cached = self._probes[ident] = (now, alive)
        bridge_redraw.note_wall(max(0.0, self.probe_ttl - (now - cached[0])) + 0.01)  # probed again after the interval
        if cached[1] is None:
            return None
        return "live" if cached[1] else "stale"

    def review_task(self, mission: dict[str, Any]) -> int | None:
        """Number of the task that awaits the operator's review in a RUNNING mission, else ``None``."""
        if str(mission.get("status")) != "RUNNING":
            return None
        stat = self.stats.get(str(mission.get("id")))
        return stat.review if stat is not None else None

    def selected_mission(self) -> dict[str, Any] | None:
        shown = self.view()
        return shown[self.selected - 1] if 1 <= self.selected <= len(shown) else None

    def attach_ready(self) -> bool:
        """True when the selected mission runs a native provider station that Enter can attach to."""
        mission = self.selected_mission()
        if mission is None or str(mission.get("status")) not in _LIVE_STATUSES:
            return False
        config = mission.get("config")
        return bool(isinstance(config, dict) and config.get("native_tui") and not config.get("headless")
                    and self.controller_state(mission) == "live")

    def enter_action(self) -> str:
        """Enter attaches to a live provider station; any other mission opens its controls."""
        return "attach" if self.attach_ready() else "open"

    def startable(self) -> bool:
        mission = self.selected_mission()
        return mission is not None and str(mission.get("status")) in {"CREATED", "STOPPED"}

    def active_notice(self) -> str:
        """The confirmation of the last save while that mission is still the selected one."""
        return self.notice if self.notice and self.selected_id() == self.notice_id else ""

    def installed_providers(self) -> tuple[str, ...]:
        if self.installed is None:
            self.installed = detect_installed()
        return self.installed

    def provider_note(self) -> str:
        """`(kilo not installed; claude disabled in config)`, or empty when everything is offered."""
        installed = self.installed_providers()
        missing = [name for name in PROVIDER_ORDER if name not in installed and name not in self.disabled]
        parts = []
        if missing:
            parts.append(f"{', '.join(missing)} not installed")
        if self.disabled:
            parts.append(f"{', '.join(self.disabled)} disabled in config")
        return f"({'; '.join(parts)})" if parts else ""

    def selection(self) -> list[str]:
        return [item.strip() for item in self.providers.split(",") if item.strip()]

    def toggle_provider(self, name: str) -> None:
        chosen = self.selection()
        if name in chosen:
            chosen.remove(name)
        else:
            chosen.append(name)
        self.providers = ",".join(chosen)
        self.error = ""

    def choices(self) -> list[tuple[str, str]]:
        """Selectable (name, detail) pairs: the selected entries in rotation order, then the others alphabetically.

        Providers and routes are mixed, so `[1]` is always the top row.
        """
        details = {name: "" for name in self.installed_providers()}
        details.update({route.route_id: route.detail for route in self.routes})
        chosen = [name for name in dict.fromkeys(self.selection()) if name in details]
        rest = sorted(name for name in details if name not in chosen)
        return [(name, details[name]) for name in [*chosen, *rest]]

    def follow(self, name: str) -> None:
        """Keep the checklist cursor on `name` after the order changed."""
        names = [item for item, _detail in self.choices()]
        if name in names:
            self.provider_cursor = names.index(name)

    def picker_vertical(self, width: int) -> bool:
        """Use a vertical checklist when routes exist or one line cannot hold the providers."""
        if self.routes:
            return True
        needed = 2 + cell_width(_PROVIDERS_LABEL) + sum(cell_width(self._provider_item(n, False)) + 1
                                                          for n in self.installed_providers())
        return needed > width - 1

    def is_vertical(self) -> bool:
        return bool(self.routes) if self.vertical is None else self.vertical

    def form_layout(self, height: int, width: int) -> FormLayout:
        """Rows of the six form fields and, for a vertical picker, of its visible items."""
        vertical = self.picker_vertical(width)
        if not vertical:
            return FormLayout([_BODY_Y + 2 * index for index in range(6)], False, [])
        count = len(self.choices())
        budget = max(height - 3, 18) - _BODY_Y
        gap = 1 if 6 + count + 5 <= budget else 0
        visible = count if gap else max(1, min(count, budget - 6))
        cursor = min(self.provider_cursor, max(0, count - 1))
        first = window_start(cursor, visible)
        ys, y = [], _BODY_Y
        for index in range(6):
            ys.append(y)
            y += 1 + gap + (visible if index == 1 else 0)
        return FormLayout(ys, True, [(first + offset, ys[1] + 1 + offset) for offset in range(min(visible, count - first))],
                          total=count)

    def provider_slots(self) -> list[tuple[int, int, str]]:
        """Screen columns (start, end, name) of each selectable provider."""
        x = 2 + cell_width(_PROVIDERS_LABEL)
        slots = []
        for name, _detail in self.choices():
            end = x + cell_width(self._provider_item(name, False))
            slots.append((x, end, name))
            x = end + 1
        return slots

    def _provider_item(self, name: str, cursor: bool) -> str:
        chosen = self.selection()
        box = str(chosen.index(name) + 1) if name in chosen else " "
        return f"{glyph('cursor') if cursor else ' '}[{box}] {name}"

    def validate(self) -> None:
        if not self.objective.strip():
            raise ValueError("Enter a mission objective.")
        if len(self.objective.encode("utf-8")) > _MAX_OBJECTIVE_BYTES:
            raise ValueError("Objective exceeds the interactive 1 MiB UTF-8 limit; import a file or use an editor.")
        providers = list(dict.fromkeys(self.selection()))
        valid = KNOWN_PROVIDERS - {"zai"}
        routes = {route.route_id: route for route in self.routes}
        if not providers:
            raise ValueError("Select at least one provider (Space toggles).")
        for item in providers:
            if item not in valid and item not in routes:
                raise ValueError(f"Unknown provider {item}; choose from {', '.join([*PROVIDER_ORDER, *routes])}.")
        for item in providers:
            route = routes.get(item)
            binary = route.provider if route is not None else item
            if binary in self.disabled:
                raise ValueError(f"{item} is disabled by [usage].providers in the llm-tools config (or LLM_USAGE_PROVIDERS). Deselect it.")
            if not provider_installed(binary):
                what = f"{item} needs {binary}, which is" if route is not None else f"{item} is"
                raise ValueError(f"{what} not installed (not on PATH). Deselect it or install its CLI.")
        typed = (self.directory or self.cwd).strip()
        directory = Path(typed).expanduser()
        if not directory.exists():
            raise ValueError(f"Folder does not exist: {short_path(str(directory), 56)}")
        directory = directory.resolve()
        if not directory.is_dir():
            raise ValueError(f"Not a folder: {short_path(str(directory), 56)}")
        self.directory = str(directory)
        self.providers = ",".join(providers)

    def create_args(self) -> list[str]:
        """Rotation arguments for ``mission create``.

        With routes selected, ``--routes`` carries the whole ordered list (a
        bare provider name is a valid route member). Without routes, an empty
        ``--routes`` stops a configured default rotation from overriding the
        providers chosen here.
        """
        chosen = self.selection()
        route_ids = {route.route_id for route in self.routes}
        if any(item in route_ids for item in chosen):
            args = ["--routes", ",".join(chosen)]
            bare = [item for item in chosen if item not in route_ids]
            if bare:
                args += ["--providers", ",".join(bare)]
            return args
        return ["--providers", ",".join(chosen), "--routes="]

    def _vertical_move(self, key: object) -> bool:
        """Move inside the vertical picker; ``False`` at its edges so the field changes."""
        count = len(self.choices())
        cursor = min(self.provider_cursor, max(0, count - 1))
        step = 1 if key == curses.KEY_DOWN else -1
        if not 0 <= cursor + step < count:
            return False
        self.provider_cursor = cursor + step
        return True

    def _move_focus(self, focus: int) -> None:
        """Focus another field. Entering Providers puts the cursor on its first entry.

        The checklist lists the selected entries first, in rotation order, so
        the first entry is the first selected one (or simply the first entry
        when nothing is selected), whichever direction the focus came from.
        """
        entering_providers = focus == 1 and self.focus != 1
        self.focus = focus
        if entering_providers:
            self.provider_cursor = 0
        if focus < 3:
            name = ("objective", "providers", "directory")[focus]
            self.cursors[focus] = len(str(getattr(self, name)))

    def _providers_key(self, key: object) -> None:
        items = [name for name, _detail in self.choices()]
        if not items:
            return
        cursor = min(self.provider_cursor, len(items) - 1)
        if key == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_RIGHT:
            cursor = min(len(items) - 1, cursor + 1)
        elif key == " ":
            self.toggle_provider(items[cursor])
            self.follow(items[cursor])
            return
        self.provider_cursor = cursor

    def _in_burst(self, now: float) -> bool:
        if self.burst_window <= 0:
            return False
        return now - self.last_input < self.burst_window or bool(self.input_pending())

    def key(self, key: object, *, deliberate: bool = False) -> str | None:
        """Return an explicit action, leaving persistence outside curses.

        ``deliberate`` marks input that cannot be part of a paste (a mouse click).
        """
        if key in {"\x03", "\x04"}:
            return "quit"
        now = time.monotonic()
        if self.theme_hint and now - self.theme_hint_since > _THEME_HINT_GRACE:
            self.theme_hint = ""  # a note, not a banner: gone at the first key after the screen settled
        enter = key in {"\n", "\r", curses.KEY_ENTER}
        if not enter:
            self.last_input = now
            if self.error.startswith(_BURST_HINT):
                self.error = ""
        burst = self.creating and enter and not deliberate and self._in_burst(now)
        if not self.creating:
            self.error = ""
            if self.filtering:
                return self._filter_key(key)
            if key == "\x1b" and self.filter_text:
                self.clear_filter()
                return None
            if key in {"q", "Q", "\x1b"}:
                return "quit"
            if key == "?":
                return "help"
            if key in {"/", "\x06"}:
                self.filtering = True
            elif key in {"n", "N"}:
                self.creating = True
            elif key in {curses.KEY_DOWN, "j", "\t"}:
                self.selected = min(len(self.view()), self.selected + 1)
            elif key in {curses.KEY_UP, "k"}:
                self.selected = max(0, self.selected - 1)
            elif key in {"\n", "\r", curses.KEY_ENTER}:
                if self.selected:
                    return self.enter_action()
                self.creating = True
            elif key in {"o", "O"} and self.selected:
                return "open"
            elif key in {"s", "S"} and self.selected and self.startable():
                return "start-selected"
            return None
        if key == "\x1b":
            self.creating = False
            self.error = ""
        elif key == "\x05" and self.focus == 0:
            return "compose-objective"
        elif self.focus == 1 and key in {curses.KEY_UP, curses.KEY_DOWN} and self.is_vertical():
            # Inside the checklist the arrows only move the cursor and stop at the
            # ends; Tab and Shift-Tab are the way out.
            self._vertical_move(key)
        elif key in {"\t", curses.KEY_DOWN}:
            self._move_focus(min(5, self.focus + 1))
        elif key in {curses.KEY_BTAB, curses.KEY_UP}:
            self._move_focus(max(0, self.focus - 1))
        elif self.focus == 3 and key in {" ", curses.KEY_LEFT, curses.KEY_RIGHT}:
            self.native = not self.native
        elif enter and burst and self.focus == 0:
            # A newline inside a burst belongs to the pasted text, not to form
            # navigation: keep the focus on Objective and edit in the composer.
            self.last_input = now
            return self.paste("\n")
        elif enter and burst and self.focus >= 4:
            # Never activate a button from a burst; require a fresh Enter.
            self.last_input = now
            self.error = _BURST_HINT + ("save" if self.focus == 4 else "start")
        elif enter:
            # Enter walks the fields and stops on Save. Start is the last row and
            # needs Tab/Down to reach it, so repeated Enter never starts provider work.
            if self.focus < 4:
                self.focus += 1
            else:
                try:
                    self.validate()
                    return "create" if self.focus == 4 else "start"
                except (ValueError, OSError, RuntimeError) as exc:
                    self.error = str(exc)
        elif self.focus == 1:
            self._providers_key(key)
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

    def wheel(self, step: int, *, y: int | None = None, height: int = 24) -> None:
        """Mouse wheel: ``step`` is -1 (up) or +1 (down), the same as the arrow keys.

        The wheel only moves the selection or the focus. It never opens a
        mission, toggles a provider, or presses a button.

        * Mission list (also while filtering): move the selection, which keeps
          itself visible exactly as Up/Down do.
        * New-mission form, vertical provider/route checklist: scroll the
          checklist when it has focus or the pointer is over it. The cursor
          stops at the first and last item instead of leaving the checklist,
          so inertial wheel scrolling cannot carry the focus out of it.
        * Elsewhere in the form: move between fields like Up/Down.
        """
        if not self.creating:
            self.error = ""
            self.selected = max(0, min(len(self.view()), self.selected + step))
            return
        # Input that just arrived as typed characters may belong to an
        # unbracketed paste; leave the form alone for the burst window.
        if self.burst_window > 0 and time.monotonic() - self.last_input < self.burst_window:
            return
        layout = self.form_layout(height, self.width or 120)
        over = layout.vertical and y is not None and any(y == row for _index, row in layout.items)
        if layout.vertical and (self.focus == 1 or over):
            if self.focus != 1:
                self.focus = 1
                self.cursors[1] = len(self.providers)
            self._vertical_move(curses.KEY_DOWN if step > 0 else curses.KEY_UP)
            return
        previous = self.last_input
        self.key(curses.KEY_DOWN if step > 0 else curses.KEY_UP)
        self.last_input = previous

    def click(self, y: int, *, height: int = 24, activate: bool = False,
              x: int | None = None, toggle: bool = False) -> str | None:
        if self.creating:
            layout = self.form_layout(height, self.width or 120)
            names = [name for name, _detail in self.choices()]
            for index, name_y in layout.items:
                if y == name_y:
                    self.focus = 1
                    self.provider_cursor = index
                    if toggle or activate:
                        self.toggle_provider(names[index])
                        self.follow(names[index])
                    return None
            if y in layout.ys:
                self.focus = layout.ys.index(y)
                if self.focus == 1 and not layout.vertical and x is not None and (toggle or activate):
                    for index, (start, end, name) in enumerate(self.provider_slots()):
                        if start <= x < end:
                            self.provider_cursor = index
                            self.toggle_provider(name)
                            self.follow(name)
                            break
                if self.focus in {4, 5}:
                    return self.key("\n", deliberate=True)
        else:
            visible = list_rows(height)
            start = window_start(self.selected, visible)
            count = min(visible, len(self.view()) + 1 - start)
            if _BODY_Y <= y < _BODY_Y + count:
                self.selected = start + y - _BODY_Y
                if activate:
                    return self.key("\n", deliberate=True)
        return None


def handle_mouse(state: Launcher, height: int, *, ignore: bool = False) -> str | None:
    """Apply the queued mouse event: wheel scrolling or a left click.

    ``ignore`` still consumes the event, so a stale one is never replayed
    later, but takes no action.
    """
    event = read_mouse()
    if event is None or ignore:
        return None
    x, y, buttons = event
    wheel = wheel_delta(buttons)
    if wheel is not None:
        if wheel[0]:
            state.wheel(wheel[0], y=y, height=height)
        return None
    left_click = curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED | curses.BUTTON1_DOUBLE_CLICKED
    if not buttons & left_click:
        return None
    return state.click(y, height=height, x=x, activate=bool(buttons & curses.BUTTON1_DOUBLE_CLICKED),
                       toggle=bool(buttons & curses.BUTTON1_CLICKED))


def _discard_incomplete_escape(state: Launcher) -> bool:
    """Reset a timed-out non-paste escape prefix so later input stays usable."""
    decoder = state.paste_decoder
    if decoder.waiting and not decoder.active and not state.escape_pending:
        decoder.reset()
        state.error = "Unknown or incomplete escape sequence ignored."
        return True
    return False


def controller_note(mission: dict[str, Any], controller: str | None, review: int | None = None) -> tuple[str, str] | None:
    """Line (and its colour role) explaining what Enter does for a mission that should have a controller.

    A task awaiting the operator's review (``review`` is its number) comes first.
    """
    if str(mission.get("status")) not in _LIVE_STATUSES:
        return None
    if review is not None:
        return localize(f"task {review} awaits your review · Enter to open"), "state_review"
    if controller == "stale":
        return localize("controller not running · Enter opens controls, then type resume"), "warn"
    if controller != "live":
        return "Enter opens controls", "dim"
    config = mission.get("config")
    if isinstance(config, dict) and config.get("native_tui"):
        return localize("Enter attaches to the provider station · o opens controls"), "dim"
    return localize("Enter opens controls · controller is running"), "dim"


def field_hint(state: Launcher, width: int = 200) -> str:
    """Hint for the focused form field (shown above the footer)."""
    return localize(_field_hint(state, width))


def _field_hint(state: Launcher, width: int) -> str:
    if state.focus == 0:
        hint = "Type the task · Tab moves on to Start · Ctrl-E full editor · paste supported"
        if state.objective:
            hint += f" · {len(state.objective.encode('utf-8'))} bytes · {state.objective.count(chr(10)) + 1} lines"
        return hint
    if state.focus == 1:
        if state.is_vertical():
            return "↑/↓ choose · Space toggle · Tab next field · numbers show rotation order"
        return "←/→ choose · Space toggle · numbers show rotation order"
    if state.focus == 2:
        return "Path to work in · Tab next"
    if state.focus == 3:
        full = "Space/←/→ switch: Native shows the provider terminal in a station; Headless runs it without a terminal"
        short = "Space/←/→ switch · Native: terminal in a station · Headless: no terminal"
        return full if cell_width(localize(full)) < width else short
    if state.focus == 4:
        return "Enter to save · no provider is started"
    first = state.selection()[:1]
    text = f"Starts {first[0] if first else 'the first provider'} now · uses provider capacity"
    if first == ["claude"] and state.native:
        from . import bridge_doctor

        if bridge_doctor.claude_folder_trusted(state.directory or state.cwd) is False:
            text += " · Claude asks once to trust this folder: choose Yes"
    return text


@dataclass(frozen=True)
class ListColumns:
    """Cell widths of the mission list columns; a width of 0 drops the column."""

    name: int
    progress: int = 0
    provider: int = 0
    age: int = 0
    folder: int = 0
    ident: int = 0


def row_provider(mission: dict[str, Any], stats: MissionStats | None, controller: str | None) -> str:
    """Provider or route a live mission is working on; empty for other missions."""
    if str(mission.get("status")) not in _LIVE_STATUSES:
        return ""
    if controller == "stale":
        return "-"
    return (stats.route if stats is not None else "") or "-"


def plan_columns(missions: list[dict[str, Any]], stats: dict[str, MissionStats], width: int, extra: int = 0) -> ListColumns:
    """Choose the columns that fit ``width``, dropping the lowest-value ones first.

    The status and mission name always stay. Columns are added in the order
    task progress, id, age, provider, folder, each only while the mission name
    keeps a readable width; whatever space remains goes to the name, then the
    folder. ``extra`` reserves cells for the stale-controller marker.
    """
    room = width - 1 - 2 - _STATE_CELLS - extra - 2 - _CHIP_CELLS  # prefix, state, gap, mission chip
    longest = max((cell_width(_clean(m.get("name", ""))) for m in missions), default=1)
    room -= _NAME_WIDTH_MIN
    progress = min(_COLUMN_WIDTH_MAX, max([cell_width(_NO_TASKS)] + [cell_width(progress_text(stats.get(str(m.get("id")))))
                                                                    for m in missions]))
    live = [m for m in missions if str(m.get("status")) in _LIVE_STATUSES]
    routes = [cell_width(_clean(stats[str(m.get("id"))].route)) for m in live if str(m.get("id")) in stats]
    provider = min(_COLUMN_WIDTH_MAX, max(routes + [cell_width("PROVIDER")])) + 2 if live else 0  # + provider square
    chosen = {"progress": 0, "ident": 0, "age": 0, "provider": 0, "folder": 0}
    # (column, width, name cells to leave): a column is kept only if the name
    # still gets that many cells (or its full length, if shorter) afterwards.
    for key, cell, keep in (("progress", progress, 20), ("ident", _ID_WIDTH, 20), ("age", _AGE_WIDTH, 20),
                            ("provider", provider, 20), ("folder", _FOLDER_WIDTH_MIN, 28)):
        spare = room - (cell + 2)
        if cell and spare >= max(0, min(longest, keep) - _NAME_WIDTH_MIN):
            chosen[key] = cell
            room = spare
    name = _NAME_WIDTH_MIN + min(room, max(0, longest - _NAME_WIDTH_MIN))
    room -= name - _NAME_WIDTH_MIN
    if chosen["folder"]:
        chosen["folder"] += room
    return ListColumns(name, chosen["progress"], chosen["provider"], chosen["age"], chosen["folder"], chosen["ident"])


def _draw_checklist(put: Callable[..., int], state: Launcher, layout: FormLayout, width: int, focused: bool) -> None:
    """Providers and routes as a vertical checklist under the ``Providers:`` label."""
    role = "selected" if focused else "info"
    y = layout.ys[1]
    choices = state.choices()
    chosen = state.selection()
    if focused:
        put(y, 0, " " * width, role, room=width)
    x = put(y, 0, ("> " if focused else "  ") + _PROVIDERS_LABEL, role, room=width)
    if chosen:
        x += put(y, x, "rotation: " + " \u2192 ".join(chosen), role, room=width)
    else:
        x += put(y, x, "none selected", "warn" if not focused else role, room=width)
    first, last = (layout.items[0][0], layout.items[-1][0]) if layout.items else (0, -1)
    if layout.total > len(layout.items):
        x += put(y, x, f"  \u00b7 {first + 1}-{last + 1} of {layout.total}", "dim" if not focused else role, room=width)
    note = state.provider_note()
    if note:
        put(y, x + 2, note, "muted" if not focused else role, room=width)
    cursor = min(state.provider_cursor, max(0, len(choices) - 1))
    item_width = max((cell_width(state._provider_item(name, False)) for name, _detail in choices), default=0)
    for index, row in layout.items:
        name, detail = choices[index]
        under = focused and index == cursor
        row_role = "selected" if under else "info"
        if under:
            put(row, 0, " " * width, row_role, room=width)
        item = state._provider_item(name, under)
        put(row, 4, item, row_role, room=width, attr=curses.A_UNDERLINE if under else 0)
        if detail:
            put(row, 4 + item_width + 2, detail, row_role if under else "dim", room=width)


def list_footer(state: Launcher) -> str:
    """Key hints of the mission list; every key named here is handled by ``Launcher.key``."""
    sep = f" {glyph('sep')} "
    if state.filtering:
        return sep.join(["Type to filter", f"{glyph('updown')} select", "Enter open", "Esc clear filter"])
    if state.attach_ready():
        parts = [f"{glyph('updown')} select", "Enter attach", "o controls", "N new mission"]
    elif state.startable():
        parts = [f"{glyph('updown')} select", "Enter controls", "S start", "N new mission"]
    else:
        parts = [f"{glyph('updown')} select", "Enter open", "N new mission"]
    if len(state.missions) > _FILTER_HINT_MIN or state.filter_text:
        parts.append("/ filter")
    parts.append("? help")
    if state.filter_text:
        parts.append("Esc clear filter")
    parts.append("Q quit")
    return sep.join(parts)


def footer_segments(text: str) -> list[tuple[str, str]]:
    """Footer hints as segments: key names in `key`, the verbs in `muted`, separators in `dim`."""
    from .bridge_draw import _split_hint

    sep = f" {glyph('sep')} "
    out: list[tuple[str, str]] = []
    for index, item in enumerate(text.split(sep)):
        if index:
            out.append((sep, "dim"))
        key, verb = _split_hint(item)
        if key:
            out.append((key, "key"))
            if verb:
                out.append((" " + verb, "muted"))
        else:
            out.append((item, "muted"))
    return out


def form_footer(state: Launcher) -> str:
    sep = f" {glyph('sep')} "
    if state.focus == 1 and state.is_vertical():
        return sep.join(["Tab next field", "Enter next", "Esc back"])
    if state.focus >= 4:
        return sep.join([f"Tab/{glyph('updown')} fields", "Enter " + ("save" if state.focus == 4 else "start"), "Esc back"])
    return sep.join([f"Tab/{glyph('updown')} fields", "Enter next", "Esc back"])


def _draw_list_header(put: Callable[..., int], cols: ListColumns, stale_w: int) -> None:
    """Dim column labels above the mission rows, aligned with the cells below."""
    cells = [("", 2), ("STATUS", _STATE_CELLS + stale_w)]
    if cols.ident:
        cells += [("", 2), ("ID", cols.ident)]
    cells += [("", 2 + _CHIP_CELLS), ("MISSION", cols.name)]
    if cols.progress:
        cells += [("", 2), ("TASKS", cols.progress)]
    if cols.provider:
        cells += [("", 2), ("", 2), ("PROVIDER", cols.provider - 2)]
    if cols.age:
        cells += [("", 2), ("UPDATED", cols.age)]
    if cols.folder:
        cells += [("", 2), ("", _FOLDER_MARK), ("FOLDER", cols.folder - _FOLDER_MARK)]
    x = 0
    for text, width in cells:
        put(_BODY_Y - 1, x, pad(truncate(text, width), width), "dim")
        x += width


def _mission_row(state: Launcher, mission: dict[str, Any], cols: ListColumns, controller: str | None,
                 stale_w: int, selected: bool, now: float) -> list[tuple[str, str]]:
    """Text and colour role of each cell in one mission row.

    Colour goes on the smallest element: the state glyph and word, the mission
    chip and the provider square. Everything else stays in the default text
    colour, or `muted` for facts and for rows of finished missions.
    """
    stat = state.stats.get(str(mission.get("id")))
    status = str(mission.get("status", "-"))
    stale = controller == "stale"
    finished = status_group(status) == _GROUP_FINISHED
    reviewing = state.review_task(mission) is not None
    shown_status = "REVIEW" if reviewing else status
    base = "selected" if selected else ("muted" if finished else "info")
    quiet = "selected" if selected else "muted"
    config = mission.get("config") if isinstance(mission.get("config"), dict) else {}
    chip = "selected" if selected else mission_role(mission.get("id"), config)
    parts: list[tuple[str, str]] = [
        ("> " if selected else "  ", "selected" if selected else "info"),
        (pad(truncate(state_text(shown_status), _STATE_CELLS), _STATE_CELLS),
         "selected" if selected else ("state_review" if reviewing else "warn" if stale else status_role(status))),
    ]
    if stale_w:
        parts.append((pad(" " + _STALE_MARKER if stale else "", stale_w), "selected" if selected else "warn"))
    if cols.ident:
        parts += [("  ", base), (pad(str(mission.get("id", ""))[:cols.ident], cols.ident), quiet)]
    parts += [("  ", base), (glyph("chip") + " ", chip), (pad(truncate(str(mission.get("name", "")), cols.name), cols.name), base)]
    if cols.progress:
        parts += [("  ", base), (pad(truncate(progress_text(stat), cols.progress), cols.progress), base)]
    if cols.provider:
        route = row_provider(mission, stat, controller)
        text = pad(truncate(route, cols.provider - 2), cols.provider - 2)
        square = glyph("square") if route and route != "-" else " "
        parts += [("  ", base), (square, "selected" if selected else provider_role(route)), (" ", base), (text, base)]
    if cols.age:
        parts += [("  ", base), (pad(relative_age(mission.get("updated_at"), now), cols.age), quiet)]
        until = age_changes_in(mission.get("updated_at"), now)
        if until is not None:
            bridge_redraw.note_wall(until)
    if cols.folder:
        here = str(mission.get("cwd")) == state.cwd
        parts += [("  ", base), (f"{glyph('here')} " if here else "  ", "selected" if selected else "accent"),
                  (short_path(str(mission.get("cwd", "")), cols.folder - _FOLDER_MARK), quiet)]
    return parts


def _draw(window: curses.window, state: Launcher) -> None:
    from .bridge_draw import role_attr
    from .bridge_tui import sync_window_size
    from .bridge_input import input_view

    sync_window_size(window)
    height, width = window.getmaxyx()
    window.erase()

    def put(y: int, x: int, text: str, role: str = "info", *, room: int | None = None, attr: int = 0) -> int:
        """Draw single-line text clipped to terminal cells; return cells used."""
        limit = (width - 1 if room is None else room) - x
        if not (0 <= y < height) or limit <= 0:
            return 0
        rendered = cell_slice(_flat(text), 0, limit)
        try:
            window.addnstr(y, x, rendered, len(rendered), role_attr(role) | attr)
        except curses.error:
            pass
        return cell_width(rendered)

    def line(y: int, text: str, role: str = "info") -> None:
        put(y, 0, text, role)

    def segments_line(y: int, parts: list[tuple[str, str]]) -> None:
        x = 0
        for text, role in parts:
            x += put(y, x, text, role)

    line(0, "LLM BRIDGE", "brand")
    if height < _MIN_ROWS or width < _MIN_COLUMNS:
        message = (f"Enlarge the terminal to at least {_MIN_COLUMNS} columns and {_MIN_ROWS} rows "
                   f"(now {width}{glyph('times')}{height}).")
        for offset, text in enumerate(textwrap.wrap(message, max(10, width - 1))):
            line(2 + offset, text, "warn")
        line(height - 1, "Q / Ctrl-D quit", "dim")
        window.refresh()
        return
    here = f"{glyph('here')} here: "
    put(0, 12, here, "brand")
    put(0, 12 + cell_width(localize(here)), short_path(state.cwd, max(10, width - 14 - cell_width(localize(here)))), "heading")
    line(1, subtitle(state.installed_providers()), "muted")
    caret_at: dict[int, int] = {}
    caret: tuple[int, int] | None = None
    if state.creating:
        line(_HEADING_Y, "New mission", "heading")
        newline = f" {glyph('newline')} "

        def field(index: int, label: str, value: str) -> str:
            preview = value.replace("\n", newline)
            cursor = min(len(value), state.cursors[index])
            insertion = len(value[:cursor].replace("\n", newline))
            visible, offset = input_view(preview, insertion, max(1, width - cell_width(label) - 2))
            caret_at[index] = 2 + cell_width(label) + offset
            return label + visible

        layout = state.form_layout(height, width)
        state.vertical, state.width = layout.vertical, width
        # An unfocused Objective shows its beginning (first line plus a line
        # count), not the tail the caret-following view would end on.
        label = "Objective: "
        if state.focus == 0 or not state.objective.strip():
            objective_row = field(0, label, state.objective)
        else:
            objective_row = label + objective_summary(state.objective, max(1, width - cell_width(label) - 3))
        rows = (
            objective_row,
            None,
            field(2, "Folder: ", state.directory or short_path(state.cwd, 4096)),
            "Mode: " + (f"Native {glyph('dash')} provider terminal in a station" if state.native
                        else f"Headless {glyph('dash')} provider runs without a terminal"),
            "Save mission only",
            "Start and open provider" if state.native else "Start headless work",
        )
        for index, text in enumerate(rows):  # type: ignore[assignment]  # rows holds strings; `text` also names a str | None earlier
            focused = index == state.focus
            role = "selected" if focused else "info"
            y = layout.ys[index]
            if text is not None:
                put(y, 0, ("> " if focused else "  ") + text, role, room=width)
                continue
            if layout.vertical:
                _draw_checklist(put, state, layout, width, focused)
                continue
            if focused:
                put(y, 0, " " * width, role, room=width)
            used = put(y, 0, ("> " if focused else "  ") + _PROVIDERS_LABEL, role, room=width)
            installed = state.installed_providers()
            cursor = min(state.provider_cursor, max(0, len(installed) - 1))
            for slot, (start, _end, name) in enumerate(state.provider_slots()):
                item = state._provider_item(name, focused and slot == cursor)
                attr = curses.A_UNDERLINE if focused and slot == cursor else 0
                put(y, start, item, role, room=width, attr=attr)
                used = start + cell_width(item)
            note = state.provider_note()
            if not installed:
                put(y, used + 1, "no provider CLI found on PATH", "warn", room=width)
            elif note:
                put(y, used + 2, note, "muted" if not focused else role, room=width)
        hint_y = max(height - 3, 18)
        hint = state.error or field_hint(state, width)
        line(hint_y, hint, "error" if state.error else "muted")
        segments_line(height - 1, footer_segments(form_footer(state)))
        if state.focus in caret_at:
            caret = (layout.ys[state.focus], min(width - 1, caret_at[state.focus]))
    else:
        shown = state.view()
        count = len(shown)
        if state.filtering or state.filter_text:
            prompt = f"Filter: {state.filter_text}"
            x = put(_HEADING_Y, 0, prompt, "heading")
            put(_HEADING_Y, x + 2, f"{count} of {len(state.missions)}" if count else "no match", "dim" if count else "warn")
            if state.filtering:
                caret = (_HEADING_Y, min(width - 1, cell_width(localize(prompt))))
        else:
            heading = "Choose a mission" + (f" {glyph('sep')} {count} saved" if count else "")
            line(_HEADING_Y, heading, "heading")
            if state.objective.strip():
                put(_HEADING_Y, cell_width(localize(heading)) + 3, f"Draft kept {glyph('sep')} N to continue", "warn")
        visible = list_rows(height)
        start = window_start(state.selected, visible)
        rows_shown = min(visible, count + 1 - start)
        controllers = {index: state.controller_state(shown[index - 1]) for index in range(start, start + rows_shown) if index}
        stale_w = _STALE_WIDTH if "stale" in controllers.values() else 0
        cols = state.columns(width, stale_w)
        now = time.time()
        if count:
            _draw_list_header(put, cols, stale_w)
        for offset in range(rows_shown):
            index = start + offset
            y = _BODY_Y + offset
            selected = index == state.selected
            if index == 0:
                line(y, ("> " if selected else "  ") + "New mission in this folder", "selected" if selected else "info")
                continue
            if selected:
                put(y, 0, " " * width, "selected")
            x = 0
            for text, role in _mission_row(state, shown[index - 1], cols, controllers.get(index), stale_w, selected, now):
                x += put(y, x, text, role)
        if state.loading and not state.missions:
            line(_BODY_Y + 2, "Loading missions...", "dim")
        elif not state.missions:
            for offset, text in enumerate(textwrap.wrap(localize(_EMPTY_STATE), max(10, width - 1))[:2]):
                line(_BODY_Y + 2 + offset, text, "dim")
        elif not count:
            line(_BODY_Y + 2, f'No mission matches "{state.filter_text}". Esc clears the filter.', "dim")
        if state.selected == 0:
            line(height - 3, f"Creates a mission in {short_path(state.cwd, max(10, width - 24))}", "muted")
        elif count:
            mission = shown[state.selected - 1]
            line(height - 3, mission_detail(mission, width - 1), "muted")
            if not state.error and not state.active_notice():  # the error or the saved notice owns the row below the detail
                note = controller_note(mission, controllers.get(state.selected), state.review_task(mission))
                if note:
                    line(height - 2, note[0], "muted" if note[1] == "dim" else note[1])
        if state.error:
            line(height - 2, state.error, "error")
        elif state.active_notice():
            parts = footer_segments(localize(state.active_notice()))
            segments_line(height - 2, [(parts[0][0], "ok"), *parts[1:]])
        elif state.theme_hint and not (count and state.selected and controller_note(shown[state.selected - 1], controllers.get(state.selected), state.review_task(shown[state.selected - 1]))):
            line(height - 2, state.theme_hint, "muted")
        segments_line(height - 1, footer_segments(list_footer(state)))
    if caret is not None:
        try:
            curses.curs_set(1)
            window.move(*caret)
        except (curses.error, AttributeError):
            pass
    else:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
    window.refresh()


class _Tee(io.StringIO):
    """Capture text while still forwarding it to the real stream."""

    def __init__(self, target: Any) -> None:
        super().__init__()
        self._target = target

    def write(self, text: str) -> int:
        try:
            self._target.write(text)
        except (OSError, ValueError, AttributeError):
            pass
        return super().write(text)


def _read_home(db: sqlite3.Connection, cwd: str) -> tuple[list[dict[str, Any]], dict[str, MissionStats]]:
    """Missions and per-mission stats from one connection: three queries in total.

    Task counts use the store's own completion rule (operator tasks and
    accepted proposals). The provider is the station of each mission's latest
    operation, which is the route id when the mission uses routes.
    """
    db.row_factory = sqlite3.Row
    missions = []
    for row in db.execute("SELECT * FROM missions ORDER BY created_at,id"):
        item = dict(row)
        try:
            item["config"] = json.loads(item.pop("config_json"))
        except (ValueError, KeyError):
            item["config"] = {}
        missions.append(item)
    stats: dict[str, MissionStats] = {str(m["id"]): MissionStats() for m in missions}
    try:
        try:
            # Maintained per-mission counters: one pass over a few rows per mission, not over every task.
            task_rows = db.execute(
                "SELECT mission_id, SUM(n) AS total, SUM(CASE WHEN key='COMPLETED' THEN n ELSE 0 END) AS done "
                "FROM mission_task_counts WHERE scope='status' GROUP BY mission_id"
            ).fetchall()
        except sqlite3.OperationalError:  # a database still at an older schema has no counters yet
            task_rows = db.execute(
                "SELECT mission_id, COUNT(*) AS total, SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) AS done "
                "FROM tasks WHERE origin='operator' OR proposal_status='ACCEPTED' GROUP BY mission_id"
            ).fetchall()
        for row in task_rows:
            stats[str(row["mission_id"])] = MissionStats(int(row["done"] or 0), int(row["total"] or 0))
        for row in db.execute(
                "SELECT mission_id, MIN(ordinal) AS ordinal FROM tasks WHERE status IN ('RUNNING','VERIFYING','READY') AND status='VERIFYING' "
                "AND (origin!='agent-proposal' OR proposal_status='ACCEPTED') GROUP BY mission_id"):
            known = stats.get(str(row["mission_id"]), MissionStats())
            stats[str(row["mission_id"])] = MissionStats(known.done, known.total, known.route, int(row["ordinal"]))
        # One indexed probe per mission for its newest operation (operations_mission_recent).
        for row in db.execute(
                "SELECT m.id AS mission_id, (SELECT COALESCE(NULLIF(o.route_id,''), o.station) FROM operations o "
                "WHERE o.mission_id=m.id ORDER BY o.created_at DESC, o.id DESC LIMIT 1) AS route FROM missions m"):
            if row["route"] is None:
                continue
            known = stats.get(str(row["mission_id"]), MissionStats())
            stats[str(row["mission_id"])] = MissionStats(known.done, known.total, str(row["route"] or ""), known.review)
    except sqlite3.Error:  # stats are advisory; the list still renders without them
        pass
    return order_missions(missions, cwd), stats


def _load_home(cwd: str) -> tuple[list[dict[str, Any]], dict[str, MissionStats]]:
    """First load: opens the store, which also creates or migrates the database."""
    with BridgeStore(store_path()) as store:
        return _read_home(store.db, cwd)


def _refresh_home(cwd: str) -> tuple[list[dict[str, Any]], dict[str, MissionStats]]:
    """Periodic reload over a short read-only connection.

    ``BridgeStore`` takes a write lock when it opens; a refresh every couple of
    seconds must never wait behind a controller's transaction.
    """
    path = store_path()
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.25, isolation_level=None)
    try:
        return _read_home(db, cwd)
    finally:
        db.close()


def _load_missions(cwd: str) -> list[dict[str, Any]]:
    return _load_home(cwd)[0]


def _mission_status(mission_id: str) -> str | None:
    """Current stored status, or ``None`` when it cannot be determined."""
    try:
        with BridgeStore(store_path(), readonly=True) as store:
            return str(store.get_mission(mission_id)["status"])
    except Exception:
        return None


def _stdin_pending() -> bool:
    """True when more terminal input is already queued (zero-timeout poll)."""
    try:
        return bool(select.select([sys.stdin.fileno()], [], [], 0)[0])
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _set_bracketed_paste(enabled: bool) -> None:
    """Ask the terminal to bracket pasted text (DECSET 2004), or stop doing so."""
    try:
        sys.stdout.write("\x1b[?2004h" if enabled else "\x1b[?2004l")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _announce_saved(mission_id: str) -> None:
    """Have the console it opens next say once which id the saved mission has."""
    from . import bridge_tui

    bridge_tui.JUST_SAVED_MISSION = mission_id


def run_launcher(dispatch: Callable[[list[str]], int]) -> int:
    """Open the home screen without launching a provider or inferring a session."""
    probe = bridge_redraw.StoreChangeProbe(default_state_dir() / "bridge.sqlite3")
    try:
        return _run_launcher(dispatch, probe)
    except bridge_hangup.TerminalGone:
        return bridge_hangup.abandon_terminal()
    finally:
        probe.close()


def _run_launcher(dispatch: Callable[[list[str]], int], probe: bridge_redraw.StoreChangeProbe) -> int:
    from .bridge_tui import initialize_colors, sync_window_size
    curses.use_env(False)

    cwd = str(Path.cwd().resolve())
    config = _load_config()
    disabled = disabled_launch_clis()
    installed = tuple(name for name in detect_installed() if name not in disabled)
    routes = tuple(route for route in detect_routes(config) if route.provider not in disabled)
    first_read = [True]

    def read_home() -> tuple[list[dict[str, Any]], dict[str, MissionStats]]:
        # The first read opens the store, which creates or migrates the database; later reads use a short read-only connection.
        if first_read[0]:
            result = _load_home(cwd)
            first_read[0] = False
            return result
        return _refresh_home(cwd)

    reloader = bridge_redraw.BackgroundReload(read_home, probe, fast=_RELOAD_FAST, slow=_RELOAD_SLOW, after=_RELOAD_IDLE_AFTER)
    state = Launcher(cwd, directory=short_path(cwd, 4096), installed=installed, routes=routes,
                     providers=default_providers(installed, config, routes),
                     burst_window=_BURST_SECONDS, input_pending=_stdin_pending,
                     reloader=reloader, loading=True,
                     disabled=tuple(name for name in PROVIDER_ORDER if name in disabled and provider_installed(name)),
                     theme_hint=theme_guess_hint())

    def apply_reload(outcome: tuple[bool, Any] | None) -> bool:
        if outcome is None:
            return False
        ok, value = outcome
        if ok:
            state.reload(*value)
        elif state.loading and not state.error:
            state.error = f"Could not read the missions: {value}"
        return True

    def loop(window: curses.window) -> str:
        window.keypad(True)
        paste_keypad = True
        try:
            curses.nonl()
        except curses.error:
            pass
        try:
            # ncurses waits ESCDELAY (1000 ms by default) after a lone Esc in
            # case it starts a key sequence; Esc clears the filter and leaves
            # the form, so it must not lag.
            curses.set_escdelay(_ESCDELAY_MS)
        except (AttributeError, curses.error):
            pass
        initialize_colors()
        enable_mouse()
        planner = bridge_redraw.RedrawPlanner()
        drawn_size: tuple[int, int] | None = None
        last_activity = time.monotonic()
        # Start reading the list now and give it a moment, so a normal list is in the first frame.
        apply_reload(reloader.poll(last_activity, last_activity))
        reloader.wait(_FIRST_LOAD_WAIT)
        while True:
            wanted_keypad = not state.paste_decoder.active
            if wanted_keypad != paste_keypad:
                window.keypad(wanted_keypad)
                paste_keypad = wanted_keypad
            sync_window_size(window)
            if window.getmaxyx() != drawn_size:
                planner.mark()  # resized (or never drawn)
            queued = state.input_pending()
            now = time.monotonic()
            hold = state.paste_decoder.active or queued or state.creating
            if apply_reload(reloader.poll(now, last_activity, hold=hold)):
                planner.mark()
            can_draw = (not state.paste_decoder.active or state.paste_interrupted) and not queued
            if can_draw and planner.should_draw(now):  # skipped while input is queued: keeps burst gaps tiny
                with bridge_redraw.collect() as timed:
                    _draw(window, state)
                drawn_size = window.getmaxyx()
                planner.drawn(timed.next_deadline(time.monotonic()))
            now = time.monotonic()
            timers: list[float | None] = [reloader.wake_in(now) if not hold else None]
            if state.escape_pending:
                timers.append(state.escape_at + _ESCAPE_SECONDS - now)
            if state.paste_decoder.active:
                timers.append(state.paste_activity + _PASTE_STALL_SECONDS - now if not state.paste_interrupted else _SEQUENCE_SECONDS)
            elif state.paste_decoder.waiting and not state.escape_pending:
                timers.append(_SEQUENCE_SECONDS)
            if can_draw:
                wait = planner.wait(now, *timers)
            else:
                wait = min([max(0.0, value) for value in timers if value is not None], default=bridge_redraw.MAX_WAIT_SECONDS)
            try:
                bridge_hangup.set_timeout(window, bridge_redraw.timeout_ms(wait))
            except AttributeError:
                pass
            try:
                key = bridge_hangup.read_wch(window)
            except curses.error:
                error_before, escaped = state.error, state.escape_pending
                if state.paste_decoder.active and time.monotonic() - state.paste_activity >= _PASTE_STALL_SECONDS:
                    state.paste_interrupted = True
                    state.paste_decoder.interrupt()
                    state.error = "Incomplete paste \u00b7 waiting for its terminator; Ctrl-D exits safely. Objective unchanged."
                _discard_incomplete_escape(state)
                if state.escape_pending and time.monotonic() - state.escape_at >= _ESCAPE_SECONDS:
                    state.escape_pending = False
                    if state.paste_decoder.active:
                        state.paste_decoder.reset()
                        state.error = "Incomplete paste discarded; objective unchanged."
                    else:
                        action = state.key("\x1b")
                        if action:
                            return action
                if state.error != error_before or state.escape_pending != escaped:
                    planner.mark()
                continue
            planner.mark()
            last_activity = time.monotonic()
            sync_window_size(window)
            height, width = window.getmaxyx()
            if state.paste_interrupted and key in {"\x04", "\x03"}:
                return "quit"
            if (height < _MIN_ROWS or width < _MIN_COLUMNS) and not state.paste_decoder.active:
                if key in {"q", "Q", "\x04", "\x03"}:
                    return "quit"
                # The key is dropped, and with it the rest of any escape sequence that began before the
                # terminal became too small: a pending Escape must not time out later as a lone Escape.
                state.escape_pending = False
                continue
            if key == curses.KEY_MOUSE:
                # A mouse event inside a bracketed paste or an incomplete escape
                # sequence is never an action.
                action = handle_mouse(state, height, ignore=state.paste_decoder.active or state.paste_decoder.waiting)
            else:
                if key == "\x1b" and not state.paste_decoder.active:
                    state.escape_pending = True
                    state.escape_at = time.monotonic()
                    continue
                if state.escape_pending:
                    state.paste_decoder.feed(b"\x1b")
                    state.escape_pending = False
                if state.paste_decoder.active or state.paste_decoder.waiting or (isinstance(key, str) and key.startswith(("[200~", "\x1b[200~"))):
                    state.paste_activity = time.monotonic()
                    pasted = state.paste_decoder.feed(key.encode("utf-8") if isinstance(key, str) else b"")
                    if pasted is not None:
                        action = state.paste(pasted)
                    elif state.paste_decoder.done and state.paste_decoder.error:
                        state.error = state.paste_decoder.error + "; objective unchanged"
                        state.paste_decoder.reset()
                        state.paste_interrupted = False
                        action = None
                    else:
                        action = None
                else:
                    action = state.key(key)
            if action:
                return action

    while True:
        # Bracketed paste is on only while this curses loop owns the terminal;
        # the composer, help viewer, and dispatched commands manage their own.
        _set_bracketed_paste(True)
        reloader.invalidate()  # re-read the list as soon as the screen is shown again
        try:
            action = bridge_hangup.run_curses(without_flow_control(loop))
        except (KeyboardInterrupt, EOFError):
            return 0
        except curses.error as exc:
            print(f"llm-bridge: terminal unavailable ({exc}); use llm-bridge --help for command-line options", file=sys.stderr)
            return 2
        finally:
            _set_bracketed_paste(False)
        if action == "quit":
            return 0
        if action == "help":
            from .bridge_help import topic_text
            from .bridge_viewer import run_document
            run_document(topic_text("quickstart"), title=localize("Bridge help · quickstart"))
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
        if action in {"open", "attach", "start-selected"}:
            mission_id = str(state.view()[state.selected - 1]["id"])
            state.filtering = False
            if action == "attach":
                # Enter on a live mission goes straight to its provider; detaching returns to the list.
                if dispatch(["attach", "--mission", mission_id]) == 0:
                    continue
            elif action == "start-selected":
                start_errors = _Tee(sys.stderr)
                with redirect_stderr(start_errors):
                    started = dispatch(["start", "--mission", mission_id])
                if started and _mission_status(mission_id) in {"CREATED", None}:
                    state.reload(*_load_home(cwd))
                    state.select_id(mission_id)
                    state.error = " ".join(start_errors.getvalue().split()) or "The mission could not start. Open it and use start from its controls."
                    continue
            if mission_id == state.notice_id:
                _announce_saved(mission_id)
            return dispatch(["control", "--mission", mission_id])
        args = ["mission", "create", "--prompt=" + state.objective, *state.create_args(),
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
        # The mission now exists: never show the filled form again, which
        # would invite a duplicate mission.
        state.objective = ""
        state.cursors[0] = 0
        state.focus = 0
        state.error = ""
        state.creating = False
        state.recovery_draft = None
        state.recovery_source = None
        if action != "start":
            # Stay on the list with the new mission selected and say what to press next.
            state.filter_text, state.filtering = "", False
            state.reload(*_load_home(cwd))
            state.select_id(mission_id)
            state.notice, state.notice_id = f"Saved {mission_id[:8]} {glyph('sep')} S start {glyph('sep')} o controls", mission_id
            continue
        _announce_saved(mission_id)
        start_errors = _Tee(sys.stderr)
        with redirect_stderr(start_errors):
            started = dispatch(["start", "--mission", mission_id, *( ["--native"] if state.native else ["--headless"] )])
        if started and _mission_status(mission_id) in {"CREATED", None}:
            # Start failed before any provider work began (the mission is still
            # CREATED, or its state cannot be read): show why on the home list with the saved mission selected.
            state.filter_text, state.filtering = "", False
            state.reload(*_load_home(cwd))
            ids = [str(m["id"]) for m in state.missions]
            state.selected = ids.index(mission_id) + 1 if mission_id in ids else 0
            reason = " ".join(start_errors.getvalue().split())
            state.error = reason or "Mission was saved but could not start. Open it and use start from its controls to retry."
            continue
        if state.native:
            # start attaches to the validated owned provider station. Once the
            # operator returns with Ctrl-] B/P or detaches (or the station was
            # closed), reopen the mission controls; the mission already exists.
            dispatch(["control", "--mission", mission_id])
            return 0
        break
    return dispatch(["control", "--mission", mission_id])
