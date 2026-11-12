"""ORCH-003 commit and push policy and ORCH-005 mission budgets.

Every repository is a throw-away one under ``tmp_path`` and every push remote is a bare repository under
``tmp_path``. Bridge state and the runtime directory are private temporary directories.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge_budget, bridge_publish, bridge_runtime
from llm_tools.bridge import main
from llm_tools.bridge_commands import CommandFailure, CommandRequest, dispatch
from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_runtime import BridgeMissionHooks, run_configured_verifier
from llm_tools.bridge_store import BridgeStore

PASSING = {"argv": [sys.executable, "-c", "import json; print(json.dumps({'passed': True, 'summary': 'all checks passed'}))"], "timeout_seconds": 20}
FAILING = {"argv": [sys.executable, "-c", "import json; print(json.dumps({'passed': False, 'summary': 'nope'}))"], "timeout_seconds": 20}


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    return directory


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", branch)
    git(path, "config", "user.name", "Repo Author")
    git(path, "config", "user.email", "author@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "first")
    return path


@pytest.fixture()
def repo(tmp_path) -> Path:
    return _init_repo(tmp_path / "repo")


@pytest.fixture()
def remote(tmp_path) -> Path:
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True)
    return path


def _mission(store: BridgeStore, cwd: Path, config: dict[str, Any], tasks: int = 1) -> tuple[str, list[int]]:
    mission = store.create_mission("mission", "objective", cwd=cwd, initial_prompt="first task", config={"providers": "codex", "routes": "", "verifier": PASSING, **config})
    ids = [int(mission["initial_task_id"])]
    for number in range(2, tasks + 1):
        ids.append(store.add_task(mission["id"], f"task {number}", title=f"Task {number}"))
    store.set_mission_status(mission["id"], "RUNNING")
    return mission["id"], ids


def _verify(store: BridgeStore, mission_id: str, task_id: int, tmp_path: Path, *, edit: tuple[str, str] | None = None) -> dict[str, Any]:
    """One provider run for the task (optionally changing a file), then the real verifier path."""
    if edit is not None:
        (Path(store.get_mission(mission_id)["cwd"]) / edit[0]).write_text(edit[1], encoding="utf-8")
    store.begin_iteration(task_id)
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation)
    store.finish_operation(operation, exit_code=0)
    return run_configured_verifier(store, mission_id, task_id, runtime=tmp_path / "runtime")


def _kinds(store: BridgeStore, mission_id: str, *kinds: str) -> list[dict[str, Any]]:
    return [event for event in store.events(mission_id) if event["kind"] in kinds]


def _with_remote(repo: Path, remote: Path, branch: str, *, upstream: bool = True) -> None:
    git(repo, "remote", "add", "origin", str(remote))
    if branch != "main":
        git(repo, "checkout", "-q", "-b", branch)
    git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    if upstream:
        git(repo, "fetch", "-q", "origin")
        git(repo, "branch", "-q", f"--set-upstream-to=origin/{branch}", branch)


# --- ORCH-003: commit -----------------------------------------------------------------------------------------


def test_commit_per_task_commits_after_the_verifier_passes(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task"})
        outcome = _verify(store, mission_id, task_id, tmp_path, edit=("result.txt", "work\n"))
        assert outcome["passed"] is True and outcome["committed"] is True
        assert git(repo, "log", "-1", "--format=%an <%ae>") == "Repo Author <author@example.invalid>"
        message = git(repo, "log", "-1", "--format=%B")
        assert message.startswith("Task 1: first task\n")
        assert f"Mission: {mission_id}" in message and "Verifier: all checks passed" in message
        assert git(repo, "show", "--name-only", "--format=", "HEAD") == "result.txt"
        events = _kinds(store, mission_id, "task-committed")
        assert len(events) == 1 and events[0]["data"]["committed"] is True and events[0]["data"]["sha"] == git(repo, "rev-parse", "HEAD")


def test_no_commit_without_the_setting_and_none_when_the_verifier_fails(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {})
        _verify(store, mission_id, task_id, tmp_path, edit=("a.txt", "a\n"))
        assert git(repo, "rev-list", "--count", "HEAD") == "1" and not _kinds(store, mission_id, "task-committed")
    with BridgeStore(tmp_path / "b2.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "verifier": FAILING})
        outcome = _verify(store, mission_id, task_id, tmp_path, edit=("b.txt", "b\n"))
        assert outcome["passed"] is False
        assert git(repo, "rev-list", "--count", "HEAD") == "1" and not _kinds(store, mission_id, "task-committed")


def test_commit_with_nothing_staged_is_not_an_error(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task"})
        outcome = _verify(store, mission_id, task_id, tmp_path)
        assert outcome["committed"] is True and git(repo, "rev-list", "--count", "HEAD") == "1"
        (event,) = _kinds(store, mission_id, "task-committed")
        assert event["data"]["committed"] is False
        assert store.get_mission(mission_id)["status"] == "RUNNING"


def test_commit_failure_holds_the_mission_with_an_event(tmp_path, repo) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho blocked by hook >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task"})
        outcome = _verify(store, mission_id, task_id, tmp_path, edit=("c.txt", "c\n"))
        assert outcome["committed"] is False
        mission = store.get_mission(mission_id)
        assert mission["status"] == "PAUSED" and mission["config"]["hold_reason"] == "commit-failed"
        assert "blocked by hook" in _kinds(store, mission_id, "task-commit-failed")[0]["data"]["reason"]
        assert store.get_task(task_id)["status"] == "COMPLETED"  # the verified result is kept


def test_commit_outside_a_repository_holds(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, plain, {"commit": "per-task"})
        _verify(store, mission_id, task_id, tmp_path)
        assert store.get_mission(mission_id)["config"]["hold_reason"] == "commit-failed"


def test_commit_in_an_isolated_mission_lands_on_the_worktree_branch(tmp_path, state, repo, capsys) -> None:
    assert main(["mission", "create", "--prompt", "Do it", "--name", "Isolated", "--cwd", str(repo), "--isolate", "worktree", "--commit", "per-task", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)["mission"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.get_mission(created["id"])
        assert mission["config"]["commit"] == "per-task" and mission["cwd"] != str(repo)
        store.set_mission_config_value(created["id"], "verifier", PASSING, event_kind="mission-verifier-changed")
        store.set_mission_status(created["id"], "RUNNING")
        _verify(store, created["id"], int(created["initial_task_id"]), tmp_path, edit=("w.txt", "w\n"))
        assert git(Path(mission["cwd"]), "rev-parse", "--abbrev-ref", "HEAD") == mission["branch"]
        assert git(Path(mission["cwd"]), "log", "-1", "--format=%s").startswith("Task 1")
    assert git(repo, "rev-list", "--count", "HEAD") == "1"  # the source checkout is untouched
    assert not (repo / "w.txt").exists()


def test_task_prompt_states_the_policy(tmp_path, repo, monkeypatch) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, _ = _mission(store, repo, {"commit": "per-task", "push": "per-mission"})
        hooks = BridgeMissionHooks(mission_id, handle=None, headless=True, native_tui=False, controller_logs=tmp_path / "c.log")
        hooks.bind(store)
        action = hooks.before_iteration()
        assert action["action"] == "run"
        assert "Bridge itself commits" in action["prompt"] and "last task has passed" in action["prompt"] and "never force-push" in action["prompt"]
    with BridgeStore(tmp_path / "b2.sqlite3") as store:
        mission_id, _ = _mission(store, repo, {})
        hooks = BridgeMissionHooks(mission_id, handle=None, headless=True, native_tui=False, controller_logs=tmp_path / "c.log")
        hooks.bind(store)
        assert "Version control policy" not in hooks.before_iteration()["prompt"]


def test_push_needs_commit_per_task(tmp_path, repo, state, capsys) -> None:
    assert main(["mission", "create", "--prompt", "x", "--cwd", str(repo), "--push", "per-task"]) != 0
    assert "push needs commit per-task" in capsys.readouterr().err


# --- ORCH-003: push -------------------------------------------------------------------------------------------


def test_push_per_task_pushes_to_the_configured_upstream(tmp_path, repo, remote) -> None:
    _with_remote(repo, remote, "feature")
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        assert _verify(store, mission_id, task_id, tmp_path, edit=("p.txt", "p\n"))["committed"] is True
        assert git(remote, "rev-parse", "refs/heads/feature") == git(repo, "rev-parse", "HEAD")
        (event,) = _kinds(store, mission_id, "task-pushed")
        assert event["data"]["remote"] == "origin" and event["data"]["branch"] == "feature" and event["data"]["scope"] == "task"
        assert store.get_mission(mission_id)["status"] == "RUNNING"


def test_push_never_does_not_push(tmp_path, repo, remote) -> None:
    _with_remote(repo, remote, "feature")
    before = git(remote, "rev-parse", "refs/heads/feature")
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "never"})
        _verify(store, mission_id, task_id, tmp_path, edit=("p.txt", "p\n"))
        assert git(remote, "rev-parse", "refs/heads/feature") == before and not _kinds(store, mission_id, "task-pushed")


def test_push_refuses_the_default_branch_unless_the_mission_was_created_for_it(tmp_path, repo, remote) -> None:
    _with_remote(repo, remote, "main")
    before = git(remote, "rev-parse", "refs/heads/main")
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        _verify(store, mission_id, task_id, tmp_path, edit=("d.txt", "d\n"))
        assert git(remote, "rev-parse", "refs/heads/main") == before
        mission = store.get_mission(mission_id)
        assert mission["status"] == "PAUSED" and mission["config"]["hold_reason"] == "push-refused"
        assert "default branch" in _kinds(store, mission_id, "push-failed")[0]["data"]["reason"]
    with BridgeStore(tmp_path / "b2.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task", "push_default_branch": True})
        _verify(store, mission_id, task_id, tmp_path, edit=("e.txt", "e\n"))
        assert git(remote, "rev-parse", "refs/heads/main") == git(repo, "rev-parse", "HEAD")


def test_push_without_a_configured_remote_is_refused_and_invents_none(tmp_path, repo, remote) -> None:
    git(repo, "remote", "add", "origin", str(remote))  # a remote exists, but nothing says to push this branch there
    git(repo, "checkout", "-q", "-b", "feature")
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        _verify(store, mission_id, task_id, tmp_path, edit=("n.txt", "n\n"))
        mission = store.get_mission(mission_id)
        assert mission["status"] == "PAUSED" and mission["config"]["hold_reason"] == "push-refused"
        assert "no upstream" in mission["config"]["hold_detail"]
    assert git(remote, "branch", "--list") == ""  # nothing reached the remote


def test_push_uses_the_configured_push_remote_for_a_new_branch(tmp_path, repo, remote) -> None:
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "checkout", "-q", "-b", "bridge/new-branch")
    git(repo, "config", "remote.pushDefault", "origin")
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        _verify(store, mission_id, task_id, tmp_path, edit=("n.txt", "n\n"))
        assert git(remote, "rev-parse", "refs/heads/bridge/new-branch") == git(repo, "rev-parse", "HEAD")


def test_push_failure_holds_the_mission(tmp_path, repo, remote) -> None:
    _with_remote(repo, remote, "feature")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho remote says no >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        outcome = _verify(store, mission_id, task_id, tmp_path, edit=("f.txt", "f\n"))
        assert outcome["committed"] is False
        mission = store.get_mission(mission_id)
        assert mission["status"] == "PAUSED" and mission["config"]["hold_reason"] == "push-failed"
        assert "remote says no" in _kinds(store, mission_id, "push-failed")[0]["data"]["reason"]
        assert _kinds(store, mission_id, "task-committed")  # the commit stays


def test_no_git_call_ever_forces(tmp_path, repo, remote, monkeypatch) -> None:
    _with_remote(repo, remote, "feature")
    calls: list[tuple[str, ...]] = []
    real = bridge_publish.git

    def recording(cwd, *args, **kwargs):
        calls.append(args)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(bridge_publish, "git", recording)
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        _verify(store, mission_id, task_id, tmp_path, edit=("g.txt", "g\n"))
    pushes = [call for call in calls if call and call[0] == "push"]
    assert len(pushes) == 1
    assert not any(arg in {"--force", "-f", "--force-with-lease", "--mirror", "--delete"} or arg.startswith(("+", "--force")) for call in calls for arg in call)


class _Sleeper:
    """Replaces ``time.sleep`` in the controller's wait loops; each call runs the next queued action."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.actions: list[Any] = []
        # Only the controller module sees the replaced sleep; subprocess keeps the real one.
        proxy = types.SimpleNamespace(**{name: getattr(time, name) for name in dir(time) if not name.startswith("__")})
        proxy.sleep = self
        monkeypatch.setattr(bridge_runtime, "time", proxy)

    def __call__(self, _seconds: float) -> None:
        assert self.actions, "the controller kept waiting with nothing left to release it"
        self.actions.pop(0)()


def test_push_per_mission_happens_once_after_the_last_task(tmp_path, repo, remote, monkeypatch) -> None:
    _with_remote(repo, remote, "feature")
    before = git(remote, "rev-parse", "refs/heads/feature")
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"commit": "per-task", "push": "per-mission"}, tasks=2)
        _verify(store, mission_id, first, tmp_path, edit=("one.txt", "1\n"))
        assert git(remote, "rev-parse", "refs/heads/feature") == before  # nothing pushed after the first task
        _verify(store, mission_id, second, tmp_path, edit=("two.txt", "2\n"))
        assert git(remote, "rev-parse", "refs/heads/feature") == before
        hooks = BridgeMissionHooks(mission_id, handle=None, headless=True, native_tui=False, controller_logs=tmp_path / "c.log")
        hooks.bind(store)
        action = hooks.before_iteration()
        assert action["action"] == "stop" and store.get_mission(mission_id)["status"] == "COMPLETED"
        assert git(remote, "rev-parse", "refs/heads/feature") == git(repo, "rev-parse", "HEAD")
        (event,) = _kinds(store, mission_id, "task-pushed")
        assert event["data"]["scope"] == "mission"


def test_failed_mission_push_keeps_the_mission_from_completing(tmp_path, repo, remote, monkeypatch) -> None:
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "checkout", "-q", "-b", "feature")  # no upstream, no push remote
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-mission"})
        _verify(store, mission_id, task_id, tmp_path, edit=("x.txt", "x\n"))
        sleeper = _Sleeper(monkeypatch)
        seen: list[str] = []

        def stop() -> None:
            seen.append(store.get_mission(mission_id)["status"])
            store.set_mission_status(mission_id, "STOPPING")

        sleeper.actions.append(stop)
        hooks = BridgeMissionHooks(mission_id, handle=None, headless=True, native_tui=False, controller_logs=tmp_path / "c.log")
        hooks.bind(store)
        assert hooks.before_iteration()["action"] == "stop"
        assert seen == ["PAUSED"] and store.get_mission(mission_id)["status"] == "STOPPING"
        assert store.get_task(task_id)["status"] == "COMPLETED"
        assert _kinds(store, mission_id, "push-failed")


# --- ORCH-005: budgets ----------------------------------------------------------------------------------------


def _operation(store: BridgeStore, task_id: int, station: str = "codex") -> None:
    store.begin_iteration(task_id)
    operation = store.begin_operation(task_id, iteration=1, station=station)
    store.mark_operation_running(operation)
    store.finish_operation(operation, exit_code=0)


def _boundary(store: BridgeStore, mission_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], str]:
    """Run the controller's loop once; the loop ends at the first wait with ``STOPPING``. Returns the action and the status seen while waiting."""
    seen: list[str] = []
    sleeper = _Sleeper(monkeypatch)

    def stop() -> None:
        seen.append(store.get_mission(mission_id)["status"])
        store.set_mission_status(mission_id, "STOPPING")

    sleeper.actions.append(stop)
    hooks = BridgeMissionHooks(mission_id, handle=None, headless=True, native_tui=False, controller_logs=tmp_path / "c.log")
    hooks.bind(store)
    return hooks.before_iteration(), (seen[0] if seen else "")


def test_runs_limit_holds_the_mission_with_budget_exhausted(tmp_path, repo, monkeypatch) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"budget": {"runs": 2}}, tasks=2)
        _operation(store, first)
        action, _ = _boundary(store, mission_id, tmp_path, monkeypatch)
        assert action["action"] == "run"  # one run used, the limit is two
    with BridgeStore(tmp_path / "b2.sqlite3") as store:
        mission_id, (first, second, third) = _mission(store, repo, {"budget": {"runs": 2}}, tasks=3)
        _operation(store, first)
        _operation(store, second)
        action, status_while_waiting = _boundary(store, mission_id, tmp_path, monkeypatch)
        assert action["action"] == "stop" and status_while_waiting == "PAUSED"
        mission = store.get_mission(mission_id)
        assert mission["config"]["hold_reason"] == "budget-exhausted"
        (event,) = _kinds(store, mission_id, "budget-exhausted")
        assert event["data"]["limits"] == ["runs"] and event["data"]["used"]["runs"] == 2
        assert len(store.db.execute("SELECT id FROM operations WHERE mission_id=?", (mission_id,)).fetchall()) == 2  # no third run started


def test_provider_change_limit(tmp_path, repo, monkeypatch) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second, third) = _mission(store, repo, {"budget": {"provider_changes": 1}}, tasks=3)
        for task_id, station in zip((first, second, third), ("codex", "codex", "claude")):
            _operation(store, task_id, station)
        hit = bridge_budget.check(store, store.get_mission(mission_id))
        assert hit and hit["limits"] == ["provider_changes"] and hit["used"]["provider_changes"] == 1


def test_wall_clock_limit(tmp_path, repo, monkeypatch) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"budget": {"wall_seconds": 600}}, tasks=2)
        _operation(store, first)
        mission = store.get_mission(mission_id)
        assert bridge_budget.check(store, mission) is None
        started = bridge_budget._epoch(store.db.execute("SELECT created_at FROM operations").fetchone()["created_at"])
        hit = bridge_budget.check(store, mission, now=started + 601)
        assert hit and hit["limits"] == ["wall_seconds"] and hit["used"]["wall_seconds"] == 601


def test_spend_limit_counts_only_the_budget_currency(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, _ = _mission(store, repo, {"budget": {"spend": 5.0, "currency": "USD"}})
        store.record_runtime_event(mission_id, "provider-spend", {"amount": 2.0, "currency": "USD"})
        store.record_runtime_event(mission_id, "provider-spend", {"amount": 100, "currency": "EUR"})  # another currency is not summed
        mission = store.get_mission(mission_id)
        assert bridge_budget.check(store, mission) is None
        store.record_runtime_event(mission_id, "provider-spend", {"amount": 3.0, "currency": "USD"})
        assert bridge_budget.check(store, mission)["limits"] == ["spend"]


def test_a_spend_limit_needs_a_currency() -> None:
    with pytest.raises(ValueError, match="needs a currency"):
        bridge_budget.validate_budget({"spend": 3})
    assert bridge_budget.validate_budget({"spend": 3, "currency": "usd"}) == {"spend": 3.0, "currency": "USD"}
    with pytest.raises(ValueError):
        bridge_budget.validate_budget({"runs": 0})
    with pytest.raises(ValueError):
        bridge_budget.validate_budget({"tokens": 5})


def test_raising_the_limit_releases_the_hold_and_resumes(tmp_path, state, repo, monkeypatch, capsys) -> None:
    assert main(["mission", "create", "--prompt", "obj", "--cwd", str(repo), "--providers", "codex", "--budget-runs", "1", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)["mission"]
    mission_id = created["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        second = store.add_task(mission_id, "second")
        store.set_mission_status(mission_id, "RUNNING")
        _operation(store, int(created["initial_task_id"]))
        action, seen = _boundary(store, mission_id, tmp_path, monkeypatch)
        assert action["action"] == "stop" and seen == "PAUSED"
        store.db.execute("UPDATE missions SET status='PAUSED' WHERE id=?", (mission_id,))  # the stop in _boundary ended the loop; the hold stays

    assert main(["status", "--mission", mission_id]) == 0
    text = capsys.readouterr().out
    assert "Budget      provider runs 1 of 1" in text and "Held        budget-exhausted" in text and "mission set" in text
    snapshot = json.loads(_status_json(capsys, mission_id))
    assert snapshot["budget"]["hold_reason"] == "budget-exhausted" and snapshot["budget"]["reached"] == ["runs"]

    assert main(["mission", "set", "--mission", mission_id, "--budget-runs", "1"]) == 0  # not raised: stays held
    assert "mission resumed" not in capsys.readouterr().out
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mission_id)["status"] == "PAUSED"
    assert main(["mission", "set", "--mission", mission_id, "--budget-runs", "3"]) == 0
    assert "mission resumed" in capsys.readouterr().out
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.get_mission(mission_id)
        assert mission["status"] == "RUNNING" and not mission["config"]["hold_reason"] and mission["config"]["budget"] == {"runs": 3}
        assert {"mission-budget-changed", "mission-hold-released", "budget-exhausted"} <= {event["kind"] for event in store.events(mission_id)}
        assert second


def _status_json(capsys, mission_id: str) -> str:
    assert main(["status", "--mission", mission_id, "--json"]) == 0
    return capsys.readouterr().out


def test_resuming_a_held_mission_without_raising_the_limit_holds_it_again(tmp_path, repo, monkeypatch) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"budget": {"runs": 1}}, tasks=2)
        _operation(store, first)
        _boundary(store, mission_id, tmp_path, monkeypatch)
        store.db.execute("UPDATE missions SET status='RUNNING' WHERE id=?", (mission_id,))  # the operator started it again
        _boundary(store, mission_id, tmp_path, monkeypatch)
        assert len(_kinds(store, mission_id, "budget-exhausted")) == 2


def test_settings_show_and_validate_through_the_typed_patch(tmp_path, state, repo, capsys) -> None:
    assert main(["mission", "create", "--prompt", "obj", "--cwd", str(repo), "--providers", "codex", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    assert main(["mission", "settings", "--mission", mission_id]) == 0
    assert "commit none · push never" in capsys.readouterr().out
    assert main(["mission", "set", "--mission", mission_id, "--commit", "per-task", "--push", "per-task", "--budget-minutes", "90", "--budget-spend", "4.5", "--budget-currency", "usd"]) == 0
    assert "Commit per-task · push per-task" in capsys.readouterr().out
    result = dispatch(CommandRequest("mission.settings.get", mission_id=mission_id))
    assert result["commit"] == "per-task" and result["push"] == "per-task"
    assert result["budget"] == {"wall_seconds": 5400, "spend": 4.5, "currency": "USD"}
    assert main(["mission", "settings", "--mission", mission_id]) == 0
    assert "commit per-task · push per-task · budget wall-clock time 90 min" in capsys.readouterr().out
    with pytest.raises(CommandFailure):  # a spend limit with no currency; nothing changes
        dispatch(CommandRequest("mission.settings.patch", {"budget_spend": 9.0, "budget_currency": None, "commit": "none"}, mission_id=mission_id))
    dispatch(CommandRequest("mission.settings.patch", {"budget_spend": 0, "push": "never"}, mission_id=mission_id))
    result = dispatch(CommandRequest("mission.settings.get", mission_id=mission_id))
    assert result["push"] == "never" and result["budget"] == {"wall_seconds": 5400}
    with pytest.raises(CommandFailure):
        dispatch(CommandRequest("mission.settings.patch", {"commit": "none", "push": "per-task"}, mission_id=mission_id))
    assert main(["mission", "create", "--prompt", "obj", "--cwd", str(repo), "--budget-spend", "3"]) != 0
    assert "needs a currency" in capsys.readouterr().err
    with pytest.raises(SystemExit):  # argparse rejects 0; none removes a limit
        main(["mission", "set", "--mission", mission_id, "--budget-runs", "0"])
    capsys.readouterr()
    assert main(["mission", "set", "--mission", mission_id, "--budget-minutes", "none"]) == 0
    assert dispatch(CommandRequest("mission.settings.get", mission_id=mission_id))["budget"] == {}
