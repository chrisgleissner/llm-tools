#!/usr/bin/env python3
"""Rebuild tests/fixtures/bridge-schema/: one small Bridge database per past schema version.

Each fixture holds the same representative content (one stopped mission, three tasks with a
dependency, an amendment, one operation, three events, a queued command), written with raw SQL
that keeps only the columns the version's tables have. The databases are committed, so the tests
do not need git history or this script; run it only to regenerate them:

    python scripts/make_bridge_schema_fixtures.py

How each version is obtained (also recorded in MANIFEST.json next to the databases):

* ``history``: the schema is created by the ``BridgeStore`` of that version, loaded from
  ``git show <commit>:llm_tools/bridge_store.py``. Versions 2, 3 and 6 were committed.
* ``derived``: versions 7, 8 and 9 were never committed on their own (the history jumps from
  6 to 10 in the 1.3.0 release commit). They are the current schema with the objects that
  ``BridgeStore._migrate`` adds for later versions dropped again.
* ``stamped``: versions 1, 4 and 5 left no source either. They are the nearest committed
  schema stamped with that ``user_version``: the migration is idempotent per object, so an
  older-looking or newer-looking schema stamped with the number exercises the same code.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "bridge-schema"
STORE_SOURCE = "llm_tools/bridge_store.py"
COMMITS = {2: "219b5f0", 3: "0ace697", 6: "b11b719"}
# version -> (kind, base version the schema is taken from)
PLAN: dict[int, tuple[str, int]] = {
    1: ("stamped", 2),
    2: ("history", 2),
    3: ("history", 3),
    4: ("stamped", 3),
    5: ("stamped", 6),
    6: ("history", 6),
    7: ("derived", 10),
    8: ("derived", 10),
    9: ("derived", 10),
}
# Objects BridgeStore._migrate creates for each version above 7; a derived fixture drops those of later versions.
ADDED_IN = {
    8: ["DROP INDEX IF EXISTS events_task_id", "DROP INDEX IF EXISTS events_operation_id"],
    9: [
        "DROP TRIGGER IF EXISTS tasks_counts_insert",
        "DROP TRIGGER IF EXISTS tasks_counts_update",
        "DROP TRIGGER IF EXISTS tasks_counts_delete",
        "DROP INDEX IF EXISTS tasks_mission_page",
        "DROP INDEX IF EXISTS tasks_mission_ordering",
        "DROP TABLE IF EXISTS mission_task_counts",
    ],
    10: ["DROP INDEX IF EXISTS tasks_current", "DROP INDEX IF EXISTS operations_mission_recent"],
}

MISSION_ID = "0000fixture0000000000000000000001"
CWD = "/nonexistent/bridge-schema-fixture"
STAMP = "2026-01-01T00:00:00+00:00"

CONTENT: dict[str, list[dict[str, Any]]] = {
    "missions": [{
        "id": MISSION_ID, "name": "fixture-mission", "objective": "Keep every row across migrations",
        "cwd": CWD, "repo_identity": CWD, "config_json": json.dumps({"providers": "codex,claude"}),
        "status": "STOPPED", "created_at": STAMP, "updated_at": STAMP,
    }],
    "tasks": [
        {"id": 1, "mission_id": MISSION_ID, "title": "first task", "original_prompt": "Do the first thing", "prompt": "Do the first thing",
         "priority": 2, "ordering": 10, "ordinal": 1, "status": "COMPLETED", "revision": 1,
         "acceptance_json": json.dumps(["tests pass", "docs updated"]),
         "evidence_json": json.dumps([{"kind": "verifier", "exit_code": 0, "sha256": "ab" * 32}]),
         "origin": "operator", "proposal_status": "NONE", "created_at": STAMP, "updated_at": STAMP},
        {"id": 2, "mission_id": MISSION_ID, "title": "second task", "original_prompt": "Do the second thing", "prompt": "Do the second thing",
         "priority": 1, "ordering": 20, "ordinal": 2, "status": "QUEUED", "revision": 2,
         "acceptance_json": json.dumps(["it works"]), "evidence_json": "[]",
         "origin": "operator", "proposal_status": "NONE", "created_at": STAMP, "updated_at": STAMP},
        {"id": 3, "mission_id": MISSION_ID, "title": "proposed follow-up", "original_prompt": "Follow up", "prompt": "Follow up",
         "priority": 0, "ordering": 30, "ordinal": 3, "status": "QUEUED", "revision": 1,
         "acceptance_json": json.dumps(["checked"]), "evidence_json": "[]",
         "origin": "agent-proposal", "proposal_status": "PENDING", "created_at": STAMP, "updated_at": STAMP},
    ],
    "task_dependencies": [{"task_id": 2, "depends_on": 1}],
    "amendments": [{"id": 1, "task_id": 2, "revision": 2, "changes_json": json.dumps({"prompt": "Do the second thing"}),
                    "status": "APPLIED", "created_at": STAMP, "applied_at": STAMP}],
    "operations": [{"id": "op-fixture-1", "mission_id": MISSION_ID, "task_id": 1, "iteration": 1, "station": "native",
                    "route_id": "codex", "model": "gpt-5", "pane_id": "%1", "process_pid": 4242, "process_start": "123456",
                    "status": "COMPLETED", "exit_code": 0, "logs_path": "/nonexistent/logs/op-fixture-1",
                    "native_session_id": "sess-1", "restoration_state": "resumable", "created_at": STAMP, "updated_at": STAMP}],
    "events": [
        {"id": 1, "mission_id": MISSION_ID, "task_id": None, "operation_id": None, "kind": "mission-created", "data_json": "{}", "created_at": STAMP},
        {"id": 2, "mission_id": MISSION_ID, "task_id": 1, "operation_id": "op-fixture-1", "kind": "operation-started", "data_json": json.dumps({"iteration": 1}), "created_at": STAMP},
        {"id": 3, "mission_id": MISSION_ID, "task_id": 1, "operation_id": "op-fixture-1", "kind": "task-completed", "data_json": json.dumps({"evidence": 1}), "created_at": STAMP},
    ],
    "commands": [{"id": "cmd-fixture-1", "mission_id": MISSION_ID, "payload_json": json.dumps({"action": "pause"}),
                  "status": "QUEUED", "result_json": None, "created_at": STAMP, "updated_at": STAMP}],
}
INSERT_ORDER = ["missions", "tasks", "task_dependencies", "amendments", "operations", "events", "commands"]


def git_show(commit: str) -> str:
    return subprocess.run(["git", "show", f"{commit}:{STORE_SOURCE}"], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def historic_store_class(version: int) -> Any:
    """The ``BridgeStore`` class committed at ``version``, loaded as a stand-alone module."""
    module = types.ModuleType(f"historic_bridge_store_v{version}")
    module.__dict__["__file__"] = f"<{COMMITS[version]}:{STORE_SOURCE}>"
    sys.modules[module.__name__] = module  # dataclasses and typing look the module up by name
    exec(compile(git_show(COMMITS[version]), module.__file__, "exec"), module.__dict__)
    assert module.SCHEMA_VERSION == version, (module.SCHEMA_VERSION, version)
    return module.BridgeStore


def insert_content(path: Path, event_schema_version: int) -> None:
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN")
        for table in INSERT_ORDER:
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for row in CONTENT[table]:
                values = {name: value for name, value in row.items() if name in columns}
                if table == "events" and "schema_version" in columns:
                    values["schema_version"] = event_schema_version
                names = ",".join(values)
                db.execute(f"INSERT INTO {table} ({names}) VALUES ({','.join('?' * len(values))})", list(values.values()))
        db.execute("COMMIT")
    finally:
        db.close()


def finish(path: Path, version: int, drops: list[str]) -> None:
    db = sqlite3.connect(path, isolation_level=None)
    try:
        for statement in drops:
            db.execute(statement)
        db.execute(f"PRAGMA user_version={version}")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("VACUUM")
    finally:
        db.close()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    path.chmod(0o644)


def build(version: int, kind: str, base: int, work: Path) -> dict[str, Any]:
    name = f"schema-v{version}.sqlite3"
    path = work / name  # BridgeStore insists on a private (0700) parent directory
    if base == 10:
        sys.path.insert(0, str(ROOT))
        from llm_tools.bridge_store import EVENT_SCHEMA_VERSION, SCHEMA_VERSION, BridgeStore

        assert SCHEMA_VERSION == 10, "derived fixtures are defined against schema 10; revisit PLAN before changing it"
        store_class, event_version = BridgeStore, EVENT_SCHEMA_VERSION
    else:
        store_class, event_version = historic_store_class(base), base
    store = store_class(path)
    store.close()
    insert_content(path, event_version)
    drops: list[str] = []
    if kind == "derived":
        for later in range(version + 1, 11):
            drops.extend(ADDED_IN[later])
    finish(path, version, drops)
    (OUT / name).write_bytes(path.read_bytes())
    (OUT / name).chmod(0o644)
    source = {"history": f"{COMMITS.get(base, '')}:{STORE_SOURCE}", "derived": f"schema 10 without the objects added for versions above {version}",
              "stamped": f"schema {base} (commit {COMMITS.get(base, 'current')}) stamped user_version={version}"}[kind]
    return {"version": version, "kind": kind, "source": source, "file": name}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        manifest = [build(version, kind, base, Path(scratch)) for version, (kind, base) in sorted(PLAN.items())]
    (OUT / "MANIFEST.json").write_text(json.dumps({"mission_id": MISSION_ID, "fixtures": manifest}, indent=2) + "\n", encoding="utf-8")
    for entry in manifest:
        print(entry["file"], entry["kind"], entry["source"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
