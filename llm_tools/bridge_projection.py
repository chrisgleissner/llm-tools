"""Types for the bounded mission projection that `llm-bridge status` and the console read.

``ProjectionParams`` carries the paging, search and size arguments that used to travel as six
separate keywords through the CLI, the monitor and the console. ``Snapshot`` and the smaller
``TypedDict`` classes describe the top-level shape that ``bridge_monitor.project_snapshot``
returns. Nested records that vary by mission (stations, capacity, lifecycle) stay
``dict[str, Any]``; the JSON a command prints is unchanged, because a ``TypedDict`` is an
ordinary ``dict`` at run time.

This module imports only the standard library so every layer can use it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict


@dataclass(frozen=True)
class ProjectionParams:
    """Which slice of a mission the projection reads.

    ``task_limit`` is the task page size (``None`` selects the monitor's default page), ``search``
    filters tasks by title or prompt, ``event_offset`` and ``event_anchor`` page the event list, and
    ``prompt_chars`` limits the prompt text read per task (``None`` reads it whole).
    """

    task_limit: int | None = None
    task_offset: int = 0
    search: str = ""
    event_offset: int = 0
    event_anchor: int | None = None
    prompt_chars: int | None = None

    def as_kwargs(self) -> dict[str, Any]:
        """The six values as the keyword arguments of ``project_snapshot`` and ``status_snapshot``."""
        return {
            "task_limit": self.task_limit,
            "task_offset": self.task_offset,
            "search": self.search,
            "event_offset": self.event_offset,
            "event_anchor": self.event_anchor,
            "prompt_chars": self.prompt_chars,
        }


class TaskPage(TypedDict):
    limit: int
    offset: int
    returned: int
    search: str


class EventPage(TypedDict):
    offset: int
    limit: int
    returned: int


class StationMode(TypedDict):
    configured_mode: str
    actual_mode: str
    source: str


class Snapshot(TypedDict, total=False):
    """Top-level keys of the projection. ``hold`` and ``needs_you`` are added by ``project_snapshot``
    and ``usage`` by the console's Usage view; the rest come from the monitor."""

    schema_version: int
    mission: dict[str, Any]
    tasks: list[dict[str, Any]]
    task_counts: dict[str, Any]
    proposal_counts: dict[str, Any]
    task_total: int
    task_page: TaskPage
    criteria: dict[str, Any]
    current_task: dict[str, Any] | None
    pending_commands: list[dict[str, Any]]
    operations: list[dict[str, Any]]
    events: list[dict[str, Any]]
    operator_events: list[dict[str, Any]]
    event_page: EventPage
    stations: list[dict[str, Any]]
    station_config: dict[str, Any]
    station_mode: StationMode
    station_observation: dict[str, Any]
    station_context: dict[str, Any]
    execution: dict[str, Any]
    capacity: dict[str, Any]
    waiting: dict[str, Any] | None
    lifecycle: dict[str, Any]
    suspend: dict[str, Any]
    health: dict[str, Any]
    persistence: dict[str, Any]
    last_meaningful_progress: dict[str, Any] | None
    hold: dict[str, Any] | None
    needs_you: Any
    usage: dict[str, Any]
