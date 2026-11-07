"""PRD-010: ``[bridge.retention]`` configuration and the background data-retention pass.

All tests use a fixed clock (``NOW``) and seed rows directly, so nothing sleeps or reads real Bridge state.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge_events, bridge_retention, config
from llm_tools.bridge_refresh import CapacityRefreshWorker
from llm_tools.bridge_retention import RetentionPolicy
from llm_tools.bridge_store import BridgeStore
from llm_tools.bridge_vocab import EventKind

NOW = datetime(2027, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


@pytest.fixture(autouse=True)
def _clear_config_cache() -> None:
    config._cache.clear()


# ---------------------------------------------------------------- configuration


def _load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> dict[str, Any]:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(path))
    return config.load_config()


def test_retention_defaults_are_documented_values() -> None:
    policy = RetentionPolicy()
    assert (policy.events_days, policy.finished_missions_days, policy.operations_days, policy.migration_backups_keep) == (90, 365, 180, 3)
    assert bridge_retention.policy_from_config({}) == policy
    assert bridge_retention.policy_from_config({"bridge": {"teams": True}}) == policy


def test_retention_table_parses_and_overrides_only_the_given_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _load(monkeypatch, tmp_path, "[bridge.retention]\nevents_days = 30\nmigration_backups_keep = 5\noperations_days = 0\n")
    assert bridge_retention.policy_from_config(cfg) == RetentionPolicy(events_days=30, operations_days=0, migration_backups_keep=5)


@pytest.mark.parametrize("body, fragment", [
    ("[bridge.retention]\nevent_days = 30\n", "did you mean 'events_days'"),
    ("[bridge.retention]\nmigration_backup_keep = 2\n", "did you mean 'migration_backups_keep'"),
    ("[bridge.retention]\nzzz = 1\n", "unknown key 'zzz'"),
    ('[bridge.retention]\nevents_days = "90"\n', "must be a whole number"),
    ("[bridge.retention]\nevents_days = true\n", "must be a whole number"),
    ("[bridge.retention]\nevents_days = 1.5\n", "must be a whole number"),
    ("[bridge.retention]\nfinished_missions_days = -1\n", "between 0 (never prune) and 3650"),
    ("[bridge.retention]\noperations_days = 100000\n", "between 0 (never prune) and 3650"),
    ("[bridge.retention]\nmigration_backups_keep = 0\n", "between 1 and 1000"),
    ('[bridge]\nretention = "x"\n', "must be a table"),
])
def test_retention_table_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str, fragment: str) -> None:
    with pytest.raises(SystemExit) as raised:
        _load(monkeypatch, tmp_path, body)
    assert raised.value.code == 2
    assert fragment in capsys.readouterr().err


def test_every_event_kind_has_exactly_one_retention_class() -> None:
    summary, audit, diagnostic = bridge_events.AUDIT_SUMMARY_KINDS, bridge_events.AUDIT_RECORD_KINDS, bridge_events.DIAGNOSTIC_KINDS
    assert not (summary & audit) and not (summary & diagnostic) and not (audit & diagnostic)
    assert summary | audit | diagnostic == {str(kind) for kind in EventKind}
    assert bridge_events.retention_class("mission-created") == "summary"
    assert bridge_events.retention_class("operation-exited") == "audit"
    assert bridge_events.retention_class("repository-checkpoint") == "diagnostic"
    assert bridge_events.retention_class("kind-from-a-future-build") == "audit"


# ---------------------------------------------------------------- event pruning


def _insert(store: BridgeStore, mission_id: str, kind: str, days_ago: float, *, operation_id: str | None = None) -> None:
    with store._transaction():
        store.db.execute(
            "INSERT INTO events(mission_id,task_id,operation_id,kind,data_json,created_at) VALUES(?,?,?,?,?,?)",
            (mission_id, None, operation_id, kind, "{}", _stamp(days_ago)),
        )


def _kinds(store: BridgeStore, mission_id: str) -> dict[str, int]:
    return {row[0]: row[1] for row in store.db.execute("SELECT kind,COUNT(*) FROM events WHERE mission_id=? GROUP BY kind", (mission_id,))}


def _finish(store: BridgeStore, mission_id: str, *, days_ago: float, status: str = "COMPLETED") -> None:
    with store._transaction():
        store.db.execute("UPDATE missions SET status=?,updated_at=? WHERE id=?", (status, _stamp(days_ago), mission_id))


SEED = (
    ("controller-config", "diagnostic"),
    ("repository-checkpoint", "diagnostic"),
    ("operation-progress", "diagnostic"),
    ("task-added", "audit"),
    ("operation-exited", "audit"),
    ("task-verified", "summary"),
    ("mission-status", "summary"),
)


@pytest.fixture
def store(tmp_path: Path):
    with BridgeStore(tmp_path / "state" / "bridge.sqlite3") as opened:
        yield opened


def _seed_mission(store: BridgeStore, name: str, tmp_path: Path) -> str:
    folder = tmp_path / name
    folder.mkdir()
    mission_id = store.create_mission(name, "objective", cwd=folder)["id"]
    store.db.execute("DELETE FROM events WHERE mission_id=?", (mission_id,))
    for kind, _ in SEED:
        _insert(store, mission_id, kind, 200)   # older than events_days (90), newer than finished_missions_days (365)
        _insert(store, mission_id, kind, 10)    # recent
    return mission_id


def test_old_diagnostic_events_of_a_finished_mission_go_while_audit_events_stay(store: BridgeStore, tmp_path: Path) -> None:
    done = _seed_mission(store, "done", tmp_path)
    _finish(store, done, days_ago=150)
    result = bridge_retention.run_pass(store, policy=RetentionPolicy(), now=NOW)
    assert result["complete"] is True
    kept = _kinds(store, done)
    for kind, klass in SEED:
        # The recent copy always stays; the old copy stays unless it is diagnostic.
        assert kept[kind] == (1 if klass == "diagnostic" else 2), kind
    assert result["deleted"] == {"events": 3}
    assert result["missions"] == {done: {"events": 3}}
    assert result["cutoffs"]["events_before"] == _stamp(90)


@pytest.mark.parametrize("status", ["RUNNING", "PAUSED", "STOPPED", "FAILED", "CREATED"])
def test_a_mission_that_is_not_finished_loses_only_old_diagnostic_events(store: BridgeStore, tmp_path: Path, status: str) -> None:
    mission_id = _seed_mission(store, "open", tmp_path)
    _finish(store, mission_id, days_ago=1000, status=status)  # even a very old, unfinished mission
    result = bridge_retention.run_pass(store, policy=RetentionPolicy(), now=NOW)
    kept = _kinds(store, mission_id)
    for kind, klass in SEED:
        # Audit records and summaries keep both copies. A diagnostic kind keeps its newest copy.
        assert kept[kind] == (1 if klass == "diagnostic" else 2), kind
    assert result["deleted"] == {"events": 3}
    assert mission_id in result["missions"]


def test_the_newest_event_of_a_diagnostic_kind_survives_in_a_mission_that_is_not_finished(store: BridgeStore, tmp_path: Path) -> None:
    mission_id = _seed_mission(store, "only", tmp_path)
    _insert(store, mission_id, "controller-mode", 400)  # the only one of its kind, long ago: status still reads it
    bridge_retention.run_pass(store, policy=RetentionPolicy(), now=NOW)
    kept = _kinds(store, mission_id)
    assert kept["controller-mode"] == 1


def test_events_of_an_active_operation_survive_in_a_mission_that_is_not_finished(store: BridgeStore, tmp_path: Path) -> None:
    mission_id = _seed_mission(store, "busy-open", tmp_path)
    task = store.add_task(mission_id, "work")
    with store._transaction():
        store.db.execute(
            "INSERT INTO operations(id,mission_id,task_id,iteration,station,status,created_at,updated_at) VALUES('live',?,?,1,'s','RUNNING',?,?)",
            (mission_id, task, _stamp(300), _stamp(300)),
        )
    _insert(store, mission_id, "operation-progress", 300, operation_id="live")
    _insert(store, mission_id, "operation-progress", 300, operation_id="live")
    _insert(store, mission_id, "operation-progress", 299)  # same kind and age, no operation: goes
    before = _kinds(store, mission_id)["operation-progress"]
    bridge_retention.run_pass(store, policy=RetentionPolicy(), now=NOW)
    owned = store.db.execute("SELECT COUNT(*) FROM events WHERE mission_id=? AND operation_id='live'", (mission_id,)).fetchone()[0]
    assert owned == 2
    assert _kinds(store, mission_id)["operation-progress"] < before


def _refreshes(store: BridgeStore, mission_id: str, *, first_day: float, hours: int) -> None:
    """Capacity refreshes every 10 minutes for ``hours`` hours, the first ``first_day`` days ago."""
    for step in range(hours * 6):
        _insert(store, mission_id, "capacity-refresh", first_day - step * 10 / 1440)


def test_old_capacity_refreshes_of_an_unfinished_mission_are_thinned_to_one_per_hour(store: BridgeStore, tmp_path: Path) -> None:
    mission_id = store.create_mission("thin", "o", cwd=tmp_path)["id"]
    store.db.execute("DELETE FROM events WHERE mission_id=?", (mission_id,))
    _refreshes(store, mission_id, first_day=200, hours=5)  # 30 rows older than events_days
    _refreshes(store, mission_id, first_day=10, hours=2)   # 12 recent rows
    result = bridge_retention.run_pass(store, policy=RetentionPolicy(), now=NOW)
    old = store.db.execute("SELECT created_at FROM events WHERE mission_id=? AND kind='capacity-refresh' AND created_at<? ORDER BY id", (mission_id, _stamp(90))).fetchall()
    hours = [row[0][:13] for row in old]
    assert len(hours) == len(set(hours)) and 5 <= len(hours) <= 6  # one per hour (a span of 5 hours can touch 6 clock hours)
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE mission_id=? AND kind='capacity-refresh' AND created_at>=?", (mission_id, _stamp(90))).fetchone()[0] == 12
    assert result["missions"][mission_id]["capacity_refreshes"] == 30 - len(hours)
    again = bridge_retention.run_pass(store, policy=RetentionPolicy(), now=NOW)
    assert again["deleted"] == {}


def test_all_old_capacity_refreshes_of_a_finished_mission_go(store: BridgeStore, tmp_path: Path) -> None:
    mission_id = store.create_mission("done-thin", "o", cwd=tmp_path)["id"]
    store.db.execute("DELETE FROM events WHERE mission_id=?", (mission_id,))
    _refreshes(store, mission_id, first_day=200, hours=2)
    _refreshes(store, mission_id, first_day=10, hours=1)
    _finish(store, mission_id, days_ago=150)
    bridge_retention.run_pass(store, policy=RetentionPolicy(), now=NOW)
    assert _kinds(store, mission_id) == {"capacity-refresh": 6}


def test_thinning_respects_the_batch_bound(store: BridgeStore, tmp_path: Path) -> None:
    mission_id = store.create_mission("thin-bulk", "o", cwd=tmp_path)["id"]
    store.db.execute("DELETE FROM events WHERE mission_id=?", (mission_id,))
    _refreshes(store, mission_id, first_day=200, hours=10)  # 60 rows in 10-11 clock hours
    steps = []
    for _ in range(20):
        step = store.prune_retention(events_before=_stamp(90), chunk=10, budget_seconds=0)
        steps.append(step["missions"].get(mission_id, {}).get("capacity_refreshes", 0))
        if step["complete"]:
            break
    assert step["complete"] is True and max(steps) <= 10 and len(steps) > 1
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE mission_id=? AND kind='capacity-refresh'", (mission_id,)).fetchone()[0] <= 11


def test_a_finished_mission_with_an_unresolved_operation_is_not_touched(store: BridgeStore, tmp_path: Path) -> None:
    mission_id = _seed_mission(store, "busy", tmp_path)
    task = store.add_task(mission_id, "work")
    with store._transaction():
        store.db.execute(
            "INSERT INTO operations(id,mission_id,task_id,iteration,station,status,created_at,updated_at) VALUES('op1',?,?,1,'s','RUNNING',?,?)",
            (mission_id, task, _stamp(400), _stamp(400)),
        )
    _insert(store, mission_id, "operation-progress", 300, operation_id="op1")
    _finish(store, mission_id, days_ago=400)
    before = _kinds(store, mission_id)
    result = bridge_retention.run_pass(store, policy=RetentionPolicy(), now=NOW)
    assert result["deleted"] == {}
    assert _kinds(store, mission_id) == before


def test_finished_missions_older_than_a_year_keep_rows_and_summaries_and_lose_everything_else(store: BridgeStore, tmp_path: Path) -> None:
    mission_id = _seed_mission(store, "ancient", tmp_path)
    task = store.add_task(mission_id, "work")
    state = store.path.parent
    run_directory = tmp_path / "logs" / "20260101-000000-ralph-robin-abcd"
    run_directory.mkdir(parents=True)
    log = run_directory / "attempt-1.out"
    log.write_bytes(b"provider output\n")
    (run_directory / "prompt.txt").write_text("secret prompt", encoding="utf-8")
    verifier = state / "runtime" / f"mission-{mission_id}" / "verifier"
    verifier.mkdir(parents=True)
    (verifier / "1-r1-a0.json").write_text("{}", encoding="utf-8")
    (verifier / "1-r1-a0.log").write_text("ok", encoding="utf-8")
    with store._transaction():
        store.db.execute(
            "INSERT INTO operations(id,mission_id,task_id,iteration,station,status,exit_code,logs_path,created_at,updated_at) "
            "VALUES('old-op',?,?,1,'s','EXITED',0,?,?,?)",
            (mission_id, task, str(log), _stamp(500), _stamp(500)),
        )
    _finish(store, mission_id, days_ago=400)
    result = bridge_retention.run_pass(store, policy=RetentionPolicy(), now=NOW)
    assert result["complete"] is True
    counts = result["missions"][mission_id]
    assert counts["operation_logs"] == 1 and counts["verifier_files"] == 2
    assert counts["operations"] == 1 and counts["finished_mission_events"] > 0
    # Mission and task rows stay; only summary events (plus the new log-pruned digest) remain.
    assert store.get_mission(mission_id)["id"] == mission_id
    assert store.db.execute("SELECT COUNT(*) FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()[0] == 1
    assert set(_kinds(store, mission_id)) <= bridge_events.AUDIT_SUMMARY_KINDS
    pruned = store.db.execute("SELECT data_json FROM events WHERE mission_id=? AND kind='operation-log-pruned'", (mission_id,)).fetchone()
    assert json.loads(pruned[0])["size_bytes"] == len(b"provider output\n") and len(json.loads(pruned[0])["sha256"]) == 64
    # Disk evidence is gone, and so is the operation row (its log was detached in the same pass).
    assert not run_directory.exists() and not list(verifier.iterdir())
    assert store.db.execute("SELECT COUNT(*) FROM operations WHERE mission_id=?", (mission_id,)).fetchone()[0] == 0
    # A second pass has nothing left to do.
    assert bridge_retention.run_pass(store, policy=RetentionPolicy(), now=NOW)["deleted"] == {}


def test_operation_rows_are_kept_until_their_log_is_pruned_and_they_are_old_enough(store: BridgeStore, tmp_path: Path) -> None:
    mission_id = _seed_mission(store, "ops", tmp_path)
    task = store.add_task(mission_id, "work")
    log = tmp_path / "keep.out"
    log.write_text("x", encoding="utf-8")
    rows = (("logs-kept", str(log), 200), ("pruned-old", None, 200), ("pruned-recent", None, 20))
    with store._transaction():
        for op_id, logs_path, age in rows:
            store.db.execute(
                "INSERT INTO operations(id,mission_id,task_id,iteration,station,status,logs_path,created_at,updated_at) VALUES(?,?,?,1,'s','EXITED',?,?,?)",
                (op_id, mission_id, task, logs_path, _stamp(age), _stamp(age)),
            )
    _finish(store, mission_id, days_ago=100)
    result = bridge_retention.run_pass(store, policy=RetentionPolicy(), now=NOW)
    left = {row[0] for row in store.db.execute("SELECT id FROM operations WHERE mission_id=?", (mission_id,))}
    assert left == {"logs-kept", "pruned-recent"}
    assert result["deleted"].get("operations") == 1 and log.exists()


def test_a_zero_day_setting_turns_its_rule_off(store: BridgeStore, tmp_path: Path) -> None:
    mission_id = _seed_mission(store, "off", tmp_path)
    _finish(store, mission_id, days_ago=1000)
    before = _kinds(store, mission_id)
    policy = RetentionPolicy(events_days=0, finished_missions_days=0, operations_days=0)
    result = bridge_retention.run_pass(store, policy=policy, now=NOW)
    assert result["deleted"] == {} and _kinds(store, mission_id) == before and result["cutoffs"] == {}


def test_batch_bound_is_respected_and_repeated_passes_converge(store: BridgeStore, tmp_path: Path) -> None:
    mission_id = _seed_mission(store, "bulk", tmp_path)
    for _ in range(25):
        _insert(store, mission_id, "controller-config", 150)
    _finish(store, mission_id, days_ago=120)
    total = store.db.execute(
        "SELECT COUNT(*) FROM events WHERE mission_id=? AND kind IN ('controller-config','repository-checkpoint','operation-progress') AND created_at<?",
        (mission_id, _stamp(90)),
    ).fetchone()[0]
    assert total == 28
    first = store.prune_retention(events_before=_stamp(90), chunk=10, budget_seconds=0)
    assert first["complete"] is False
    assert first["missions"][mission_id]["events"] == 10  # one chunk, then the exhausted budget stops the pass
    totals = [first["missions"][mission_id]["events"]]
    for _ in range(10):
        step = store.prune_retention(events_before=_stamp(90), chunk=10, budget_seconds=0)
        totals.append(step["missions"].get(mission_id, {}).get("events", 0))
        if step["complete"]:
            break
    assert max(totals) <= 10 and sum(totals) == total
    assert step["complete"] is True


# ---------------------------------------------------------------- migration backups


def _make_backups(store: BridgeStore, count: int) -> list[Path]:
    paths = []
    for index in range(count):
        path = store.path.with_name(f"{store.path.name}.v{6 + index % 3}-2027010{index + 1}T000000Z.bak")
        path.write_bytes(b"x" * (index + 1))
        paths.append(path)
    return paths


def test_backups_beyond_the_newest_n_are_removed_with_a_log_line(store: BridgeStore) -> None:
    paths = _make_backups(store, 5)
    removed = store.prune_migration_backups(3)
    assert removed == paths[:2]
    assert [path.exists() for path in paths] == [False, False, True, True, True]
    lines = store.path.with_name("migrations.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all("pruned schema" in line for line in lines) and str(paths[0]) in lines[0]
    assert store.prune_migration_backups(3) == []  # nothing more to remove
    with pytest.raises(ValueError):
        store.prune_migration_backups(0)


def test_the_retention_pass_prunes_backups_per_policy_and_counts_them(store: BridgeStore) -> None:
    paths = _make_backups(store, 4)
    result = bridge_retention.run_pass(store, policy=RetentionPolicy(migration_backups_keep=1), now=NOW)
    assert result["deleted"] == {"migration_backups": 3}
    assert [path.exists() for path in paths] == [False, False, False, True]


# ---------------------------------------------------------------- the controller's background pass


def test_background_pass_records_one_audit_event_with_counts_per_category(store: BridgeStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    running = _seed_mission(store, "running", tmp_path)
    done = _seed_mission(store, "done", tmp_path)
    _finish(store, done, days_ago=150)
    _make_backups(store, 5)
    worker = CapacityRefreshWorker(running, store.path)
    monkeypatch.setattr(worker, "_retention_policy", lambda: RetentionPolicy())
    monkeypatch.setattr(bridge_retention, "datetime", type("Clock", (), {"now": staticmethod(lambda tz=None: NOW)}))
    assert worker._prune_pass() is True
    worker._discard_connection()
    events = [json.loads(row[0]) for row in store.db.execute("SELECT data_json FROM events WHERE mission_id=? AND kind='events-pruned'", (running,))]
    retention = [event for event in events if "retention" in event]
    assert len(retention) == 1
    # Three old diagnostic events in the finished mission and three in the running one (its newest of each kind stays).
    assert retention[0]["deleted"] == {"events": 6, "migration_backups": 2}
    assert retention[0]["total"] == 8
    assert retention[0]["retention"]["missions"] == {done: {"events": 3}, running: {"events": 3}}
    assert retention[0]["retention"]["cutoffs"]["events_before"] == _stamp(90)
    # The finished mission carries its own audit record of what it lost.
    mine = [json.loads(row[0]) for row in store.db.execute("SELECT data_json FROM events WHERE mission_id=? AND kind='events-pruned'", (done,))]
    assert [event["deleted"] for event in mine] == [{"events": 3}]
    # The running mission kept its audit records and summaries.
    assert _kinds(store, running)["operation-exited"] == 2 and _kinds(store, running)["task-verified"] == 2
    # A second pass finds nothing and records nothing new.
    assert worker._prune_pass() is True
    worker._discard_connection()
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE kind='events-pruned' AND data_json LIKE '%retention%'").fetchone()[0] == 2


def test_background_pass_falls_back_to_defaults_when_the_config_is_unreadable(store: BridgeStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mission_id = _seed_mission(store, "m", tmp_path)
    worker = CapacityRefreshWorker(mission_id, store.path)

    def broken() -> dict[str, Any]:
        raise SystemExit(2)

    monkeypatch.setattr(config, "load_config", broken)
    assert worker._retention_policy() == RetentionPolicy()


def test_routine_retention_is_audited_at_most_once_an_hour_and_flushed_on_stop(store: BridgeStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mission_id = _seed_mission(store, "long-running", tmp_path)
    worker = CapacityRefreshWorker(mission_id, store.path)
    monkeypatch.setattr(worker, "_retention_policy", lambda: RetentionPolicy())
    monkeypatch.setattr(bridge_retention, "datetime", type("Clock", (), {"now": staticmethod(lambda tz=None: NOW)}))
    assert worker._prune_pass() is True  # first routine pass is recorded
    _insert(store, mission_id, "repository-checkpoint", 100)
    _insert(store, mission_id, "repository-checkpoint", 50)
    assert worker._prune_pass() is True  # deletes the 100-day-old checkpoint, but an hour has not passed
    worker._discard_connection()

    def audits() -> list[dict[str, Any]]:
        rows = store.db.execute("SELECT data_json FROM events WHERE mission_id=? AND kind='events-pruned'", (mission_id,))
        return [json.loads(row[0]) for row in rows if "retention" in json.loads(row[0])]

    assert len(audits()) == 1 and audits()[0]["deleted"] == {"events": 3}
    worker.flush_retention_audit()
    flushed = audits()
    assert len(flushed) == 2 and flushed[1]["deleted"] == {"events": 1}
