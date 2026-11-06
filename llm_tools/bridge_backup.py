"""Consistent backup and restore of the Bridge database (DATA-006).

``backup`` copies the live SQLite database with the SQLite online backup API,
so it is consistent while missions run, and checks the copy before it is
named. ``restore`` refuses while any mission controller runs, keeps the
current database beside the restored one, and raises the event id sequence to
at least its previous value: a coordinator that already replicated newer
events then never receives a reused event id (PROTO-005).

Worker enrolment (token and coordinator CA) lives in ``<state>/worker/`` and
is deliberately not part of a backup; re-enrol after restoring on a new host.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .bridge_store import SCHEMA_VERSION, BridgeStore, controller_running


class BackupError(ValueError):
    """A backup or restore was refused; nothing was changed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _inspect(path: Path) -> dict[str, Any]:
    """Integrity, schema version and size facts of a database file, opened read-only."""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupError(f"{path} is not a readable SQLite database: {exc}") from None
    try:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise BackupError(f"{path} failed the SQLite integrity check: {str(integrity)[:200]}")
        version = int(db.execute("PRAGMA user_version").fetchone()[0])
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"missions", "tasks", "events", "commands"} <= tables:
            raise BackupError(f"{path} is not a Bridge database")
        missions = int(db.execute("SELECT COUNT(*) FROM missions").fetchone()[0])
        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        last_event = int(db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{path} is not a readable SQLite database: {exc}") from None
    finally:
        db.close()
    return {"schema_version": version, "missions": missions, "last_event_id": last_event,
            "event_sequence": max(last_event, int(sequence[0]) if sequence else 0)}


def _copy_database(source: sqlite3.Connection, destination: Path) -> None:
    """Write ``source`` to ``destination`` atomically with mode 0600."""
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".bridge-backup-")
    os.close(descriptor)
    os.chmod(temporary, 0o600)
    try:
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")  # a single self-contained file
        finally:
            target.close()
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def backup(output: Path, *, store_path: Path) -> dict[str, Any]:
    """Write a consistent copy of the database to ``output``; never overwrites."""
    output = output.expanduser().absolute()
    if output.exists():
        raise BackupError(f"{output} already exists; choose a new file name")
    if not output.parent.is_dir():
        raise BackupError(f"folder {output.parent} does not exist")
    with BridgeStore(store_path) as store:  # creates or migrates, so the copy has the current schema
        _copy_database(store.db, output)
    facts = _inspect(output)
    return {"path": str(output), "bytes": output.stat().st_size, "sha256": _sha256(output), **facts}


def restore(source: Path, *, store_path: Path, mission_ids: Callable[[], list[str]] | None = None,
            running: Callable[[str], bool | None] = controller_running) -> dict[str, Any]:
    """Replace the database with ``source``; the replaced one is kept as ``*.before-restore-<time>``."""
    source = source.expanduser().absolute()
    if not source.is_file():
        raise BackupError(f"{source} does not exist")
    facts = _inspect(source)
    if facts["schema_version"] > SCHEMA_VERSION:
        raise BackupError(f"{source} has schema version {facts['schema_version']}; this Bridge supports up to {SCHEMA_VERSION}. Upgrade Bridge first")
    store_path = store_path.expanduser().absolute()
    previous_sequence = 0
    kept: Path | None = None
    if store_path.exists():
        with BridgeStore(store_path) as store:
            ids = mission_ids() if mission_ids is not None else [row["id"] for row in store.list_missions()]
            busy = [mission_id for mission_id in ids if running(mission_id)]
            if busy:
                raise BackupError(f"stop the running mission controllers first ({', '.join(item[:8] for item in busy)}); llm-bridge stop --mission ID")
            sequence = store.db.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
            last = store.db.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            previous_sequence = max(int(last), int(sequence[0]) if sequence else 0)
            kept = store_path.with_name(f"{store_path.name}.before-restore-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
            _copy_database(store.db, kept)
    store_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        _copy_database(reader, store_path)
    finally:
        reader.close()
    for suffix in ("-wal", "-shm"):
        Path(str(store_path) + suffix).unlink(missing_ok=True)
    with BridgeStore(store_path) as store:  # migrates an older backup to the current schema
        sequence = max(previous_sequence, facts["event_sequence"])
        with store._transaction():
            store.db.execute("DELETE FROM sqlite_sequence WHERE name='events'")
            store.db.execute("INSERT INTO sqlite_sequence(name, seq) VALUES('events', ?)", (sequence,))
    return {"restored_from": str(source), "previous_database": str(kept) if kept else None,
            "event_sequence": sequence, **{key: facts[key] for key in ("schema_version", "missions", "last_event_id")}}
