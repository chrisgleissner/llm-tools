"""Tell a running native provider about tasks the operator adds or amends meanwhile (DOG-017).

A native provider runs one task in the station pane for as long as it needs. Tasks added with
`task add`, the console or a remote client while it works are stored QUEUED, and the provider only
sees them when the next task starts. This module closes that gap with the channel an operator
already has: text typed into the provider pane. It reuses the pane reading and typing of
`bridge_finish` (the composer heuristics and `send_keys`).

How it works:

* The durable cursor is the event ID. A delivery looks at `task-added` and `task-amended` events of
  the mission that are newer than the operation's `operation-prepared` event (or newer than the
  last `task-notice-delivered` event of the operation). Tasks queued before the provider started
  are the operator's plan, not news.
* Several events for one task, and several tasks, collapse into one notice. An amend of a task that
  has not been announced yet stays an "added" entry.
* The notice is typed only when the provider pane has shown the same screen for `QUIET_SECONDS`,
  the screen shows a composer and no approval or trust dialog, and no "interrupt" hint (a provider
  that is working), and the composer is EMPTY. The pane is read with its escape sequences: the text
  after the prompt glyph counts as empty only when it is absent or drawn entirely faint or grey
  (the provider's placeholder). Any other text is an operator draft, and typing would submit it, so
  nothing is typed and the next look tries again. A draft that blocks delivery for
  `DRAFT_REPORT_SECONDS` is reported once as a `task-notice-deferred` event.
* `[bridge] task_notices = false` in the llm-tools config turns the whole feature off.
* After typing, a `task-notice-delivered` event records the tasks and the event ID reached, so a
  restarted controller does not announce them again.

Headless and background missions keep their plain queue: no provider pane exists to type into.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .bridge_finish import COMPOSER_LINE, composer_is_idle
from .bridge_states import ACTIVE_OPERATION_STATUSES

# Providers whose composer the Bridge recognises (the same set `llm-bridge finish` can drive).
NOTICE_PROVIDERS = frozenset({"claude", "codex", "copilot"})
QUIET_SECONDS = 3.0
DRAFT_REPORT_SECONDS = 600.0
KEY_PAUSE_SECONDS = 0.3
MAX_LISTED_TASKS = 5
TITLE_LIMIT = 60
FINISHED_TASK_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
# A provider that is working shows an interrupt hint next to its composer.
BUSY_PATTERN = re.compile(r"(?:esc|ctrl\+c)\s+to\s+interrupt", re.IGNORECASE)
DELIVERED_TEXT = "delivered to the running provider at its next prompt"


@dataclass(frozen=True)
class NoticeItem:
    task_id: int
    ordinal: int
    title: str
    kind: str  # "added" or "amended"

    def as_json(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "ordinal": self.ordinal, "title": self.title, "kind": self.kind}


class NoticeStore(Protocol):
    def task_notice_cursor(self, mission_id: str, operation_id: str) -> int: ...
    def task_notice_candidates(self, mission_id: str, after_id: int, *, limit: int = ...) -> list[dict[str, Any]]: ...
    def record_task_notice(self, mission_id: str, operation_id: str, *, through_event_id: int, tasks: list[dict[str, Any]], text_bytes: int) -> None: ...
    def record_task_notice_deferred(self, mission_id: str, operation_id: str, *, reason: str, tasks: list[dict[str, Any]], waited_seconds: int) -> None: ...


def _clean(value: object, limit: int) -> str:
    text = " ".join("".join(char if char.isprintable() else " " for char in str(value)).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def compose_notice(mission_id: str, items: Sequence[NoticeItem]) -> str:
    """One line for the provider's composer: what changed, that it must not start it, how to read it."""
    shown = list(items)[:MAX_LISTED_TASKS]
    parts = [
        f'task {item.ordinal} "{_clean(item.title, TITLE_LIMIT)}"' + (" was amended" if item.kind == "amended" else " was added")
        for item in shown
    ]
    if len(items) > len(shown):
        parts.append(f"and {len(items) - len(shown)} more")
    number = str(items[0].ordinal) if len(items) == 1 else "N"
    return (
        "[Bridge notice] The operator changed the queue while you work: "
        + ", ".join(parts)
        + ". This is information only: do not start these tasks now, Bridge runs queued tasks itself after your current one."
        + f" Read {'it' if len(items) == 1 else 'a task'} with: llm-bridge task show --mission {mission_id} --task {number}"
    )


_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_BORDER = "│┃|"
_PROMPT_GLYPHS = "›❯>"


def strip_escapes(text: str) -> str:
    return _ESCAPE.sub("", text)


def notices_enabled() -> bool:
    """False when the llm-tools config sets ``[bridge] task_notices = false``; a broken config keeps the default."""
    from . import config as toolconfig  # only the controller needs the llm-tools config

    try:
        config = toolconfig.load_config()
    except SystemExit:
        return True
    bridge = config.get("bridge") if isinstance(config, dict) else None
    return not (isinstance(bridge, dict) and bridge.get("task_notices") is False)


@dataclass
class _Style:
    faint: bool = False
    grey: bool = False
    reverse: bool = False

    @property
    def dim(self) -> bool:
        return self.faint or self.grey


def _grey_256(index: int) -> bool:
    return index in (8, 59) or 232 <= index <= 249


def _apply_sgr(style: _Style, parameters: str) -> None:
    codes = [int(part) if part.isdigit() else 0 for part in parameters.replace(":", ";").split(";")] if parameters else [0]
    position = 0
    while position < len(codes):
        code = codes[position]
        position += 1
        if code == 0:
            style.faint = style.grey = style.reverse = False
        elif code == 2:
            style.faint = True
        elif code == 22:
            style.faint = False
        elif code == 7:
            style.reverse = True
        elif code == 27:
            style.reverse = False
        elif code == 90:
            style.grey = True
        elif code in (39, 30, 31, 32, 33, 34, 35, 36, 37, 91, 92, 93, 94, 95, 96, 97):
            style.grey = False  # a named colour other than bright black is real text
        elif code == 38 and position < len(codes):
            mode = codes[position]
            if mode == 5 and position + 1 < len(codes):
                style.grey = _grey_256(codes[position + 1])
                position += 2
            elif mode == 2 and position + 3 < len(codes):
                red, green, blue = codes[position + 1 : position + 4]
                style.grey = max(red, green, blue) - min(red, green, blue) <= 8 and max(red, green, blue) <= 170
                position += 4
            else:
                style.grey = False
                position += 1


def _cells(line: str) -> list[tuple[str, bool, bool]]:
    """The visible characters of an escape-bearing line with whether each is faint/grey and whether it is reverse video."""
    style = _Style()
    cells: list[tuple[str, bool, bool]] = []
    position = 0
    while position < len(line):
        sgr = _SGR.match(line, position)
        if sgr:
            _apply_sgr(style, sgr.group(1))
            position = sgr.end()
            continue
        escape = _ESCAPE.match(line, position)
        if escape:
            position = escape.end()
            continue
        cells.append((line[position], style.dim, style.reverse))
        position += 1
    return cells


def composer_is_empty(screen: str) -> bool:
    """Whether the provider's composer holds no operator text.

    ``screen`` must be a capture with escape sequences. The last composer line is the one whose text
    after the prompt glyph is read. Text drawn faint (SGR 2), bright black (90) or in a 256-colour or
    truecolour grey is the provider's placeholder; so is a single reverse-video character (a drawn
    cursor) in front of such text. Any other text is a draft. A line this cannot read as a composer
    counts as holding a draft.
    """
    lines = [line for line in screen.splitlines() if strip_escapes(line).strip()][-12:]
    for line in reversed(lines):
        if not COMPOSER_LINE.match(strip_escapes(line).rstrip()):
            continue
        cells = _cells(line)
        glyph = next((index for index, (char, _dim, _rev) in enumerate(cells) if char in _PROMPT_GLYPHS), None)
        if glyph is None:
            return False
        after = cells[glyph + 1 :]
        while after and (not after[-1][0].strip() or after[-1][0] in _BORDER):
            after.pop()
        content = [(dim, reverse) for char, dim, reverse in after if char.strip()]
        plain = [item for item in content if not item[0] and not item[1]]
        cursor = [item for item in content if item[1] and not item[0]]
        return not plain and len(cursor) <= 1
    return False


class TaskNoticeDelivery:
    """Per-operation delivery state: the event cursor, the announcements waiting, and the pane quiet timer."""

    def __init__(self, mission_id: str, operation_id: str, *, store: NoticeStore, quiet_seconds: float | None = None) -> None:
        self.mission_id = mission_id
        self.operation_id = operation_id
        self.store = store
        self.quiet_seconds = QUIET_SECONDS if quiet_seconds is None else quiet_seconds
        self._draft_since: float | None = None
        self._draft_reported = False
        self._scan_cursor: int | None = None
        self._pending: dict[int, NoticeItem] = {}
        self._last_screen: str | None = None
        self._quiet_since = 0.0

    def _collect(self) -> None:
        if self._scan_cursor is None:
            self._scan_cursor = self.store.task_notice_cursor(self.mission_id, self.operation_id)
        for event in self.store.task_notice_candidates(self.mission_id, self._scan_cursor):
            self._scan_cursor = max(self._scan_cursor, int(event["id"]))
            if str(event.get("status")) in FINISHED_TASK_STATUSES:
                self._pending.pop(int(event["task_id"]), None)
                continue
            task_id = int(event["task_id"])
            known = self._pending.get(task_id)
            kind = "added" if event["kind"] == "task-added" or (known is not None and known.kind == "added") else "amended"
            self._pending[task_id] = NoticeItem(task_id, int(event["ordinal"]), str(event.get("title") or ""), kind)

    @property
    def waiting(self) -> bool:
        return bool(self._pending)

    def _note_draft(self, items: list[NoticeItem], now: float) -> None:
        """An operator draft blocks delivery: say so once, after it has blocked for ``DRAFT_REPORT_SECONDS``."""
        if self._draft_since is None:
            self._draft_since = now
        elif not self._draft_reported and now - self._draft_since >= DRAFT_REPORT_SECONDS:
            self._draft_reported = True
            self.store.record_task_notice_deferred(
                self.mission_id, self.operation_id, reason="composer-draft", tasks=[item.as_json() for item in items], waited_seconds=int(now - self._draft_since),
            )

    def poll(self, *, capture: Callable[[], str | None], send: Callable[[str], None], now: float) -> list[NoticeItem]:
        """Collect new task events and, when the provider is idle, type one notice; return what was announced."""
        self._collect()
        if not self._pending:
            return []
        screen = capture()
        if screen is None:
            return []
        if screen != self._last_screen:
            self._last_screen, self._quiet_since = screen, now
        if now - self._quiet_since < self.quiet_seconds:
            return []
        plain = strip_escapes(screen)
        if not composer_is_idle(plain) or BUSY_PATTERN.search(plain):
            return []
        items = sorted(self._pending.values(), key=lambda item: item.ordinal)
        if not composer_is_empty(screen):
            self._note_draft(items, now)
            return []
        self._draft_since = None
        text = compose_notice(self.mission_id, items)
        send(text)
        assert self._scan_cursor is not None
        self.store.record_task_notice(
            self.mission_id, self.operation_id, through_event_id=self._scan_cursor, tasks=[item.as_json() for item in items], text_bytes=len(text.encode("utf-8")),
        )
        self._pending.clear()
        self._draft_reported = False
        self._last_screen = None  # typing changes the screen; wait for a new quiet period
        return items


def type_notice(backend: Any, handle: Any, text: str, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Type ``text`` into the provider pane and submit it, as an operator would."""
    backend.send_keys(handle, pane="provider", keys=text)
    sleep(KEY_PAUSE_SECONDS)
    backend.send_keys(handle, pane="provider", keys="Enter")


# ---------------------------------------------------------------- what the operator is told


def active_operation(store: Any, mission_id: str) -> dict[str, Any] | None:
    marks = ",".join("?" for _ in ACTIVE_OPERATION_STATUSES)
    row = store.db.execute(
        f"SELECT id,task_id,station,route_id,status FROM operations WHERE mission_id=? AND status IN ({marks}) ORDER BY rowid DESC LIMIT 1",
        (mission_id, *ACTIVE_OPERATION_STATUSES),
    ).fetchone()
    return dict(row) if row else None


def task_delivery(store: Any, mission_id: str, task_id: int, *, controller_live: bool) -> dict[str, Any] | None:
    """When the provider will see an added or amended task, for the receipt and the typed command result.

    ``{"mode": "provider-notice"}`` means a running native provider is told at its next idle prompt.
    ``{"mode": "queue"}`` means the task is only seen when it starts; ``after_task`` is the number of
    the task it waits for, or ``None`` when it is next. ``None`` is returned for a task that is not
    waiting to run and has no running provider to tell.
    """
    from .bridge_switcher import provider_of_station

    mission = store.get_mission(mission_id)
    config = mission["config"]
    if mission["status"] == "RUNNING" and config.get("native_tui") and not config.get("headless") and controller_live and notices_enabled():
        operation = active_operation(store, mission_id)
        if operation is not None:
            provider = provider_of_station(config, str(operation["route_id"] or operation["station"] or ""))
            if provider in NOTICE_PROVIDERS:
                return {"mode": "provider-notice", "text": DELIVERED_TEXT, "after_task": None}
    ahead = store.runs_after(task_id)
    if ahead:
        return {"mode": "queue", "text": f"starts after task {ahead[-1]}", "after_task": int(ahead[-1])}
    if str(store.get_task(task_id)["status"]) not in {"QUEUED", "READY"}:
        return None  # a running task keeps its text until the step ends: the receipt says "applies after the current step"
    started = mission["status"] in {"RUNNING", "PAUSED"}
    return {"mode": "queue", "text": "starts next" if started else "starts when the mission starts", "after_task": None}
