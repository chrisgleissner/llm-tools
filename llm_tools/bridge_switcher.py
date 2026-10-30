"""Mission switcher: list every saved mission and jump to one.

The picker model (:class:`MissionPicker`) is plain Python so it can be tested
without a terminal. :func:`run_picker` draws it with curses, and
:func:`pick_mission_action` / :func:`run_pick_cli` connect the choice to the
existing terminal transitions (attach, cross-station switch, open controls).
"""

from __future__ import annotations

import curses
import locale
import os
import socket
import stat
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import bridge_hangup, bridge_redraw
from .bridge_format import printable
from .bridge_glyphs import FINISHED_STATES, GLYPHS, provider_role, state_look, state_text
from .bridge_input import cell_slice, cell_width
from .bridge_mouse import enable_mouse, read_mouse, wheel_delta
from .bridge_store import BridgeStore, controller_running, store_path
from .bridge_theme import mission_role
from .bridge_states import CONTROLLED_MISSION_STATUSES, FINISHED_MISSION_STATUSES, STATION_CLOSING_MISSION_STATUSES

if TYPE_CHECKING:
    from .bridge_tui import ControlViewAction
    from .terminal import TerminalAction

LABEL_CELLS = 14
_TERMINAL_STATUSES = FINISHED_MISSION_STATUSES
_TASK_RANK = {"RUNNING": 0, "VERIFYING": 1, "READY": 2}
_CONTROLLED_STATUSES = CONTROLLED_MISSION_STATUSES
_STATION_CLOSABLE_STATES = STATION_CLOSING_MISSION_STATUSES
STALE_MARKER = "stale"
STALE_CELLS = len(STALE_MARKER) + 1


# ---------------------------------------------------------------- text helpers

def unicode_ok(codeset: str | None = None) -> bool:
    """Whether the terminal locale can show non-ASCII glyphs (UTF-8 codeset)."""
    if codeset is None:
        try:
            locale.setlocale(locale.LC_CTYPE, "")
        except locale.Error:
            pass
        from .bridge_glyphs import ascii_mode

        ascii_mode.cache_clear()
        return not ascii_mode()
    return str(codeset or "").lower().replace("-", "").replace("_", "") in {"utf8", "utf8mb4"}


def _flat_keep(text: object) -> str:
    """Single-line text that keeps its spacing (column layout)."""
    return printable(text)


def _flat(text: object) -> str:
    return " ".join(printable(text).split())


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def clip(text: object, width: int, *, unicode: bool = True) -> str:
    """Clip to ``width`` cells with an ellipsis (``~`` when ASCII-only)."""
    value = _flat(text)
    if not unicode:
        value = _ascii(value)
    if width <= 0:
        return ""
    if cell_width(value) <= width:
        return value
    mark = "…" if unicode else "~"
    return cell_slice(value, 0, width - 1) + mark if width > 1 else mark


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - cell_width(text))


def short_home(path: object, home: str | None = None) -> str:
    """``~/…`` form of a folder."""
    shown = str(path)
    home = (home if home is not None else os.path.expanduser("~")).rstrip(os.sep)
    if home and shown == home:
        return "~"
    if home and shown.startswith(home + os.sep):
        return "~" + shown[len(home):]
    return shown


def middle_clip(text: str, width: int, *, unicode: bool = True) -> str:
    """Keep the start and the end of a long path visible."""
    value = text if unicode else _ascii(text)
    if cell_width(value) <= width:
        return value
    mark = "…" if unicode else "~"
    if width <= 1:
        return mark[:width]
    head = (width - 1) * 2 // 5
    tail_room = width - 1 - head
    kept: list[str] = []
    used = 0
    for char in reversed(value):
        amount = cell_width(char)
        if used + amount > tail_room:
            break
        kept.append(char)
        used += amount
    return cell_slice(value, 0, head) + mark + "".join(reversed(kept))


def mission_label(name: object, cwd: object, limit: int = LABEL_CELLS) -> str:
    """Short mission label for the station status row and terminal title.

    The repository folder's name identifies a mission best; the mission name
    is used when the folder has none (for example ``/``). The result is at most
    ``limit`` cells and is ellipsized when longer.
    """
    folder = Path(str(cwd or "")).name if str(cwd or "").strip() else ""
    text = _flat(folder or name or "")
    return clip(text, limit) if text else ""


# ---------------------------------------------------------------- mission data

@dataclass(frozen=True)
class MissionEntry:
    """One row of the switcher."""

    mission_id: str
    name: str
    status: str
    cwd: str
    provider: str = "-"
    task_id: int | None = None
    task_ordinal: int | None = None
    task_status: str = ""
    task_title: str = ""
    station: bool = False
    controller: bool = False
    updated_at: str = ""
    rotation: str = ""
    colour: object = None  # the pinned identity colour (1-8), else the ID's hash decides
    review_ordinal: int | None = None  # number of the lowest-numbered task awaiting the operator's review

    @property
    def config(self) -> dict[str, object]:
        return {"colour": self.colour}

    @property
    def finished(self) -> bool:
        return self.status.upper() in FINISHED_STATES

    @property
    def closable(self) -> bool:
        """The mission is in a state whose idle station Bridge closes (runtime ``_FINISHED_STATION_STATES``)."""
        return self.status.upper() in _STATION_CLOSABLE_STATES

    @property
    def live(self) -> bool:
        """Not finished, and a controller or a station is running.

        A failed or stopped mission is live only while its controller runs; a
        station left behind by it is idle, not live work.
        """
        if self.finished:
            return False
        return self.controller or (self.station and not self.closable)

    @property
    def station_idle(self) -> bool:
        """A tmux station is still running for a mission that has finished (or failed or stopped)."""
        return self.station and self.closable and (self.finished or not self.controller)

    @property
    def stale(self) -> bool:
        """The mission should have a controller (RUNNING, PAUSED, STOPPING) but none is running."""
        return self.status.upper() in _CONTROLLED_STATUSES and not self.controller

    @property
    def in_review(self) -> bool:
        return self.status.upper() == "RUNNING" and self.review_ordinal is not None

    @property
    def display_status(self) -> str:
        """The state word shown for the mission: ``REVIEW`` while a task awaits the operator."""
        return "REVIEW" if self.in_review else self.status

    @property
    def short_id(self) -> str:
        return self.mission_id[:8]

    @property
    def task_number(self) -> int | None:
        """The task's number within its mission, falling back to the global ID."""
        return self.task_ordinal if self.task_ordinal is not None else self.task_id

    def search_text(self) -> str:
        cached = self.__dict__.get("_search_text")
        if cached is None:
            cached = self.__dict__["_search_text"] = self._build_search_text()  # the entry is immutable: build once
        return cached

    def _build_search_text(self) -> str:
        parts = [self.mission_id, self.name, self.status, self.cwd, short_home(self.cwd), self.provider, self.task_title,
                 f"task {self.task_number}" if self.task_id is not None else "", "live" if self.live else "", "review" if self.in_review else "", "stale" if self.stale else "",
                 "station idle" if self.station_idle else ""]
        return " ".join(parts).lower()

    def as_json(self) -> dict[str, Any]:
        task = None
        if self.task_id is not None:
            task = {"id": self.task_id, "ordinal": self.task_number, "status": self.task_status, "title": self.task_title}
        return {
            "id": self.mission_id,
            "short_id": self.short_id,
            "name": self.name,
            "status": self.status,
            "cwd": self.cwd,
            "provider": None if self.provider == "-" else self.provider,
            "current_task": task,
            "live": self.live,
            "station": self.station,
            "station_idle": self.station_idle,
            "stale": self.stale,
            "review_task": self.review_ordinal if self.in_review else None,
            "controller_running": self.controller,
            "rotation": self.rotation,
            "colour": mission_role(self.mission_id, self.config),
        }


def provider_of_station(config: dict[str, Any], station: str) -> str:
    """Launch CLI behind an operation's station (a provider name or a route ID)."""
    if not station:
        return "-"
    frozen = config.get("frozen_tool_config", "")
    try:
        import tomllib

        parsed = tomllib.loads(frozen) if isinstance(frozen, str) and frozen else {}
        routes = parsed.get("routes", {}) if isinstance(parsed, dict) else {}
        details = routes.get(station) if isinstance(routes, dict) else None
        if isinstance(details, dict) and isinstance(details.get("provider"), str) and details["provider"]:
            return details["provider"]
    except (ValueError, TypeError):
        pass
    return station


def _rotation(config: dict[str, Any]) -> str:
    value = config.get("routes") or config.get("providers") or ""
    names = [item.strip() for item in str(value).split(",") if item.strip()]
    return ",".join(names)


def socket_listening(path: str | Path, timeout: float = 0.2) -> bool:
    """Whether a server accepts connections on the Unix socket ``path``; a dead socket file refuses."""
    try:
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            return False
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        probe.settimeout(timeout)
        probe.connect(str(path))
    except OSError:  # refused, gone, or not answering in time
        return False
    finally:
        probe.close()
    return True


def default_station_probe(mission_id: str) -> bool:
    """Whether a tmux server is listening on this mission's station socket.

    This is for display (the mission lists), so it only connects to the socket
    in-process instead of starting a tmux client per mission. Actions that act
    on a station (close, attach, switch) check ownership themselves.
    """
    try:
        from .bridge_runtime import mission_socket_path

        return socket_listening(mission_socket_path(mission_id))
    except (OSError, RuntimeError, ValueError):
        return False


def default_controller_probe(mission_id: str) -> bool:
    return bool(controller_running(mission_id, on_error=False))


def collect_entries(
    store: BridgeStore,
    *,
    station_probe: Callable[[str], bool] = default_station_probe,
    controller_probe: Callable[[str], bool] = default_controller_probe,
) -> list[MissionEntry]:
    """Read every mission with its active provider and current task."""
    latest_station: dict[str, str] = {}
    for row in store.db.execute(
        "SELECT m.id AS mission_id, o.station, o.route_id FROM missions m JOIN operations o ON o.rowid = "
        "(SELECT rowid FROM operations WHERE mission_id=m.id ORDER BY rowid DESC LIMIT 1)"
    ):
        latest_station[str(row["mission_id"])] = str(row["route_id"] or row["station"] or "")
    current: dict[str, tuple[tuple[int, int, int, int], dict[str, Any]]] = {}
    for row in store.db.execute(
        "SELECT id,ordinal,mission_id,title,status,priority,ordering FROM tasks WHERE status IN ('RUNNING','VERIFYING','READY') "
        "AND (origin!='agent-proposal' OR proposal_status='ACCEPTED')"
    ):
        key = (_TASK_RANK[str(row["status"])], -int(row["priority"] or 0), int(row["ordering"] or 0), int(row["id"]))
        mission = str(row["mission_id"])
        if mission not in current or key < current[mission][0]:
            current[mission] = (key, dict(row))
    review: dict[str, int] = {}
    for row in store.db.execute(
        "SELECT mission_id,MIN(ordinal) AS ordinal FROM tasks WHERE status IN ('RUNNING','VERIFYING','READY') AND status='VERIFYING' "
        "AND (origin!='agent-proposal' OR proposal_status='ACCEPTED') GROUP BY mission_id"
    ):
        review[str(row["mission_id"])] = int(row["ordinal"])
    entries: list[MissionEntry] = []
    for mission in store.list_missions():
        mission_id = str(mission["id"])
        config = mission.get("config") if isinstance(mission.get("config"), dict) else {}
        task = current.get(mission_id, (None, None))[1]
        entries.append(MissionEntry(
            mission_id=mission_id,
            name=_flat(mission.get("name") or ""),
            status=str(mission.get("status") or "unknown"),
            cwd=str(mission.get("cwd") or ""),
            provider=provider_of_station(config, latest_station.get(mission_id, "")),
            task_id=int(task["id"]) if task else None,
            task_ordinal=int(task["ordinal"]) if task else None,
            task_status=str(task["status"]) if task else "",
            task_title=_flat(task["title"]) if task else "",
            station=bool(station_probe(mission_id)),
            controller=bool(controller_probe(mission_id)),
            updated_at=str(mission.get("updated_at") or mission.get("created_at") or ""),
            rotation=_rotation(config),
            colour=config.get("colour"),
            review_ordinal=review.get(mission_id),
        ))
    return order_entries(entries)


def order_entries(entries: list[MissionEntry]) -> list[MissionEntry]:
    """Live missions first, then active before finished, newest first."""
    newest = sorted(entries, key=lambda e: e.updated_at, reverse=True)
    return sorted(newest, key=lambda e: (not e.live, e.status in _TERMINAL_STATUSES))


STATION_CLOSE_MESSAGES: dict[str, tuple[str, bool]] = {
    "closed": ("Station closed", True),
    "no-station": ("No station is running", True),
    "provider-alive": ("A provider process is still running; station kept", False),
    "clients-attached": ("A terminal is attached to it; station kept", False),
    "controller-active": ("A controller is active; station kept", False),
    "not-finished": ("Mission is not finished; station kept", False),
}


def close_station(entry: MissionEntry) -> str:
    """Close the idle station of a finished mission through the runtime's safe path.

    Returns the result code of :func:`bridge_runtime.gc_finished_station`; a
    station whose provider process is alive, or that has a client attached, is kept.
    """
    from .bridge_runtime import gc_finished_station

    try:
        return gc_finished_station(entry.mission_id)
    except (OSError, RuntimeError, ValueError, PermissionError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"


def gc_stations(entries: list[MissionEntry], closer: Callable[[MissionEntry], str] = close_station) -> list[dict[str, str]]:
    """Close every finished mission's idle station; one result per such mission."""
    return [
        {"id": entry.mission_id, "name": entry.name, "result": closer(entry)}
        for entry in entries if entry.station_idle
    ]


def format_gc(results: list[dict[str, str]]) -> str:
    """Human summary of :func:`gc_stations`."""
    if not results:
        return "No idle stations of finished missions."
    lines = []
    for item in results:
        message = STATION_CLOSE_MESSAGES.get(item["result"], (f"Station kept ({item['result']})", False))[0]
        lines.append(f"{item['id'][:8]}  {message}  {item['name']}")
    closed = sum(1 for item in results if item["result"] == "closed")
    lines.append(f"{closed} of {len(results)} station{'s' if len(results) != 1 else ''} closed")
    return "\n".join(lines)


def gc_json(results: list[dict[str, str]]) -> dict[str, Any]:
    return {"schema_version": 1, "stations": results, "closed": sum(1 for item in results if item["result"] == "closed")}


def action_for(entry: MissionEntry, source_mission: str | None) -> "TerminalAction | ControlViewAction | None":
    """Terminal transition for choosing ``entry``.

    A mission with a running station is attached (the existing attach paths
    pick plain attach, nested attach or cross-station switching). Any other
    mission opens its controls. Choosing a mission without a station from its
    own console changes nothing.
    """
    if entry.station:
        from .terminal import TerminalAction

        return TerminalAction(entry.mission_id, None, "attach")
    if entry.mission_id == source_mission:
        return None
    from .bridge_tui import ControlViewAction

    return ControlViewAction(entry.mission_id, "Bridge")


# ---------------------------------------------------------------- picker model

def fit_columns(desired: list[int], available: int, minimum: int = 6) -> list[int]:
    """Share ``available`` cells; narrow columns keep their content width."""
    widths = [0] * len(desired)
    remaining = max(0, available)
    order = sorted(range(len(desired)), key=lambda i: desired[i])
    for position, index in enumerate(order):
        fair = remaining // (len(order) - position)
        widths[index] = max(1, min(max(desired[index], 1), max(fair, min(minimum, desired[index]))))
        remaining = max(0, remaining - widths[index])
    return widths


_NAMED_KEYS = {
    curses.KEY_UP: "KEY_UP", curses.KEY_DOWN: "KEY_DOWN", curses.KEY_PPAGE: "KEY_PPAGE",
    curses.KEY_NPAGE: "KEY_NPAGE", curses.KEY_HOME: "KEY_HOME", curses.KEY_END: "KEY_END",
    curses.KEY_BACKSPACE: "KEY_BACKSPACE", curses.KEY_ENTER: "KEY_ENTER", curses.KEY_DC: "KEY_BACKSPACE",
}


@dataclass
class MissionPicker:
    """Filterable list of missions with a single selection."""

    entries: list[MissionEntry]
    current: str | None = None
    unicode: bool = True
    query: str = ""
    selected: int = 0
    top: int = 0
    chosen: MissionEntry | None = None
    cancelled: bool = False
    page: int = 10
    home: str | None = None
    closer: Callable[["MissionEntry"], str] | None = None  # closes a finished mission's idle station; default: close_station
    confirming: "MissionEntry | None" = None  # the entry whose station close awaits Y/N
    notice: str = ""  # outcome of the last station close, shown until the next key
    state_cells: int = field(default=11, repr=False)
    notice_ok: bool = False

    def visible(self) -> list[MissionEntry]:
        terms = self.query.lower().split()
        return [e for e in self.entries if all(term in e.search_text() for term in terms)]

    def selected_entry(self) -> MissionEntry | None:
        rows = self.visible()
        return rows[self.selected] if 0 <= self.selected < len(rows) else None

    def _move(self, delta: int) -> None:
        count = len(self.visible())
        self.selected = max(0, min(count - 1, self.selected + delta)) if count else 0

    def key(self, key: object) -> str | None:
        """Handle one key; return ``"select"`` or ``"cancel"`` when finished."""
        if isinstance(key, int):
            key = _NAMED_KEYS.get(key, key)
        if self.confirming is not None:
            return self._confirm_key(key)
        self.notice = ""
        if key in {"\x1b", "\x03", "\x04"}:
            self.cancelled = True
            return "cancel"
        if key in {"\n", "\r", "KEY_ENTER"}:
            entry = self.selected_entry()
            if entry is None:
                return None
            self.chosen = entry
            return "select"
        entry = self.selected_entry()
        if entry is not None and entry.station_idle and (key == "\x18" or (key == "x" and not self.query)):
            self.confirming = entry
        elif key in {"KEY_UP", "\x10"}:
            self._move(-1)
        elif key in {"KEY_DOWN", "\x0e"}:
            self._move(1)
        elif key == "KEY_PPAGE":
            self._move(-max(1, self.page - 1))
        elif key == "KEY_NPAGE":
            self._move(max(1, self.page - 1))
        elif key == "KEY_HOME":
            self.selected = 0
        elif key == "KEY_END":
            self.selected = max(0, len(self.visible()) - 1)
        elif key in {"KEY_BACKSPACE", "\x7f", "\b"}:
            self.query = self.query[:-1]
            self.selected = 0
        elif key == "\x15":
            self.query = ""
            self.selected = 0
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.query += key
            self.selected = 0
        return None

    def _confirm_key(self, key: object) -> str | None:
        """Y closes the idle station that ``x`` asked about; any other key keeps it."""
        entry, self.confirming = self.confirming, None
        if entry is None or key not in {"y", "Y"}:
            return None
        result = (self.closer or close_station)(entry)
        message, closed = STATION_CLOSE_MESSAGES.get(result, (f"Station kept ({result})", False))
        if closed:
            self.entries = [replace(e, station=False) if e.mission_id == entry.mission_id else e for e in self.entries]
        self.notice = f"{message}: {entry.name or entry.short_id}"
        self.notice_ok = closed
        return None

    # -- mouse -------------------------------------------------------------

    LIST_TOP = 3  # screen row of the first mission row (title, filter, column heads)
    WHEEL_ROWS = 3

    def wheel(self, direction: int) -> None:
        """Wheel up (-1) or down (+1) moves the selection a few rows."""
        self._move(direction * self.WHEEL_ROWS)

    def click(self, row: int, *, double: bool = False) -> str | None:
        """Select the mission on screen ``row``; a double click opens it."""
        rows = self.visible()
        index = self.top + row - self.LIST_TOP
        if row < self.LIST_TOP or not 0 <= index < len(rows) or row >= self.LIST_TOP + self.page:
            return None
        self.selected = index
        if double:
            self.chosen = rows[index]
            return "select"
        return None

    # -- rendering ---------------------------------------------------------

    def _glyphs(self) -> tuple[str, str, str, str]:
        """(cursor, live marker, this-mission marker, separator) in the picker's character set."""
        return ("›", "●", "▶", " · ") if self.unicode else (">", "*", "@", " | ")

    def _g(self, name: str) -> str:
        return GLYPHS[name][0 if self.unicode else 1]

    # Fixed cells before the name: cursor, live/here mark, mission chip, space, id (8), gap, state (11).
    FIXED_CELLS = 1 + 1 + 1 + 1 + 8 + 2 + 11

    def _columns(self, rows: list[MissionEntry], width: int) -> tuple[bool, list[int]]:
        # A stale marker column follows the state only while a listed mission needs it (as on the home screen).
        self.state_cells = 11 + (STALE_CELLS if any(e.stale for e in rows) else 0)
        show_provider = width >= 76
        names = [len(e.name) for e in rows] or [4]
        repos = [cell_width(short_home(e.cwd, self.home)) for e in rows] or [4]
        provs = [len(e.provider) for e in rows] or [4]
        tasks = [len(self._task_text(e)) for e in rows] or [4]
        desired = [min(max(names + [4]), 40), min(max(repos + [6]), 40), min(max(tasks + [4]), 40)]
        gaps = 2 * (4 if show_provider else 3)
        prov_width = min(max(provs + [8]), 10) + 2 if show_provider else 0  # + provider square and space
        available = width - self.FIXED_CELLS - (self.state_cells - 11) - gaps - prov_width
        widths = fit_columns(desired, available, 8)
        if show_provider:
            widths.append(prov_width)
        return show_provider, widths

    @staticmethod
    def _task_text(entry: MissionEntry) -> str:
        if entry.task_id is None:
            return "-"
        return f"{entry.task_number} {entry.task_title}".strip()

    def _header(self, widths: list[int], show_provider: bool) -> str:
        header = ["   ", " ", pad("ID", 8), "  ", pad("Status", self.state_cells), "  ", pad("Name", widths[0]), "  ", pad("Folder", widths[1]), "  "]
        if show_provider:
            header += ["  ", pad("Provider", widths[3] - 2), "  "]
        header.append("Task")
        return "".join(header)

    def _row_segments(self, entry: MissionEntry, widths: list[int], show_provider: bool, cursor: bool) -> list[tuple[str, str]]:
        """One mission row. Colour sits on the chip, the state and the provider square only."""
        arrow, live, here, _sep = self._glyphs()
        finished = entry.status.upper() in FINISHED_STATES
        look = state_look(entry.display_status)
        state_role = "state_review" if entry.in_review else "warn" if entry.stale else look[1] if look else "info"
        text_role = "muted" if finished else "info"
        mark, mark_role = (here, "accent") if entry.mission_id == self.current else (live, "state_running") if entry.live else (" ", "info")
        folder = clip(middle_clip(short_home(entry.cwd, self.home), widths[1], unicode=self.unicode), widths[1], unicode=self.unicode)
        row: list[tuple[str, str]] = [
            (arrow if cursor else " ", "accent"), (mark, mark_role), (self._g("chip"), mission_role(entry.mission_id, entry.config)), (" ", "info"),
            (pad(entry.short_id, 8), "muted"), ("  ", "info"),
            (pad(clip(state_text(entry.display_status, unicode=self.unicode), 11, unicode=self.unicode), 11), state_role),
        ]
        if self.state_cells > 11:
            row.append((pad(" " + STALE_MARKER if entry.stale else "", self.state_cells - 11), "warn"))
        row += [
            ("  ", "info"),
            (pad(clip(entry.name or "-", widths[0], unicode=self.unicode), widths[0]), text_role), ("  ", "info"),
            (pad(folder, widths[1]), "muted"), ("  ", "info"),
        ]
        if show_provider:
            has_provider = entry.provider not in {"", "-"}
            row += [
                (self._g("square") if has_provider else " ", provider_role(entry.provider) if has_provider else "info"), (" ", "info"),
                (pad(clip(entry.provider, widths[3] - 2, unicode=self.unicode), widths[3] - 2), text_role), ("  ", "info"),
            ]
        row.append((clip(self._task_text(entry), widths[2], unicode=self.unicode), "muted" if finished or entry.task_id is None else "info"))
        return row

    def _row(self, entry: MissionEntry, widths: list[int], show_provider: bool, cursor: bool) -> str:
        return "".join(text for text, _role in self._row_segments(entry, widths, show_provider, cursor))

    def segment_lines(self, height: int, width: int) -> list[tuple[list[tuple[str, str]], str]]:
        """Return ``(segments, style)`` rows filling at most ``height`` x ``width``.

        ``style`` is ``sel`` for the selected row (drawn as one selection bar),
        otherwise ``normal``/``head``/``input``/``dim`` as in :meth:`lines`.
        """
        arrow, live, here, sep = self._glyphs()
        rows = self.visible()
        total = len(self.entries)
        live_count = sum(1 for e in self.entries if e.live)
        out: list[tuple[list[tuple[str, str]], str]] = []

        def plain(text: str, role: str, style: str) -> tuple[list[tuple[str, str]], str]:
            return [(clip(text, width, unicode=self.unicode), role)], style

        title = [
            ("Missions", "heading"), (sep, "dim"), (f"{len(rows)} of {total}", "muted"), (sep, "dim"), (f"{live_count} live", "muted"),
            (sep, "dim"), (live, "state_running"), (" live  ", "muted"), (here, "accent"), (" this mission", "muted"),
        ]
        out.append((self._fit(title, width), "head"))
        out.append((self._fit([("Filter: ", "accent"), (f"{self.query}_", "info")], width), "input"))
        list_height = max(1, height - 5)
        self.page = list_height
        show_provider, widths = self._columns(rows, width)
        out.append((self._fit([(self._header(widths, show_provider), "muted")], width, mark=False), "dim"))
        if not rows:
            message = (f"No missions match '{self.query}'" if self.query else "No missions yet · create one with: llm-bridge mission create --prompt \"...\"")
            if not self.unicode:
                message = _ascii(message)
            out.append(plain(message, "info", "normal"))
            list_rows = 1
        else:
            self.selected = max(0, min(self.selected, len(rows) - 1))
            if self.selected < self.top:
                self.top = self.selected
            elif self.selected >= self.top + list_height:
                self.top = self.selected - list_height + 1
            self.top = max(0, min(self.top, max(0, len(rows) - list_height)))
            window = rows[self.top:self.top + list_height]
            for offset, entry in enumerate(window):
                is_selected = self.top + offset == self.selected
                segments = self._row_segments(entry, widths, show_provider, is_selected)
                if is_selected:
                    segments = [(cell_slice("".join(text for text, _ in segments), 0, width), "selected")]
                out.append((self._fit(segments, width, mark=False), "sel" if is_selected else "normal"))
            list_rows = len(window)
        for _ in range(list_height - list_rows):
            out.append(([("", "info")], "normal"))
        entry = self.selected_entry()
        folder = middle_clip(short_home(entry.cwd, self.home), max(10, width // 2), unicode=self.unicode) if entry is not None else ""
        if self.confirming is not None:
            detail = [(f"Close the idle station of {self.confirming.name or self.confirming.short_id}? Y close{sep}N keep", "warn")]
        elif self.notice:
            detail = [(self.notice, "ok" if self.notice_ok else "warn")]
        elif entry is not None and entry.in_review:
            detail = [(f"task {entry.review_ordinal} awaits your review{sep}Enter to open", "state_review"), (f"{sep}{folder}", "muted")]
        elif entry is not None:
            state = ("station idle" if entry.station_idle else "station running" if entry.station
                     else "controller running" if entry.controller else "controller not running" if entry.stale else "not running")
            role = "state_running" if entry.live else "warn" if entry.stale and not entry.finished else "muted"
            rotation = (entry.rotation or "-").replace(",", " -> " if not self.unicode else " → ")
            detail = [(f"{folder}{sep}{rotation}{sep}", "muted"), (state, role)]
        else:
            detail = [("", "muted")]
        out.append((self._fit(detail, width), "dim"))
        hints = [("Type to filter", ""), ("Up/Down/wheel", "select"), ("Enter", "switch"), ("Esc", "cancel")]
        if self.confirming is not None:
            hints = [("Y", "close station"), ("N", "keep it")]
        elif entry is not None and entry.station_idle and not self.query:
            hints = [("Type to filter", ""), ("Up/Down", "select"), ("Enter", "switch"), ("x", "close station"), ("Esc", "cancel")]
        footer: list[tuple[str, str]] = []
        for index, (key, verb) in enumerate(hints):
            if index:
                footer.append((sep, "dim"))
            footer.extend([(key, "key" if verb else "muted"), (f" {verb}", "muted")] if verb else [(key, "muted")])
        out.append((self._fit(footer, width), "dim"))
        return out[:height]

    def _fit(self, segments: list[tuple[str, str]], width: int, *, mark: bool = True) -> list[tuple[str, str]]:
        """Clip segments to ``width`` cells, ending in the picker's ellipsis when ``mark``."""
        out: list[tuple[str, str]] = []
        used = 0
        total = sum(cell_width(text if self.unicode else _ascii(text)) for text, _ in segments)
        room = width if total <= width or not mark else max(0, width - 1)
        for text, role in segments:
            text = _flat_keep(text) if self.unicode else _ascii(_flat_keep(text))
            piece = cell_slice(text, 0, max(0, room - used))
            out.append((piece, role))
            used += cell_width(piece)
            if used >= room:
                break
        if mark and total > width:
            out.append(("…" if self.unicode else "~", out[-1][1] if out else "info"))
        return out

    def lines(self, height: int, width: int) -> list[tuple[str, str]]:
        """Return ``(text, style)`` rows filling at most ``height`` x ``width``."""
        return [("".join(text for text, _ in segments), style) for segments, style in self.segment_lines(height, width)]

    def plain_text(self, width: int = 100) -> str:
        """Non-interactive table of the visible rows (used by ``llm-bridge missions``)."""
        rows = self.visible()
        show_provider, widths = self._columns(rows, width)
        previous = self.current
        self.current = None
        try:
            body = [self._header(widths, show_provider)] + [self._row(e, widths, show_provider, False).rstrip() for e in rows]
        finally:
            self.current = previous
        return "\n".join(body)


# ---------------------------------------------------------------- curses runner

def mouse_outcome(picker: MissionPicker, row: int, buttons: int) -> str | None:
    """Apply a curses mouse event: wheel scrolls, click selects, double click opens."""
    wheel = wheel_delta(buttons)
    if wheel is not None:
        if wheel[0]:
            picker.wheel(wheel[0])
    elif buttons & getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0):
        return picker.click(row, double=True)
    elif buttons & (getattr(curses, "BUTTON1_CLICKED", 0) | getattr(curses, "BUTTON1_RELEASED", 0)):
        return picker.click(row)
    return None


def run_picker(entries: list[MissionEntry], *, current: str | None = None, unicode: bool | None = None) -> MissionEntry | None:
    """Show the picker full screen; return the chosen mission or ``None``."""
    from .bridge_tui import _put, _put_segments, initialize_colors, sync_window_size

    picker = MissionPicker(entries, current=current, unicode=unicode_ok() if unicode is None else unicode)
    os.environ.setdefault("ESCDELAY", "25")
    curses.use_env(False)

    def loop(window: "curses.window") -> MissionEntry | None:
        window.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        enable_mouse()
        initialize_colors()
        # The list is a fixed snapshot with no clock-driven text: draw on input
        # or resize and sleep in get_wch otherwise.
        planner = bridge_redraw.RedrawPlanner()
        drawn_size: tuple[int, int] | None = None
        while True:
            sync_window_size(window)
            size = window.getmaxyx()
            if size != drawn_size:
                planner.mark()  # resized (or never drawn)
            if planner.should_draw(time.monotonic()):
                height, width = size
                window.erase()
                for y, (segments, style) in enumerate(picker.segment_lines(height, width)):
                    if style == "sel":
                        _put(window, y, 0, "".join(text for text, _ in segments), width, role="selected")
                    else:
                        _put_segments(window, y, 0, segments, width)
                window.refresh()
                drawn_size = size
                planner.drawn(None)
            bridge_hangup.set_timeout(window, bridge_redraw.timeout_ms(planner.wait(time.monotonic())))
            try:
                key = bridge_hangup.read_wch(window)
            except curses.error:
                continue
            planner.mark()
            if key == curses.KEY_MOUSE:
                event = read_mouse()
                if event is None:
                    continue
                outcome = mouse_outcome(picker, event[1], event[2])
            else:
                outcome = picker.key(key)
            if outcome == "select":
                return picker.chosen
            if outcome == "cancel":
                return None

    try:
        return bridge_hangup.run_curses(loop)
    except KeyboardInterrupt:
        return None


# ---------------------------------------------------------------- wiring

def load_entries() -> list[MissionEntry]:
    with BridgeStore(store_path(), readonly=True) as store:
        return collect_entries(store)


def _client_for(source_mission: str) -> str | None:
    """Pick the client that just used the station when several are attached."""
    try:
        from .bridge import _load_handle
        from .terminal import TmuxTerminalBackend

        backend = TmuxTerminalBackend()
        handle = _load_handle(source_mission)
        if len(backend.writable_clients(handle)) > 1:
            return backend.active_client(handle)
    except (OSError, RuntimeError, ValueError, PermissionError, subprocess.SubprocessError):
        pass
    return None


def _expand_bridge_pane(source_mission: str) -> bool:
    """Give the picker the whole window when it runs in the compact Bridge pane."""
    try:
        if not os.environ.get("TMUX_PANE") or os.get_terminal_size().lines > 8:
            return False
        from .bridge import _load_handle
        from .terminal import TmuxTerminalBackend

        handle = _load_handle(source_mission)
        if os.environ.get("TMUX_PANE") != handle.bridge_pane:
            return False
        TmuxTerminalBackend().focus(handle, pane="bridge", expand=True)
        return True
    except (OSError, RuntimeError, ValueError, PermissionError, subprocess.SubprocessError):
        return False


def _restore_bridge_pane(source_mission: str) -> None:
    try:
        from .bridge import _load_handle
        from .terminal import TmuxTerminalBackend

        TmuxTerminalBackend().focus(_load_handle(source_mission), pane="bridge", expand=False)
    except (OSError, RuntimeError, ValueError, PermissionError, subprocess.SubprocessError):
        pass


def pick_mission_action(
    source_mission: str,
    *,
    entries: list[MissionEntry] | None = None,
    chooser: Callable[..., MissionEntry | None] = run_picker,
) -> "TerminalAction | ControlViewAction | None":
    """Run the picker from a mission's console and return the chosen transition."""
    from .terminal import TerminalAction

    rows =entries if entries is not None else load_entries()
    expanded = _expand_bridge_pane(source_mission)
    chosen = chooser(rows, current=source_mission)
    if chosen is None:
        if expanded:
            _restore_bridge_pane(source_mission)
        return None
    action = action_for(chosen, source_mission)
    if isinstance(action, TerminalAction) and chosen.mission_id != source_mission and os.environ.get("TMUX_PANE"):
        client = _client_for(source_mission)
        if client:
            action = TerminalAction(action.mission_id, client, action.action)
    return action


def run_pick_cli(
    from_mission: str | None,
    client: str | None,
    *,
    entries: list[MissionEntry] | None = None,
    chooser: Callable[..., MissionEntry | None] = run_picker,
) -> int:
    """``llm-bridge missions --pick``: the popup / standalone picker.

    With ``from_mission`` (the station popup) a live mission is reached by
    switching that station's client. Without it the current terminal attaches
    like ``llm-bridge attach``. A mission without a station opens its controls.
    """
    from .bridge import _load_handle, console_snapshot, dispatch_console_command
    from .terminal import TerminalAction, TmuxTerminalBackend

    rows = entries if entries is not None else load_entries()
    chosen = chooser(rows, current=from_mission)
    if chosen is None:
        return 0
    if chosen.station:
        backend = TmuxTerminalBackend()
        destination = _load_handle(chosen.mission_id)
        if from_mission is None:
            from .bridge_tui import _execute_terminal_action

            _execute_terminal_action(chosen.mission_id, TerminalAction(chosen.mission_id, None, "attach"))
            return 0
        source = _load_handle(from_mission)
        if chosen.mission_id == from_mission:
            backend.focus(source, pane="provider", expand=True)
            return 0
        selected = client or backend.active_client(source)
        backend.switch_client_to(source, destination, client_name=selected)
        return 0
    if chosen.mission_id == from_mission and from_mission is not None:
        return 0
    # Controls run in this (popup) terminal as in a plain one.
    for name in ("TMUX", "TMUX_PANE"):
        os.environ.pop(name, None)
    from .bridge_tui import run_control

    return run_control(chosen.mission_id, dispatch_console_command, console_snapshot)


def format_missions(entries: list[MissionEntry], *, width: int = 100) -> str:
    """Human listing for ``llm-bridge missions``."""
    if not entries:
        return "No missions yet · create one with: llm-bridge mission create --prompt \"...\""
    picker = MissionPicker(entries, unicode=unicode_ok(), home=None)
    sep = picker._glyphs()[3]
    header = f"{len(entries)} mission{'s' if len(entries) != 1 else ''}{sep}{sum(1 for e in entries if e.live)} live"
    return header + "\n" + picker.plain_text(width)


def missions_json(entries: list[MissionEntry]) -> dict[str, Any]:
    return {"schema_version": 1, "missions": [entry.as_json() for entry in entries]}


__all__ = [
    "MissionEntry", "MissionPicker", "action_for", "close_station", "collect_entries", "format_gc", "format_missions", "gc_json",
    "gc_stations", "mission_label",
    "missions_json", "pick_mission_action", "run_pick_cli", "run_picker", "unicode_ok",
]
