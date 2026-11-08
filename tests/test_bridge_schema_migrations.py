"""Forward-only schema migrations: fixture databases of every past version, backups, and the downgrade refusal.

The fixtures in ``tests/fixtures/bridge-schema/`` are built by ``scripts/make_bridge_schema_fixtures.py``
(MANIFEST.json records how each one was obtained). Every test copies a fixture before opening it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge, bridge_store
from llm_tools.bridge_store import EVENT_SCHEMA_VERSION, SCHEMA_VERSION, BridgeStore, SchemaTooNew

FIXTURES = Path(__file__).parent / "fixtures" / "bridge-schema"
MISSION_ID = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))["mission_id"]
PAST_VERSIONS = list(range(1, SCHEMA_VERSION))
TABLES = ["missions", "tasks", "task_dependencies", "amendments", "operations", "events", "commands"]


def _private(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory


def _install(tmp_path: Path, version: int) -> Path:
    target = _private(tmp_path) / "bridge.sqlite3"
    shutil.copyfile(FIXTURES / f"schema-v{version}.sqlite3", target)
    return target


def _rows(path: Path, columns_from: Path | None = None) -> dict[str, list[tuple[Any, ...]]]:
    """Every table's rows, restricted to the columns the table of ``columns_from`` (default: this file) has."""
    with sqlite3.connect(path) as db, sqlite3.connect(columns_from or path) as reference:
        out: dict[str, list[tuple[Any, ...]]] = {}
        for table in TABLES:
            names = [row[1] for row in reference.execute(f"PRAGMA table_info({table})")]
            have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            names = [name for name in names if name in have]
            out[table] = db.execute(f"SELECT {','.join(names)} FROM {table} ORDER BY rowid").fetchall()
        return out


def _schema(path: Path) -> dict[tuple[str, str], Any]:
    """sqlite_master reduced to what the code can observe.

    Tables compare by column name, type, NOT NULL, default and primary key plus foreign keys (column
    position is not observable: every query names its columns, and ALTER TABLE can only append).
    Indexes and triggers compare by their whitespace-normalised SQL.
    ``events.schema_version`` keeps its original default in a migrated database because SQLite cannot
    change a default in place; ``BridgeStore._event`` therefore writes the value explicitly
    (``test_events_written_after_a_migration_carry_the_current_event_schema_version``).
    """
    with sqlite3.connect(path) as db:
        out: dict[tuple[str, str], Any] = {}
        for kind, name, sql in db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"):
            if kind == "table":
                columns = {row[1]: (row[2], row[3], row[4], row[5]) for row in db.execute(f"PRAGMA table_info({name})")}
                if name == "events":
                    columns["schema_version"] = columns["schema_version"][:2] + ("<default not comparable>",) + columns["schema_version"][3:]
                out[(kind, name)] = (sorted(columns.items()), sorted(db.execute(f"PRAGMA foreign_key_list({name})").fetchall()))
            else:
                out[(kind, name)] = " ".join((sql or "").split())
        return out


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def _backups(directory: Path) -> list[Path]:
    return sorted(directory.glob("bridge.sqlite3.v*.bak"))


@pytest.fixture(scope="module")
def fresh_schema(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], Any]:
    directory = tmp_path_factory.mktemp("fresh")
    directory.chmod(0o700)
    BridgeStore(directory / "bridge.sqlite3").close()
    return _schema(directory / "bridge.sqlite3")


def test_every_past_version_has_a_fixture() -> None:
    assert sorted(path.name for path in FIXTURES.glob("schema-v*.sqlite3")) == sorted(f"schema-v{v}.sqlite3" for v in PAST_VERSIONS)
    for version in PAST_VERSIONS:
        assert _user_version(FIXTURES / f"schema-v{version}.sqlite3") == version


@pytest.mark.parametrize("version", PAST_VERSIONS)
def test_a_fixture_migrates_to_the_current_schema_and_keeps_its_data(tmp_path: Path, version: int, fresh_schema: dict[tuple[str, str], Any]) -> None:
    path = _install(tmp_path, version)
    before = _rows(path)
    assert before["tasks"], "the fixture must hold rows"

    store = BridgeStore(path)
    store.close()

    assert _user_version(path) == SCHEMA_VERSION
    assert _schema(path) == fresh_schema
    # Every value the old database held is still there; columns added later hold their defaults/backfills.
    assert _rows(path, columns_from=FIXTURES / f"schema-v{version}.sqlite3") == before
    with sqlite3.connect(path) as db:
        assert [row[0] for row in db.execute("SELECT ordinal FROM tasks ORDER BY id")] == [1, 2, 3]
        assert db.execute("SELECT original_prompt FROM tasks WHERE id=1").fetchone()[0] == "Do the first thing"
        counts = dict(((scope, key), n) for scope, key, n in db.execute("SELECT scope,key,n FROM mission_task_counts WHERE mission_id=?", (MISSION_ID,)))
        queued = db.execute("SELECT COUNT(*) FROM tasks WHERE status='QUEUED' AND (origin='operator' OR proposal_status='ACCEPTED')").fetchone()[0]
        assert counts[("all", "total")] == 3 and counts[("status", "COMPLETED")] == 1 and counts[("status", "QUEUED")] == queued >= 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    reopened = BridgeStore(path)  # a second open of the migrated database must neither migrate nor back up
    assert reopened.migration_backup is None
    reopened.close()
    assert len(_backups(path.parent)) == 1


@pytest.mark.parametrize("version", PAST_VERSIONS)
def test_a_migration_takes_a_private_backup_first(tmp_path: Path, version: int) -> None:
    path = _install(tmp_path, version)
    original = hashlib.sha256(path.read_bytes()).hexdigest()

    store = BridgeStore(path)
    backup = store.migration_backup
    store.close()

    assert backup is not None and backup.parent == path.parent
    assert backup.name.startswith(f"bridge.sqlite3.v{version}-") and backup.name.endswith("Z.bak")
    stamp = backup.name[len(f"bridge.sqlite3.v{version}-"):-len(".bak")]
    assert datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert _backups(path.parent) == [backup]
    assert [p.name for p in path.parent.glob("*.bak-*")] == []  # no stray -wal/-shm/-journal next to the copy
    assert _user_version(backup) == version
    assert _rows(backup) == _rows(FIXTURES / f"schema-v{version}.sqlite3")
    assert hashlib.sha256(backup.read_bytes()).hexdigest() != original or version == SCHEMA_VERSION  # a valid copy, not the fixture file itself
    log = (path.parent / "migrations.log").read_text(encoding="utf-8")
    assert f"schema {version} -> {SCHEMA_VERSION}; backup {backup}" in log
    assert stat.S_IMODE((path.parent / "migrations.log").stat().st_mode) == 0o600


def test_a_new_database_and_a_current_database_are_not_backed_up(tmp_path: Path) -> None:
    path = _private(tmp_path) / "bridge.sqlite3"
    first = BridgeStore(path)
    assert first.migration_backup is None
    first.close()
    BridgeStore(path).close()
    BridgeStore(path, readonly=True).close()
    assert _backups(path.parent) == []
    assert not (path.parent / "migrations.log").exists()


def test_a_read_only_open_of_a_current_database_changes_nothing(tmp_path: Path) -> None:
    path = _install(tmp_path, SCHEMA_VERSION - 1)
    BridgeStore(path).close()
    snapshot = {p.name: p.read_bytes() for p in path.parent.iterdir() if not p.name.endswith(("-wal", "-shm"))}
    reader = BridgeStore(path, readonly=True)
    assert reader.readonly and reader.migration_backup is None
    reader.close()
    assert {p.name: p.read_bytes() for p in path.parent.iterdir() if not p.name.endswith(("-wal", "-shm"))} == snapshot


def test_events_written_after_a_migration_carry_the_current_event_schema_version(tmp_path: Path) -> None:
    path = _install(tmp_path, 6)  # events.schema_version defaults to 6 in this database
    store = BridgeStore(path)
    store.add_task(MISSION_ID, "new work after the migration")
    store.close()
    with sqlite3.connect(path) as db:
        newest = db.execute("SELECT schema_version FROM events ORDER BY id DESC LIMIT 1").fetchone()[0]
        old = db.execute("SELECT schema_version FROM events WHERE id=1").fetchone()[0]
    assert newest == EVENT_SCHEMA_VERSION
    assert old == 6  # existing rows are not rewritten


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return datetime(2030, 1, 2, 3, 4, 5, tzinfo=tz)


def test_a_retried_migration_in_the_same_second_keeps_both_backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_store, "datetime", _FrozenDatetime)
    path = _install(tmp_path, SCHEMA_VERSION - 1)
    real_migrate = BridgeStore._migrate
    calls: list[int] = []

    def failing_once(self: BridgeStore, current_version: int) -> None:
        calls.append(current_version)
        if len(calls) == 1:
            raise RuntimeError("disk full")
        real_migrate(self, current_version)

    monkeypatch.setattr(BridgeStore, "_migrate", failing_once)
    with pytest.raises(RuntimeError, match="disk full"):
        BridgeStore(path)
    assert _user_version(path) == SCHEMA_VERSION - 1  # the failed migration rolled back completely
    BridgeStore(path).close()
    assert _user_version(path) == SCHEMA_VERSION
    base = f"bridge.sqlite3.v{SCHEMA_VERSION - 1}-20300102T030405Z"
    assert sorted(p.name for p in _backups(path.parent)) == sorted([f"{base}.bak", f"{base}-1.bak"])


def _raise_to(path: Path, version: int) -> None:
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version={version}")


def test_a_newer_database_is_refused_and_names_the_backup(tmp_path: Path) -> None:
    path = _install(tmp_path, SCHEMA_VERSION - 1)
    BridgeStore(path).close()  # leaves bridge.sqlite3.v9-<stamp>.bak
    (backup,) = _backups(path.parent)
    older = path.with_name("bridge.sqlite3.v8-20200101T000000Z.bak")
    older.write_bytes(b"")
    newer_than_us = path.with_name(f"bridge.sqlite3.v{SCHEMA_VERSION + 1}-20990101T000000Z.bak")  # made by an even newer llm-bridge
    newer_than_us.write_bytes(b"")
    _raise_to(path, SCHEMA_VERSION + 2)

    for readonly in (False, True):
        with pytest.raises(SchemaTooNew) as raised:
            BridgeStore(path, readonly=readonly)
        message = str(raised.value)
        assert f"this Bridge database is schema {SCHEMA_VERSION + 2} but this llm-bridge understands up to {SCHEMA_VERSION}" in message
        assert f"upgrade llm-tools, or restore the backup {backup} made before the upgrade" in message
        assert "v8-" not in message and f"v{SCHEMA_VERSION + 1}-" not in message
    assert _user_version(path) == SCHEMA_VERSION + 2  # refusing must not touch the database


def test_a_newer_database_without_a_backup_says_so(tmp_path: Path) -> None:
    path = _private(tmp_path) / "bridge.sqlite3"
    BridgeStore(path).close()
    _raise_to(path, SCHEMA_VERSION + 1)
    with pytest.raises(SchemaTooNew, match=r"upgrade llm-tools \(no backup of .*bridge\.sqlite3 was found next to it\)"):
        BridgeStore(path)


def test_downgrading_after_an_upgrade_refuses_and_names_the_upgrade_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _install(tmp_path, 6)
    BridgeStore(path).close()  # this release: 6 -> 10
    monkeypatch.setattr(bridge_store, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    upgraded = BridgeStore(path)  # a later release: 10 -> 11
    upgrade_backup = upgraded.migration_backup
    upgraded.close()
    monkeypatch.undo()  # the operator goes back to this release

    assert upgrade_backup is not None and upgrade_backup.name.startswith(f"bridge.sqlite3.v{SCHEMA_VERSION}-")
    with pytest.raises(SchemaTooNew, match=f"schema {SCHEMA_VERSION + 1} .*restore the backup {upgrade_backup}"):
        BridgeStore(path)


def test_the_cli_exits_with_status_4_and_names_the_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    path = _install(tmp_path, SCHEMA_VERSION - 1)
    BridgeStore(path).close()
    (backup,) = _backups(path.parent)
    _raise_to(path, SCHEMA_VERSION + 2)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(path.parent))
    monkeypatch.setenv("LLM_BRIDGE_NO_SIGNIN_CHECK", "1")

    assert bridge.main(["mission", "list"]) == bridge.EXIT_WRONG_STATE == 4
    error = capsys.readouterr().err
    assert error.startswith("llm-bridge: this Bridge database is schema ") and str(backup) in error

    assert bridge.main(["mission", "list", "--json"]) == 4
    payload = json.loads(capsys.readouterr().out)["error"]
    assert payload["code"] == "schema-too-new" and str(backup) in payload["message"]
