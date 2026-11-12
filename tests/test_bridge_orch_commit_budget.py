"""ORCH-003 commit and push policy and ORCH-005 mission budgets.

Every repository is a throw-away one under ``tmp_path`` and every push remote is a bare repository under
``tmp_path``. Commit and push need an isolated mission, so these missions run in real worktrees of those repositories.
Bridge state and the runtime directory are private temporary directories.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import time
import types
import uuid
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge_budget, bridge_isolation, bridge_publish, bridge_runtime
from llm_tools.bridge import main
from llm_tools.bridge_commands import CommandFailure, CommandRequest, dispatch, execute
from llm_tools.bridge_refresh import CapacityRefreshWorker
from llm_tools.bridge_runtime import BridgeMissionHooks, run_configured_verifier
from llm_tools.bridge_store import BridgeStore
from tests.conftest import wait_for

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


def _bare(path: Path, default: str = "main") -> Path:
    subprocess.run(["git", "init", "-q", "--bare", "-b", default, str(path)], check=True)
    return path


@pytest.fixture()
def remote(tmp_path) -> Path:
    return _bare(tmp_path / "remote.git")


def _mission(store: BridgeStore, repo: Path, config: dict[str, Any], tasks: int = 1) -> tuple[str, list[int]]:
    """An isolated mission: a real worktree of ``repo`` on its own branch."""
    mission_id = uuid.uuid4().hex
    facts = bridge_isolation.prepare(repo, mission_id=mission_id, name="work", state_dir=repo.parent / "worktree-state")
    mission = store.create_mission("mission", "objective", cwd=facts["cwd"], mission_id=mission_id, initial_prompt="first task",
                                   config={"providers": "codex", "routes": "", "verifier": PASSING, **config}, isolation=facts)
    ids = [int(mission["initial_task_id"])]
    for number in range(2, tasks + 1):
        ids.append(store.add_task(mission_id, f"task {number}", title=f"Task {number}"))
    store.set_mission_status(mission_id, "RUNNING")
    return mission_id, ids


def _wt(store: BridgeStore, mission_id: str) -> Path:
    return Path(store.get_mission(mission_id)["worktree_path"])


def _verify(store: BridgeStore, mission_id: str, task_id: int, tmp_path: Path, *, edit: tuple[str, str] | None = None) -> dict[str, Any]:
    """One provider run for the task (optionally changing a file in the mission's folder), then the real verifier path."""
    if edit is not None:
        (Path(store.get_mission(mission_id)["cwd"]) / edit[0]).write_text(edit[1], encoding="utf-8")
    store.begin_iteration(task_id)
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation)
    store.finish_operation(operation, exit_code=0)
    return run_configured_verifier(store, mission_id, task_id, runtime=tmp_path / "runtime")


def _kinds(store: BridgeStore, mission_id: str, *kinds: str) -> list[dict[str, Any]]:
    return [event for event in store.events(mission_id) if event["kind"] in kinds]


def _with_remote(repo: Path, wt: Path, remote: Path, remote_branch: str) -> None:
    """The remote knows main (its default) and the mission branch tracks ``remote_branch``."""
    if "origin" not in git(repo, "remote").split():
        git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main:refs/heads/main")
    if remote_branch != "main":
        git(wt, "push", "-q", "origin", f"HEAD:refs/heads/{remote_branch}")
    git(wt, "fetch", "-q", "origin")
    git(wt, "branch", "-q", f"--set-upstream-to=origin/{remote_branch}", git(wt, "rev-parse", "--abbrev-ref", "HEAD"))


def _hooks(store: BridgeStore, mission_id: str, tmp_path: Path) -> BridgeMissionHooks:
    hooks = BridgeMissionHooks(mission_id, handle=None, headless=True, native_tui=False, controller_logs=tmp_path / "c.log")
    hooks.bind(store)
    return hooks


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


def _boundary(store: BridgeStore, mission_id: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], str]:
    """Run the controller's loop once; it ends at the first wait with ``STOPPING``. Returns the action and the status seen while waiting."""
    seen: list[str] = []
    sleeper = _Sleeper(monkeypatch)

    def stop() -> None:
        seen.append(store.get_mission(mission_id)["status"])
        store.set_mission_status(mission_id, "STOPPING")

    sleeper.actions.append(stop)
    return _hooks(store, mission_id, tmp_path).before_iteration(), (seen[0] if seen else "")


# --- ORCH-003: commit -----------------------------------------------------------------------------------------


def test_commit_per_task_commits_in_the_worktree_after_the_verifier_passes(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task"})
        wt = _wt(store, mission_id)
        outcome = _verify(store, mission_id, task_id, tmp_path, edit=("result.txt", "work\n"))
        assert outcome["passed"] is True and outcome["committed"] is True
        assert git(wt, "log", "-1", "--format=%an <%ae>") == "Repo Author <author@example.invalid>"
        message = git(wt, "log", "-1", "--format=%B")
        assert message.startswith("Task 1: first task\n")
        assert f"Mission: {mission_id}" in message and "Verifier: all checks passed" in message
        assert git(wt, "show", "--name-only", "--format=", "HEAD") == "result.txt"
        (event,) = _kinds(store, mission_id, "task-committed")
        assert event["data"]["committed"] is True and event["data"]["sha"] == git(wt, "rev-parse", "HEAD")
    assert git(repo, "rev-list", "--count", "HEAD") == "1"  # the source checkout is untouched


def test_commit_in_a_sub_folder_mission_still_commits_the_whole_worktree(tmp_path, repo) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "pkg")
    git(repo, "commit", "-q", "-m", "pkg")
    mission_id = uuid.uuid4().hex
    facts = bridge_isolation.prepare(repo / "pkg", mission_id=mission_id, name="sub", state_dir=tmp_path / "ws")
    assert Path(facts["cwd"]) != Path(facts["worktree_path"])  # the mission's folder is a sub-folder of the work tree
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        store.create_mission("m", "o", cwd=facts["cwd"], mission_id=mission_id, initial_prompt="t", isolation=facts,
                             config={"providers": "codex", "routes": "", "verifier": PASSING, "commit": "per-task"})
        store.set_mission_status(mission_id, "RUNNING")
        task_id = int(store.db.execute("SELECT id FROM tasks WHERE mission_id=?", (mission_id,)).fetchone()["id"])
        (Path(facts["worktree_path"]) / "top.txt").write_text("top\n", encoding="utf-8")  # outside the sub-folder
        outcome = _verify(store, mission_id, task_id, tmp_path, edit=("new.py", "y = 2\n"))
        assert outcome["committed"] is True
        assert sorted(git(Path(facts["worktree_path"]), "show", "--name-only", "--format=", "HEAD").splitlines()) == ["pkg/new.py", "top.txt"]


def test_no_commit_without_the_setting_and_none_when_the_verifier_fails(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {})
        _verify(store, mission_id, task_id, tmp_path, edit=("a.txt", "a\n"))
        assert git(_wt(store, mission_id), "rev-list", "--count", "HEAD") == "1" and not _kinds(store, mission_id, "task-committed")
    with BridgeStore(tmp_path / "b2.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "verifier": FAILING})
        outcome = _verify(store, mission_id, task_id, tmp_path, edit=("b.txt", "b\n"))
        assert outcome["passed"] is False
        assert git(_wt(store, mission_id), "rev-list", "--count", "HEAD") == "1" and not _kinds(store, mission_id, "task-committed")


def test_commit_with_nothing_staged_is_not_an_error(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task"})
        outcome = _verify(store, mission_id, task_id, tmp_path)
        assert outcome["committed"] is True and git(_wt(store, mission_id), "rev-list", "--count", "HEAD") == "1"
        (event,) = _kinds(store, mission_id, "task-committed")
        assert event["data"]["committed"] is False
        assert store.get_mission(mission_id)["status"] == "RUNNING"


def _break_commits(repo: Path) -> Path:
    hook = repo / ".git" / "hooks" / "pre-commit"  # worktrees share the repository's hooks
    hook.write_text("#!/bin/sh\necho blocked by hook >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    return hook


def test_commit_failure_holds_the_mission_with_an_event(tmp_path, repo) -> None:
    _break_commits(repo)
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task"})
        outcome = _verify(store, mission_id, task_id, tmp_path, edit=("c.txt", "c\n"))
        assert outcome["committed"] is False
        mission = store.get_mission(mission_id)
        assert mission["status"] == "PAUSED" and mission["config"]["hold_reason"] == "commit-failed"
        assert mission["config"]["pending_publish"] == {"task_id": task_id, "stage": "commit"}
        assert "blocked by hook" in _kinds(store, mission_id, "task-commit-failed")[0]["data"]["reason"]
        assert store.get_task(task_id)["status"] == "COMPLETED"  # the verified result is kept


def test_a_mission_that_is_not_isolated_never_commits(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=repo, initial_prompt="t", config={"providers": "codex", "routes": "", "verifier": PASSING, "commit": "per-task"})
        store.set_mission_status(mission["id"], "RUNNING")
        task_id = int(mission["initial_task_id"])
        _verify(store, mission["id"], task_id, tmp_path, edit=("x.txt", "x\n"))
        assert git(repo, "rev-list", "--count", "HEAD") == "1"
        assert "not isolated" in store.get_mission(mission["id"])["config"]["hold_detail"]


def test_commit_and_push_are_refused_for_a_mission_that_is_not_isolated(tmp_path, state, repo, capsys) -> None:
    verifier = ["--verifier", "true"]
    assert main(["mission", "create", "--prompt", "x", "--cwd", str(repo), "--commit", "per-task", *verifier]) != 0
    assert "--isolate worktree" in capsys.readouterr().err
    assert main(["mission", "create", "--prompt", "x", "--cwd", str(repo), "--push-default-branch", *verifier]) != 0
    assert "--isolate worktree" in capsys.readouterr().err
    assert main(["mission", "create", "--prompt", "x", "--cwd", str(repo), "--providers", "codex", "--json", *verifier]) == 0
    mission = json.loads(capsys.readouterr().out)["mission"]["id"]
    assert main(["mission", "set", "--mission", mission, "--commit", "per-task"]) != 0
    assert "--isolate worktree" in capsys.readouterr().err
    assert main(["mission", "set", "--mission", mission, "--push", "per-task"]) != 0
    capsys.readouterr()


def test_commit_without_a_verifier_is_refused(tmp_path, state, repo, capsys) -> None:
    assert main(["mission", "create", "--prompt", "x", "--cwd", str(repo), "--isolate", "worktree", "--commit", "per-task"]) != 0
    assert "need a verifier" in capsys.readouterr().err
    assert main(["mission", "create", "--prompt", "x", "--cwd", str(repo), "--isolate", "worktree", "--commit", "per-task", "--verifier", "true", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)["mission"]
    assert created["config"]["commit"] == "per-task" if "config" in created else True
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(created["id"])["config"]["commit"] == "per-task"
    # an isolated mission without a verifier cannot be switched to commit either
    assert main(["mission", "create", "--prompt", "y", "--cwd", str(repo), "--isolate", "worktree", "--name", "other", "--json"]) == 0
    other = json.loads(capsys.readouterr().out)["mission"]["id"]
    assert main(["mission", "set", "--mission", other, "--commit", "per-task"]) != 0
    assert "need a verifier" in capsys.readouterr().err


def test_task_prompt_states_the_policy(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, _ = _mission(store, repo, {"commit": "per-task", "push": "per-mission"})
        action = _hooks(store, mission_id, tmp_path).before_iteration()
        assert action["action"] == "run"
        assert "Bridge itself commits" in action["prompt"] and "last task has passed" in action["prompt"] and "never force-push" in action["prompt"]
    with BridgeStore(tmp_path / "b2.sqlite3") as store:
        mission_id, _ = _mission(store, repo, {})
        assert "Version control policy" not in _hooks(store, mission_id, tmp_path).before_iteration()["prompt"]


def test_push_needs_commit_per_task(tmp_path, repo, state, capsys) -> None:
    assert main(["mission", "create", "--prompt", "x", "--cwd", str(repo), "--push", "per-task"]) != 0
    assert "push needs commit per-task" in capsys.readouterr().err


def test_remote_callers_cannot_change_commit_or_push(tmp_path, state, repo, capsys) -> None:
    assert main(["mission", "create", "--prompt", "x", "--cwd", str(repo), "--isolate", "worktree", "--verifier", "true", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    for params in ({"commit": "per-task"}, {"push": "per-task"}, {"push_default_branch": True}):
        result = execute(CommandRequest("mission.settings.patch", params, mission_id=mission_id, remote=True))
        assert result.outcome == "rejected" and result.error.code == "unsupported-remote-option", params
    # the same patch from the local CLI path is accepted, and budgets stay available to remote callers
    dispatch(CommandRequest("mission.settings.patch", {"commit": "per-task"}, mission_id=mission_id))
    result = execute(CommandRequest("mission.settings.patch", {"budget_runs": 4}, mission_id=mission_id, remote=True))
    assert result.outcome == "applied", result
    with BridgeStore(state / "bridge.sqlite3") as store:
        config = store.get_mission(mission_id)["config"]
        assert config["commit"] == "per-task" and config["budget"] == {"runs": 4}


def test_remote_request_flag_is_never_serialised() -> None:
    request = CommandRequest("mission.status", mission_id="a" * 32, remote=True)
    assert "remote" not in request.to_json()
    assert CommandRequest.from_json(request.to_json()).remote is False
    assert dataclasses.replace(request, remote=False).remote is False


# --- ORCH-003: push -------------------------------------------------------------------------------------------


def test_push_per_task_pushes_to_the_configured_upstream(tmp_path, repo, remote) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        wt = _wt(store, mission_id)
        _with_remote(repo, wt, remote, "feature")
        assert _verify(store, mission_id, task_id, tmp_path, edit=("p.txt", "p\n"))["committed"] is True
        assert git(remote, "rev-parse", "refs/heads/feature") == git(wt, "rev-parse", "HEAD")
        (event,) = _kinds(store, mission_id, "task-pushed")
        assert event["data"]["remote"] == "origin" and event["data"]["branch"] == "feature" and event["data"]["scope"] == "task"
        assert store.get_mission(mission_id)["status"] == "RUNNING"


def test_push_never_does_not_push(tmp_path, repo, remote) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "never"})
        wt = _wt(store, mission_id)
        _with_remote(repo, wt, remote, "feature")
        before = git(remote, "rev-parse", "refs/heads/feature")
        _verify(store, mission_id, task_id, tmp_path, edit=("p.txt", "p\n"))
        assert git(remote, "rev-parse", "refs/heads/feature") == before and not _kinds(store, mission_id, "task-pushed")


def test_push_refuses_the_default_branch_unless_the_mission_was_created_for_it(tmp_path, repo, remote) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        wt = _wt(store, mission_id)
        _with_remote(repo, wt, remote, "main")
        before = git(remote, "rev-parse", "refs/heads/main")
        _verify(store, mission_id, task_id, tmp_path, edit=("d.txt", "d\n"))
        assert git(remote, "rev-parse", "refs/heads/main") == before
        mission = store.get_mission(mission_id)
        assert mission["status"] == "PAUSED" and mission["config"]["hold_reason"] == "push-refused"
        assert "default branch" in _kinds(store, mission_id, "push-failed")[0]["data"]["reason"]
    with BridgeStore(tmp_path / "b2.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task", "push_default_branch": True})
        wt = _wt(store, mission_id)
        _with_remote(repo, wt, remote, "main")
        _verify(store, mission_id, task_id, tmp_path, edit=("e.txt", "e\n"))
        assert git(remote, "rev-parse", "refs/heads/main") == git(wt, "rev-parse", "HEAD")


def test_default_branch_comes_from_the_remote_not_from_the_name_main(tmp_path, repo) -> None:
    trunk = _bare(tmp_path / "trunk.git", default="trunk")
    git(repo, "remote", "add", "origin", str(trunk))
    git(repo, "push", "-q", "origin", "main:refs/heads/trunk")  # the remote's default is trunk, and refs/remotes/origin/HEAD is not set
    assert not (git(repo, "for-each-ref", "refs/remotes/origin/HEAD"))
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"commit": "per-task", "push": "per-task"}, tasks=2)
        wt = _wt(store, mission_id)
        git(wt, "fetch", "-q", "origin")
        git(wt, "branch", "-q", "--set-upstream-to=origin/trunk", git(wt, "rev-parse", "--abbrev-ref", "HEAD"))
        before = git(trunk, "rev-parse", "refs/heads/trunk")
        _verify(store, mission_id, first, tmp_path, edit=("t.txt", "t\n"))
        assert git(trunk, "rev-parse", "refs/heads/trunk") == before  # trunk is the default branch, so it is refused
        assert store.get_mission(mission_id)["config"]["hold_reason"] == "push-refused"
    # main is an ordinary branch of this remote: a push to it is allowed
    with BridgeStore(tmp_path / "b2.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        wt = _wt(store, mission_id)
        git(wt, "push", "-q", "origin", "HEAD:refs/heads/main")
        git(wt, "fetch", "-q", "origin")
        git(wt, "branch", "-q", "--set-upstream-to=origin/main", git(wt, "rev-parse", "--abbrev-ref", "HEAD"))
        _verify(store, mission_id, task_id, tmp_path, edit=("m.txt", "m\n"))
        assert git(trunk, "rev-parse", "refs/heads/main") == git(wt, "rev-parse", "HEAD")


def test_push_is_refused_when_the_default_branch_cannot_be_determined(tmp_path, repo, remote) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        wt = _wt(store, mission_id)
        git(repo, "remote", "add", "origin", str(tmp_path / "missing.git"))  # nothing answers there
        git(wt, "config", "remote.pushDefault", "origin")
        _verify(store, mission_id, task_id, tmp_path, edit=("u.txt", "u\n"))
        mission = store.get_mission(mission_id)
        assert mission["config"]["hold_reason"] == "push-refused"
        assert "could not be determined" in mission["config"]["hold_detail"]


def test_push_without_a_configured_remote_is_refused_and_invents_none(tmp_path, repo, remote) -> None:
    git(repo, "remote", "add", "origin", str(remote))  # a remote exists, but nothing says to push this branch there
    git(repo, "push", "-q", "origin", "main:refs/heads/main")
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        _verify(store, mission_id, task_id, tmp_path, edit=("n.txt", "n\n"))
        mission = store.get_mission(mission_id)
        assert mission["status"] == "PAUSED" and mission["config"]["hold_reason"] == "push-refused"
        assert "no upstream" in mission["config"]["hold_detail"]
    assert git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads") == "main"  # only the seed; nothing from the mission reached the remote


def test_push_uses_the_configured_push_remote_for_a_new_branch(tmp_path, repo, remote) -> None:
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main:refs/heads/main")
    git(repo, "config", "remote.pushDefault", "origin")
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        wt = _wt(store, mission_id)
        _verify(store, mission_id, task_id, tmp_path, edit=("n.txt", "n\n"))
        branch = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
        assert git(remote, "rev-parse", f"refs/heads/{branch}") == git(wt, "rev-parse", "HEAD")


def test_push_failure_holds_the_mission(tmp_path, repo, remote) -> None:
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho remote says no >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        wt = _wt(store, mission_id)
        git(repo, "remote", "add", "origin", str(remote))
        hook.chmod(0o644)  # let the seed through
        git(repo, "push", "-q", "origin", "main:refs/heads/main")
        hook.chmod(0o755)
        git(wt, "config", "remote.pushDefault", "origin")
        outcome = _verify(store, mission_id, task_id, tmp_path, edit=("f.txt", "f\n"))
        assert outcome["committed"] is False
        mission = store.get_mission(mission_id)
        assert mission["status"] == "PAUSED" and mission["config"]["hold_reason"] == "push-failed"
        assert "remote says no" in _kinds(store, mission_id, "push-failed")[0]["data"]["reason"]
        assert _kinds(store, mission_id, "task-committed")  # the commit stays


def test_no_git_call_ever_forces(tmp_path, repo, remote, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    real = bridge_publish.git

    def recording(cwd, *args, **kwargs):
        calls.append(args)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(bridge_publish, "git", recording)
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-task"})
        _with_remote(repo, _wt(store, mission_id), remote, "feature")
        _verify(store, mission_id, task_id, tmp_path, edit=("g.txt", "g\n"))
    pushes = [call for call in calls if call and call[0] == "push"]
    assert len(pushes) == 1
    assert not any(arg in {"--force", "-f", "--force-with-lease", "--mirror", "--delete"} or arg.startswith(("+", "--force")) for call in calls for arg in call)


def test_push_per_mission_happens_once_after_the_last_task(tmp_path, repo, remote) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"commit": "per-task", "push": "per-mission"}, tasks=2)
        wt = _wt(store, mission_id)
        _with_remote(repo, wt, remote, "feature")
        before = git(remote, "rev-parse", "refs/heads/feature")
        _verify(store, mission_id, first, tmp_path, edit=("one.txt", "1\n"))
        _verify(store, mission_id, second, tmp_path, edit=("two.txt", "2\n"))
        assert git(remote, "rev-parse", "refs/heads/feature") == before  # nothing pushed after a single task
        assert _hooks(store, mission_id, tmp_path).before_iteration()["action"] == "stop"
        assert store.get_mission(mission_id)["status"] == "COMPLETED"
        assert git(remote, "rev-parse", "refs/heads/feature") == git(wt, "rev-parse", "HEAD")
        (event,) = _kinds(store, mission_id, "task-pushed")
        assert event["data"]["scope"] == "mission"


def test_failed_mission_push_keeps_the_mission_from_completing(tmp_path, repo, remote, monkeypatch) -> None:
    git(repo, "remote", "add", "origin", str(remote))  # no upstream, no push remote
    git(repo, "push", "-q", "origin", "main:refs/heads/main")
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (task_id,) = _mission(store, repo, {"commit": "per-task", "push": "per-mission"})
        _verify(store, mission_id, task_id, tmp_path, edit=("x.txt", "x\n"))
        action, seen = _boundary(store, mission_id, tmp_path, monkeypatch)
        assert action["action"] == "stop" and seen == "PAUSED" and store.get_mission(mission_id)["status"] == "STOPPING"
        assert store.get_task(task_id)["status"] == "COMPLETED"
        assert _kinds(store, mission_id, "push-failed")


def test_a_failed_commit_is_retried_for_its_own_task_before_the_next_task(tmp_path, repo, monkeypatch) -> None:
    hook = _break_commits(repo)
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"commit": "per-task"}, tasks=2)
        wt = _wt(store, mission_id)
        assert _verify(store, mission_id, first, tmp_path, edit=("one.txt", "1\n"))["committed"] is False
        assert git(wt, "rev-list", "--count", "HEAD") == "1"
        hook.unlink()  # the operator fixes the cause and resumes
        store.set_mission_status(mission_id, "RUNNING")
        action = _hooks(store, mission_id, tmp_path).before_iteration()
        assert action["action"] == "run" and action["task_id"] == second  # the retry happened before task 2 started
        assert git(wt, "log", "-1", "--format=%s") == "Task 1: first task"
        assert git(wt, "show", "--name-only", "--format=", "HEAD") == "one.txt"
        assert not store.get_mission(mission_id)["config"].get("pending_publish")
        _verify(store, mission_id, second, tmp_path, edit=("two.txt", "2\n"))
        assert git(wt, "log", "-1", "--format=%s") == "Task 2: Task 2"
        assert git(wt, "show", "--name-only", "--format=", "HEAD") == "two.txt"  # task 2's commit holds only its own file


def test_a_commit_that_fails_again_on_resume_holds_the_mission_again(tmp_path, repo, monkeypatch) -> None:
    _break_commits(repo)
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"commit": "per-task"}, tasks=2)
        _verify(store, mission_id, first, tmp_path, edit=("one.txt", "1\n"))
        store.set_mission_status(mission_id, "RUNNING")
        action, seen = _boundary(store, mission_id, tmp_path, monkeypatch)
        assert action["action"] == "stop" and seen == "PAUSED"
        assert len(_kinds(store, mission_id, "task-commit-failed")) == 2
        assert store.get_task(second)["status"] in {"READY", "QUEUED"}  # task 2 never started


def test_hold_writes_the_reason_and_the_status_in_one_transaction(tmp_path, repo, monkeypatch) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, _ = _mission(store, repo, {})

        def broken(*_args, **_kwargs):
            raise RuntimeError("status write failed")

        monkeypatch.setattr(store, "set_mission_status", broken)
        with pytest.raises(RuntimeError):
            bridge_publish.hold_mission(store, mission_id, "commit-failed", "x")
        mission = store.get_mission(mission_id)
        assert mission["status"] == "RUNNING" and not mission["config"].get("hold_reason")  # nothing half-written
        monkeypatch.undo()
        bridge_publish.hold_mission(store, mission_id, "commit-failed", "x")
        mission = store.get_mission(mission_id)
        assert mission["status"] == "PAUSED" and mission["config"]["hold_reason"] == "commit-failed"


# --- ORCH-005: budgets ----------------------------------------------------------------------------------------


def _operation(store: BridgeStore, task_id: int, station: str = "codex") -> None:
    store.begin_iteration(task_id)
    operation = store.begin_operation(task_id, iteration=1, station=station)
    store.mark_operation_running(operation)
    store.finish_operation(operation, exit_code=0)


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


class _Config:
    """The slice of the scheduler configuration that ``before_launch`` reads and sets."""

    cwd = "."
    provider = "codex"
    model = ""
    prompt_text = "do it"
    retry_delays = "60,180,600"
    run_dir = None
    exec_mode = ""
    headless = True
    native_tui = False


def _launch(store: BridgeStore, mission_id: str, tmp_path: Path, task_id: int) -> tuple[bool, _Config]:
    hooks = _hooks(store, mission_id, tmp_path)
    hooks._await_capacity_data = lambda *_args: True  # type: ignore[method-assign]
    config = _Config()
    config.cwd = store.get_mission(mission_id)["cwd"]
    return hooks.before_launch({"task_id": task_id, "revision": 1}, {"provider": "codex"}, config, 1), config


def test_every_launch_is_checked_against_the_run_limit(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"budget": {"runs": 2}}, tasks=2)
        _operation(store, first)
        launched, config = _launch(store, mission_id, tmp_path, second)  # the second launch is still within the limit
        assert launched is True
        assert config.retry_delays == ""  # a retry inside the scheduler would start a provider process that no launch check sees
        store.finish_operation(store.db.execute("SELECT id FROM operations ORDER BY rowid DESC").fetchone()["id"], exit_code=1)
        store.db.execute("UPDATE tasks SET status='READY' WHERE id=?", (second,))
        launched, _ = _launch(store, mission_id, tmp_path, second)  # a retry of that task is a third provider run
        assert launched is False
        mission = store.get_mission(mission_id)
        assert mission["status"] == "PAUSED" and mission["config"]["hold_reason"] == "budget-exhausted"
        assert len(store.db.execute("SELECT id FROM operations WHERE mission_id=?", (mission_id,)).fetchall()) == 2


def test_without_a_run_limit_scheduler_retries_stay_on(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first,) = _mission(store, repo, {"budget": {"provider_changes": 3}})
        launched, config = _launch(store, mission_id, tmp_path, first)
        assert launched is True and config.retry_delays == "60,180,600"


def test_provider_change_limit(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second, third) = _mission(store, repo, {"budget": {"provider_changes": 1}}, tasks=3)
        for task_id, station in zip((first, second, third), ("codex", "codex", "claude")):
            _operation(store, task_id, station)
        hit = bridge_budget.check(store, store.get_mission(mission_id))
        assert hit and hit["limits"] == ["provider_changes"] and hit["used"]["provider_changes"] == 1


def test_wall_clock_limit_between_runs(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"budget": {"wall_seconds": 600}}, tasks=2)
        _operation(store, first)
        mission = store.get_mission(mission_id)
        assert bridge_budget.check(store, mission) is None
        started = bridge_budget._epoch(store.db.execute("SELECT created_at FROM operations").fetchone()["created_at"])
        hit = bridge_budget.check(store, mission, now=started + 601)
        assert hit and hit["limits"] == ["wall_seconds"] and hit["used"]["wall_seconds"] == 601


def test_wall_clock_limit_holds_on_the_heartbeat_while_a_run_is_in_flight(tmp_path, repo) -> None:
    database = tmp_path / "b.sqlite3"
    with BridgeStore(database) as store:
        mission_id, (first, _second) = _mission(store, repo, {"budget": {"wall_seconds": 600}}, tasks=2)
        store.begin_iteration(first)
        operation = store.begin_operation(first, iteration=1, station="codex")
        store.mark_operation_running(operation)  # the provider is running now, and its run has lasted past the limit
        store.db.execute("UPDATE operations SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (operation,))
    worker = CapacityRefreshWorker(mission_id, database, heartbeat_seconds=1, initial_refresh_delay_seconds=3600, prune_interval_seconds=3600)
    worker.start()
    try:
        def held() -> bool:
            with BridgeStore(database) as reader:
                return str(reader.get_mission(mission_id)["status"]) == "PAUSED"

        wait_for(held, 10, 0.1, "the heartbeat should hold the mission for its wall-clock limit")
    finally:
        worker.stop()
    with BridgeStore(database) as store:
        mission = store.get_mission(mission_id)
        assert mission["config"]["hold_reason"] == "budget-exhausted"
        assert store.get_operation(operation)["status"] == "RUNNING"  # the run in flight was not touched
        assert [event["data"]["limits"] for event in _kinds(store, mission_id, "budget-exhausted")] == [["wall_seconds"]]


def test_spend_limit_counts_only_the_budget_currency(tmp_path, repo) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, _ = _mission(store, repo, {"budget": {"spend": 5.0, "currency": "USD"}})
        store.record_runtime_event(mission_id, "provider-spend", {"amount": 2.0, "currency": "USD"})
        store.record_runtime_event(mission_id, "provider-spend", {"amount": 100, "currency": "EUR"})  # another currency is not summed
        mission = store.get_mission(mission_id)
        assert bridge_budget.check(store, mission) is None
        store.record_runtime_event(mission_id, "provider-spend", {"amount": 3.0, "currency": "$"})  # a dollar sign is USD
        assert bridge_budget.check(store, mission)["limits"] == ["spend"]


def _kilo_usage(cost: float, currency: str = "$") -> str:
    return json.dumps({"kilo": {"provider": "kilo", "available": True, "scopes": [
        {"name": "balance", "kind": "balance", "remaining_amount": cost, "currency": currency, "extras": {"spent": True}}]}})


def _copilot_usage(spent: float) -> str:
    return json.dumps({"copilot": {"provider": "copilot", "available": True, "add_on": {"spent": spent, "currency": "$"}}})


def test_spend_is_the_increase_since_the_baseline_for_kilo(tmp_path, repo, monkeypatch) -> None:
    selection = {"decisions": [{"provider": "kilo", "usable": True}, {"provider": "claude", "usable": True}]}
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, _ = _mission(store, repo, {"budget": {"spend": 5.0, "currency": "USD"}, "providers": "kilo,claude"})

        def sample(cost: float) -> list[dict[str, Any]]:
            monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", _kilo_usage(cost))
            return bridge_budget.sample_spend(store, store.get_mission(mission_id), selection)

        assert sample(40.0) == []  # the first reading is the baseline: earlier spend is not the mission's
        assert sample(42.5) == [{"provider": "kilo", "amount": 2.5, "currency": "USD"}]
        assert sample(42.5) == []
        # A lower figure is a new billing month: its whole amount is spent since the reset, and nothing already counted is lost.
        assert sample(1.0) == [{"provider": "kilo", "amount": 1.0, "currency": "USD"}]
        assert bridge_budget.check(store, store.get_mission(mission_id)) is None  # 2.5 + 1.0 of 5.0
        assert sample(3.0) == [{"provider": "kilo", "amount": 2.0, "currency": "USD"}]
        assert bridge_budget.check(store, store.get_mission(mission_id))["limits"] == ["spend"]  # 5.5 of 5.0


def test_spend_from_copilot_add_on_and_a_selection_without_spend_providers(tmp_path, repo, monkeypatch) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, _ = _mission(store, repo, {"budget": {"spend": 5.0, "currency": "USD"}, "providers": "copilot"})
        monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", _copilot_usage(1.0))
        hooks = _hooks(store, mission_id, tmp_path)
        hooks.on_selection({"decisions": [{"provider": "copilot", "usable": True}], "index": 0, "provider": "copilot", "route": "", "rotation_reason": "first"})
        monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", _copilot_usage(3.5))
        hooks.on_selection({"decisions": [{"provider": "copilot", "usable": True}], "index": 0, "provider": "copilot", "route": "", "rotation_reason": "same"})
        (event,) = _kinds(store, mission_id, "provider-spend")
        assert event["data"] == {"amount": 2.5, "currency": "USD", "provider": "copilot"}
        before = len(store.events(mission_id))
        bridge_budget.sample_spend(store, store.get_mission(mission_id), {"decisions": [{"provider": "claude"}]})
        assert len(store.events(mission_id)) == before  # no spend-capable provider in the selection: nothing is read or written


def test_a_spend_limit_is_refused_unless_a_rotation_provider_reports_spend_in_that_currency(tmp_path, state, repo, capsys) -> None:
    base = ["mission", "create", "--prompt", "x", "--cwd", str(repo), "--budget-spend", "5", "--budget-currency", "USD"]
    assert main([*base, "--providers", "claude,codex"]) != 0
    err = capsys.readouterr().err
    assert "Copilot" in err and "Kilo" in err and "none of them" in err
    assert main([*base, "--providers", "copilot", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)["mission"]["id"]
    assert main([*base[:-1], "EUR", "--providers", "copilot"]) != 0
    assert "not in that currency" in capsys.readouterr().err
    assert main(["mission", "set", "--mission", created, "--budget-currency", "EUR"]) != 0
    assert "not in that currency" in capsys.readouterr().err
    assert main(["mission", "set", "--mission", created, "--budget-spend", "9"]) == 0
    capsys.readouterr()
    assert main(["mission", "create", "--prompt", "x", "--cwd", str(repo), "--providers", "claude", "--budget-spend", "5", "--budget-currency", "USD", "--name", "n2"]) != 0
    capsys.readouterr()


def test_a_spend_limit_needs_a_currency() -> None:
    with pytest.raises(ValueError, match="needs a currency"):
        bridge_budget.validate_budget({"spend": 3})
    assert bridge_budget.validate_budget({"spend": 3, "currency": "usd"}) == {"spend": 3.0, "currency": "USD"}
    assert bridge_budget.validate_budget({"spend": 3, "currency": "$"}) == {"spend": 3.0, "currency": "USD"}
    with pytest.raises(ValueError):
        bridge_budget.validate_budget({"runs": 0})
    with pytest.raises(ValueError):
        bridge_budget.validate_budget({"tokens": 5})


def test_raising_the_limit_releases_the_hold_and_resumes(tmp_path, state, repo, monkeypatch, capsys) -> None:
    assert main(["mission", "create", "--prompt", "obj", "--cwd", str(repo), "--providers", "codex", "--budget-runs", "1", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)["mission"]
    mission_id = created["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.add_task(mission_id, "second")
        store.set_mission_status(mission_id, "RUNNING")
        _operation(store, int(created["initial_task_id"]))
        action, seen = _boundary(store, mission_id, tmp_path, monkeypatch)
        assert action["action"] == "stop" and seen == "PAUSED"
        store.db.execute("UPDATE missions SET status='PAUSED' WHERE id=?", (mission_id,))  # the stop in _boundary ended the loop; the hold stays

    assert main(["status", "--mission", mission_id]) == 0
    text = capsys.readouterr().out
    assert "Budget      provider runs 1 of 1" in text and "Held        budget-exhausted" in text and "mission set" in text
    assert main(["status", "--mission", mission_id, "--json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
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


def test_resuming_a_held_mission_without_raising_the_limit_holds_it_again(tmp_path, repo, monkeypatch) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"budget": {"runs": 1}}, tasks=2)
        _operation(store, first)
        _boundary(store, mission_id, tmp_path, monkeypatch)
        store.db.execute("UPDATE missions SET status='RUNNING' WHERE id=?", (mission_id,))  # the operator started it again
        _boundary(store, mission_id, tmp_path, monkeypatch)
        assert len(_kinds(store, mission_id, "budget-exhausted")) == 2


def test_console_overview_shows_the_budget(tmp_path, repo) -> None:
    from llm_tools.bridge_console_rows import _ViewRows
    from llm_tools.bridge_monitor import project_snapshot

    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission_id, (first, second) = _mission(store, repo, {"budget": {"runs": 1, "wall_seconds": 3600}}, tasks=2)
        _operation(store, first)
        bridge_budget.hold_if_reached(store, store.get_mission(mission_id))
        snapshot = project_snapshot(store, mission_id)
    text = "\n".join(_ViewRows._bridge_rows(types.SimpleNamespace(snapshot=snapshot)))
    assert "provider runs 1 of 1" in text and "wall-clock time" in text and "limit reached" in text
    assert "held: budget exhausted" in text


def test_settings_show_and_validate_through_the_typed_patch(tmp_path, state, repo, capsys) -> None:
    assert main(["mission", "create", "--prompt", "obj", "--cwd", str(repo), "--providers", "copilot", "--isolate", "worktree", "--verifier", "true", "--json"]) == 0
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
    with pytest.raises(SystemExit):  # argparse rejects 0; none removes a limit
        main(["mission", "set", "--mission", mission_id, "--budget-runs", "0"])
    capsys.readouterr()
    assert main(["mission", "set", "--mission", mission_id, "--budget-minutes", "none"]) == 0
    assert dispatch(CommandRequest("mission.settings.get", mission_id=mission_id))["budget"] == {}
