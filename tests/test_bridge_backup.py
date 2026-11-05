"""Backup and restore of the Bridge database (DATA-006)."""

from __future__ import annotations

import json
import sqlite3
import stat

import pytest

from llm_tools import bridge_backup
from llm_tools.bridge import main
from llm_tools.bridge_store import SCHEMA_VERSION, BridgeStore


@pytest.fixture
def state(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    return directory, mission_id


def _store(directory):
    return BridgeStore(directory / "bridge.sqlite3")


def test_backup_then_restore_returns_to_the_snapshot_and_never_reuses_event_ids(state, tmp_path, capsys):
    directory, mission_id = state
    copy = tmp_path / "bridge-backup.sqlite3"
    with _store(directory) as store, store._transaction():
        store.db.execute("SELECT 1")  # an open writer on another connection does not block a consistent copy
        assert main(["backup", "--output", str(copy), "--json"]) == 0
    facts = json.loads(capsys.readouterr().out)
    assert facts["missions"] == 1 and facts["schema_version"] == SCHEMA_VERSION and len(facts["sha256"]) == 64
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    with _store(directory) as store:
        store.add_task(mission_id, "added after the backup")
        before = store.last_event_id()
    assert main(["restore", "--input", str(copy)]) == 0
    out = capsys.readouterr().out
    assert "Restored 1 missions" in out and "before-restore-" in out
    with _store(directory) as store:
        assert [task["original_prompt"] for task in store.list_tasks(mission_id)] == ["Objective\n"]
        store.add_task(mission_id, "after restore")
        assert store.last_event_id() > before  # replicated ids from the lost timeline are not reused
    kept = list(directory.glob("bridge.sqlite3.before-restore-*"))
    assert len(kept) == 1
    with sqlite3.connect(kept[0]) as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    assert main(["backup", "--output", str(copy)]) == 2
    assert "already exists" in capsys.readouterr().err
    assert main(["backup", "--output", str(tmp_path / "missing" / "x")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_restore_refusals_leave_the_database_untouched(state, tmp_path, capsys):
    directory, mission_id = state
    copy = tmp_path / "copy.sqlite3"
    bridge_backup.backup(copy, store_path=directory / "bridge.sqlite3")
    with pytest.raises(bridge_backup.BackupError, match="stop the running mission controllers"):
        bridge_backup.restore(copy, store_path=directory / "bridge.sqlite3", running=lambda mission: True)
    garbage = tmp_path / "garbage"
    garbage.write_bytes(b"not a database" * 100)
    other = tmp_path / "other.sqlite3"
    with sqlite3.connect(other) as db:
        db.execute("CREATE TABLE unrelated(x)")
    newer = tmp_path / "newer.sqlite3"
    newer.write_bytes(copy.read_bytes())
    with sqlite3.connect(newer) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    for path, message in [(tmp_path / "absent", "does not exist"), (garbage, "not a readable SQLite"), (other, "not a Bridge database"), (newer, "Upgrade Bridge")]:
        assert main(["restore", "--input", str(path)]) == 2, path
        assert message in capsys.readouterr().err
    assert not list(directory.glob("*.before-restore-*"))
    with _store(directory) as store:
        assert store.get_mission(mission_id)["status"] == "CREATED"


def test_corrupt_backup_fails_the_integrity_check(state, tmp_path, monkeypatch):
    directory, _ = state
    copy = tmp_path / "copy.sqlite3"
    bridge_backup.backup(copy, store_path=directory / "bridge.sqlite3")
    real = sqlite3.connect

    class Checked:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            if sql == "PRAGMA integrity_check":
                return self.db.execute("SELECT '*** in database main *** Page 3: btreeInitPage() returns error code 11'")
            return self.db.execute(sql, *args)

        def close(self):
            self.db.close()

    monkeypatch.setattr(bridge_backup.sqlite3, "connect", lambda *a, **k: Checked(real(*a, **k)))
    with pytest.raises(bridge_backup.BackupError, match="integrity check"):
        bridge_backup._inspect(copy)


def test_restore_onto_a_new_host_without_a_database(state, tmp_path, monkeypatch, capsys):
    directory, mission_id = state
    copy = tmp_path / "copy.sqlite3"
    bridge_backup.backup(copy, store_path=directory / "bridge.sqlite3")
    fresh = tmp_path / "fresh-state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(fresh))
    assert main(["restore", "--input", str(copy), "--json"]) == 0
    facts = json.loads(capsys.readouterr().out)
    assert facts["previous_database"] is None and facts["missions"] == 1
    with _store(fresh) as store:
        assert store.get_mission(mission_id)["id"] == mission_id
    assert not (fresh / "worker").exists()  # enrolment is never part of a backup
