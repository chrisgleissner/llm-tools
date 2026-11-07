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
  the screen shows an empty-looking composer and no approval or trust dialog, and no "interrupt"
  hint (a provider that is working). Otherwise nothing is typed and the next look tries again.
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

from .bridge_finish import composer_is_idle
from .bridge_states import ACTIVE_OPERATION_STATUSES

# Providers whose composer the Bridge recognises (the same set `llm-bridge finish` can drive).
NOTICE_PROVIDERS = frozenset({"claude", "codex", "copilot"})
QUIET_SECONDS = 3.0
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


class TaskNoticeDelivery:
    """Per-operation delivery state: the event cursor, the announcements waiting, and the pane quiet timer."""

    def __init__(self, mission_id: str, operation_id: str, *, store: NoticeStore, quiet_seconds: float | None = None) -> None:
        self.mission_id = mission_id
        self.operation_id = operation_id
        self.store = store
        self.quiet_seconds = QUIET_SECONDS if quiet_seconds is None else quiet_seconds
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
        if not composer_is_idle(screen) or BUSY_PATTERN.search(screen):
            return []
        items = sorted(self._pending.values(), key=lambda item: item.ordinal)
        text = compose_notice(self.mission_id, items)
        send(text)
        assert self._scan_cursor is not None
        self.store.record_task_notice(
            self.mission_id, self.operation_id, through_event_id=self._scan_cursor, tasks=[item.as_json() for item in items], text_bytes=len(text.encode("utf-8")),
        )
        self._pending.clear()
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
    if mission["status"] == "RUNNING" and config.get("native_tui") and not config.get("headless") and controller_live:
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
