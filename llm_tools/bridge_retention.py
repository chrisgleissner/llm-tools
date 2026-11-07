"""Data retention for Bridge state: the ``[bridge.retention]`` policy and the pruning pass that enforces it.

The policy has four settings (see "Data retention" in docs/llm-bridge/operations.md):

``events_days``
    Diagnostic events of a finished mission older than this many days are deleted.
``finished_missions_days``
    A mission that finished (COMPLETED or CANCELLED) longer ago than this keeps its mission and task rows
    and its audit summary events. Its other events, its remaining operation logs and its verifier files
    on disk are deleted.
``operations_days``
    Operation rows of a finished mission whose logs are already pruned are deleted after this many days.
    The size and SHA-256 of a pruned log stay in the mission's ``operation-log-pruned`` events.
``migration_backups_keep``
    The newest schema-migration backups to keep (``bridge.sqlite3.v*.bak``).

A value of 0 for one of the three ``*_days`` settings turns that rule off. A mission that is not finished
is never touched by these rules; its controller-bookkeeping events are bounded separately by
``BridgeStore.prune_plumbing_events``.

The module imports only the standard library at import time so ``config`` can validate the table.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bridge_store import BridgeStore

DEFAULT_EVENTS_DAYS = 90
DEFAULT_FINISHED_MISSIONS_DAYS = 365
DEFAULT_OPERATIONS_DAYS = 180
DEFAULT_MIGRATION_BACKUPS_KEEP = 3
MAX_DAYS = 3650
MAX_BACKUPS_KEEP = 1000

# A provider run directory is named <YYYYMMDD>-<HHMMSS>-<suffix> by ``common.setup_run_logs``.
_RUN_DIRECTORY = re.compile(r"\d{8}-\d{6}-.+")
# Files and rows removed per pass; the controller repeats the pass until it reports ``complete``.
ARTIFACT_BATCH = 200


@dataclass(frozen=True)
class RetentionPolicy:
    events_days: int = DEFAULT_EVENTS_DAYS
    finished_missions_days: int = DEFAULT_FINISHED_MISSIONS_DAYS
    operations_days: int = DEFAULT_OPERATIONS_DAYS
    migration_backups_keep: int = DEFAULT_MIGRATION_BACKUPS_KEEP


RETENTION_KEYS = frozenset(field.name for field in fields(RetentionPolicy))


def validate_table(table: Any) -> None:
    """Raise ``ValueError`` (with a did-you-mean hint for a misspelt key) when ``[bridge.retention]`` is invalid."""
    if not isinstance(table, dict):
        raise ValueError("bridge.retention must be a table")
    for key in table:
        if key not in RETENTION_KEYS:
            hint = difflib.get_close_matches(str(key), sorted(RETENTION_KEYS), n=1)
            suggestion = f"; did you mean '{hint[0]}'?" if hint else ""
            raise ValueError(f"bridge.retention: unknown key '{key}' (allowed: {', '.join(sorted(RETENTION_KEYS))}){suggestion}")
    for key, value in table.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"bridge.retention.{key} must be a whole number")
        if key == "migration_backups_keep":
            if not 1 <= value <= MAX_BACKUPS_KEEP:
                raise ValueError(
                    f"bridge.retention.{key} must be between 1 and {MAX_BACKUPS_KEEP} "
                    "(the newest backups are the only way back from a failed upgrade, so at least one is kept)"
                )
        elif not 0 <= value <= MAX_DAYS:
            raise ValueError(f"bridge.retention.{key} must be between 0 (never prune) and {MAX_DAYS} days")


def policy_from_config(cfg: dict[str, Any] | None) -> RetentionPolicy:
    """The retention policy of a loaded (already validated) config; defaults for absent keys or a malformed table."""
    bridge = cfg.get("bridge") if isinstance(cfg, dict) else None
    table = bridge.get("retention") if isinstance(bridge, dict) else None
    if not isinstance(table, dict):
        return RetentionPolicy()
    try:
        validate_table(table)
    except ValueError:
        return RetentionPolicy()
    return RetentionPolicy(**{key: int(value) for key, value in table.items()})


def _cutoff(now: datetime, days: int) -> str | None:
    return (now - timedelta(days=days)).isoformat(timespec="seconds") if days > 0 else None


def _file_digest(path: Path) -> tuple[int, str]:
    """Size and SHA-256 of a regular file, read in blocks; ``(0, "unavailable")`` when it cannot be read."""
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
                size += len(block)
    except OSError:
        return 0, "unavailable"
    return size, digest.hexdigest()


def _remove_operation_logs(store: BridgeStore, logs_path: Path) -> None:
    """Delete one operation's log file, and its run directory when the file lives in one nobody else uses."""
    run_directory = logs_path.parent
    if _RUN_DIRECTORY.fullmatch(run_directory.name) and not run_directory.is_symlink():
        siblings = [path for path in run_directory.iterdir() if path != logs_path] if run_directory.is_dir() else []
        if not any(store.logs_path_references(str(path)) for path in siblings):
            shutil.rmtree(run_directory, ignore_errors=True)
            return
    for candidate in (logs_path, *logs_path.parent.glob(logs_path.name + ".*")):
        if candidate.is_file() and not candidate.is_symlink():
            candidate.unlink(missing_ok=True)


def prune_finished_artifacts(store: BridgeStore, mission_id: str, *, limit: int = ARTIFACT_BATCH) -> dict[str, int]:
    """Delete the on-disk evidence of one long-finished mission: provider run logs and verifier files.

    Each log's size and SHA-256 are recorded first (``operation-log-pruned``), as for the per-mission
    run-directory cap. Returns the counts per category; ``operation_logs`` reaching ``limit`` means more remain.
    """
    counts = {"operation_logs": 0, "verifier_files": 0}
    for operation in store.operations_with_logs(mission_id, limit=limit):
        logs_path = Path(str(operation["logs_path"]))
        size, digest = _file_digest(logs_path)
        store.record_pruned_operation_log(mission_id, str(logs_path), size_bytes=size, sha256=digest)
        try:
            _remove_operation_logs(store, logs_path)
        except OSError:
            pass  # the reference is already detached; a leftover file is retried by nothing and is only disk
        counts["operation_logs"] += 1
    verifier = store.path.parent / "runtime" / f"mission-{mission_id}" / "verifier"
    try:
        entries = [entry for entry in os.scandir(verifier) if entry.is_file(follow_symlinks=False)]
    except OSError:
        entries = []
    for entry in entries[:limit]:
        try:
            os.unlink(entry.path)
        except OSError:
            continue
        counts["verifier_files"] += 1
    return {key: value for key, value in counts.items() if value}


def run_pass(
    store: BridgeStore,
    *,
    policy: RetentionPolicy,
    now: datetime | None = None,
    chunk: int = 2000,
    budget_seconds: float = 1.0,
) -> dict[str, Any]:
    """One bounded retention pass over the whole database.

    Returns ``{"deleted": {category: count}, "missions": {mission_id: {category: count}}, "complete": bool,
    "cutoffs": {...}}``. ``complete`` is false when the time budget or the per-pass file limit stopped it
    early; the controller repeats the pass shortly. Categories: ``events``, ``finished_mission_events``,
    ``operations``, ``managed_processes``, ``operation_logs``, ``verifier_files`` and ``migration_backups``.
    """
    moment = now or datetime.now(timezone.utc)
    events_before = _cutoff(moment, policy.events_days)
    finished_before = _cutoff(moment, policy.finished_missions_days)
    operations_before = _cutoff(moment, policy.operations_days)
    missions: dict[str, dict[str, int]] = {}
    complete = True
    if finished_before is not None:
        # Disk evidence first: detaching a log reference is what lets the operation row go in the same pass.
        for mission in store.retention_missions(finished_before=finished_before):
            counts = prune_finished_artifacts(store, str(mission["id"]))
            if counts:
                missions.setdefault(str(mission["id"]), {}).update(counts)
            if counts.get("operation_logs", 0) >= ARTIFACT_BATCH or counts.get("verifier_files", 0) >= ARTIFACT_BATCH:
                complete = False
    database = store.prune_retention(
        events_before=events_before,
        finished_before=finished_before,
        operations_before=operations_before,
        chunk=chunk,
        budget_seconds=budget_seconds,
    )
    complete = complete and bool(database["complete"])
    for mission_id, counts in database["missions"].items():
        bucket = missions.setdefault(mission_id, {})
        for category, count in counts.items():
            bucket[category] = bucket.get(category, 0) + count
    deleted: dict[str, int] = {}
    for counts in missions.values():
        for category, count in counts.items():
            deleted[category] = deleted.get(category, 0) + count
    removed = store.prune_migration_backups(policy.migration_backups_keep)
    if removed:
        deleted["migration_backups"] = len(removed)
    return {
        "deleted": deleted,
        "missions": missions,
        "complete": complete,
        "cutoffs": {key: value for key, value in (
            ("events_before", events_before), ("finished_before", finished_before), ("operations_before", operations_before),
        ) if value is not None},
    }
