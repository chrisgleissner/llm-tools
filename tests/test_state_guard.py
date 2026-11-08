"""The session guard that reports test missions written to the operator's real Bridge state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests import state_guard


def _database(path: Path, missions: list[tuple[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS missions (id TEXT PRIMARY KEY, cwd TEXT NOT NULL)")
        connection.executemany("INSERT INTO missions VALUES (?, ?)", missions)
    return path


def test_real_state_databases_follow_the_cli_resolution(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path / "home"), "LLM_BRIDGE_STATE_DIR": str(tmp_path / "explicit")}
    assert state_guard.real_state_databases(environ) == [
        tmp_path / "explicit" / "bridge.sqlite3",
        tmp_path / "home" / ".local" / "state" / "llm-tools" / "llm-bridge" / "bridge.sqlite3",
    ]
    assert state_guard.real_state_databases({"XDG_STATE_HOME": str(tmp_path / "s")}) == [
        tmp_path / "s" / "llm-tools" / "llm-bridge" / "bridge.sqlite3"
    ]


def test_new_missions_from_temporary_folders_are_reported_and_others_are_not(tmp_path: Path) -> None:
    temp_root = tmp_path / "tmp"
    database = _database(tmp_path / "state" / "bridge.sqlite3", [("old-demo-mission", str(temp_root / "a"))])
    baseline = state_guard.snapshot([database, tmp_path / "missing.sqlite3"])
    assert baseline == {database: {"old-demo-mission"}}
    _database(database, [("new-demo-mission", str(temp_root / "tmp.x")), ("operator-mission", str(tmp_path / "src" / "project"))])

    leaked = state_guard.leaked_missions(baseline, [str(temp_root)])

    assert leaked == [f"{database}: mission new-demo (cwd {temp_root / 'tmp.x'})"]


def test_an_unreadable_database_is_skipped(tmp_path: Path) -> None:
    broken = tmp_path / "bridge.sqlite3"
    broken.write_text("not a database", encoding="utf-8")
    assert state_guard.snapshot([broken]) == {}
    assert state_guard.leaked_missions({broken: set()}, [str(tmp_path)]) == []
