"""Commit and push policy for missions (ORCH-003).

Two mission settings control what Bridge does with a task's result:

* ``commit``: ``none`` (default) or ``per-task``. With ``per-task`` Bridge commits the working directory
  itself after the configured verifier passes.
* ``push``: ``never`` (default), ``per-task`` or ``per-mission``. Bridge pushes the commit after each task
  or when the mission has finished its last task. A push needs ``commit = per-task``.

Bridge runs git itself, so the policy does not depend on the provider or on the provider's sandbox.
Rules that hold for every git call made here:

* Commit and push are for isolated missions only (``--isolate worktree``): a commit in a checkout that the
  operator also works in would take their changes. Git runs at the mission's worktree path (the top of the
  work tree, even when the mission's folder is a sub-folder), without a shell, with a timeout and with
  terminal prompts disabled.
* Both need a configured verifier: Bridge commits only after a verifier pass.
* No force option or force refspec is ever used.
* The push target is the branch's configured upstream, or the same-named branch on the configured push
  remote (``branch.<name>.pushRemote`` or ``remote.pushDefault``). With neither, the push is refused and the
  mission is held; Bridge does not choose a remote.
* A push to the repository's default branch is refused unless the mission was created with
  ``--push-default-branch``. The default branch is the one ``git ls-remote --symref REMOTE HEAD`` names; when
  that cannot be determined the push is refused.
* A commit with nothing to commit is not an error.
* A failed commit or push holds the mission (status ``PAUSED``, ``hold_reason`` in its config, saved in one
  transaction) and records an event. The task keeps its verified status. The pending step is saved as
  ``pending_publish``; when the operator resumes, the controller retries it, with the failed task's own commit
  message, before the next task starts, so one task's changes never go into another task's commit.
* The author and committer come from the repository's git configuration; Bridge sets neither.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COMMIT_MODES = ("none", "per-task")
PUSH_MODES = ("never", "per-task", "per-mission")
DEFAULT_COMMIT = "none"
DEFAULT_PUSH = "never"
GIT_TIMEOUT_SECONDS = 60.0
PUSH_TIMEOUT_SECONDS = 180.0
LS_REMOTE_TIMEOUT_SECONDS = 30.0
SUBJECT_LIMIT = 72
HOLD_COMMIT_FAILED = "commit-failed"
HOLD_PUSH_FAILED = "push-failed"
HOLD_PUSH_REFUSED = "push-refused"


def validate_policy(commit: Any, push: Any) -> tuple[str, str]:
    """Normalise ``commit`` and ``push``; raise ``ValueError`` with an operator-facing message."""
    commit = DEFAULT_COMMIT if commit is None else commit
    push = DEFAULT_PUSH if push is None else push
    if commit not in COMMIT_MODES:
        raise ValueError(f"commit must be one of {', '.join(COMMIT_MODES)}")
    if push not in PUSH_MODES:
        raise ValueError(f"push must be one of {', '.join(PUSH_MODES)}")
    if push != "never" and commit != "per-task":
        raise ValueError("push needs commit per-task: Bridge pushes only commits that it made after a verifier pass")
    return str(commit), str(push)


def policy_of(config: dict[str, Any] | None) -> dict[str, Any]:
    config = config if isinstance(config, dict) else {}
    try:
        commit, push = validate_policy(config.get("commit"), config.get("push"))
    except ValueError:
        commit, push = DEFAULT_COMMIT, DEFAULT_PUSH
    return {"commit": commit, "push": push, "push_default_branch": bool(config.get("push_default_branch", False))}


def policy_prompt(config: dict[str, Any] | None) -> str:
    """The sentence block that tells the provider what Bridge does with its work."""
    policy = policy_of(config)
    if policy["commit"] == "none":
        return ""
    text = "Version control policy: Bridge itself commits the working directory after the verifier passes for each task. Do not run git commit yourself"
    if policy["push"] == "never":
        return text + " and do not push; Bridge does not push either.\n\n"
    when = "after each task" if policy["push"] == "per-task" else "when the mission's last task has passed"
    return text + f". Bridge pushes the commits {when}; do not push yourself and never force-push.\n\n"


@dataclass
class GitResult:
    code: int
    out: str
    err: str


def _git_env() -> dict[str, str]:
    keep = {"GIT_SSH", "GIT_SSH_COMMAND", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_") or key in keep}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "true"
    return env


def git(cwd: str | Path, *args: str, timeout: float = GIT_TIMEOUT_SECONDS) -> GitResult:
    """Run ``git -C cwd args`` without a shell. A timeout or a missing git is a failed result, not an exception."""
    argv = ["git", "-C", str(Path(cwd).resolve()), *args]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=_git_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return GitResult(124, "", f"git {args[0]} timed out after {int(timeout)} s")
    except OSError as exc:
        return GitResult(127, "", f"git could not run: {exc}")
    return GitResult(done.returncode, done.stdout.strip(), done.stderr.strip())


def commit_message(mission: dict[str, Any], task: dict[str, Any], summary: str) -> str:
    title = " ".join(str(task.get("title") or "").split())
    subject = f"Task {task['ordinal']}: {title}".rstrip(": ")
    if len(subject) > SUBJECT_LIMIT:
        subject = subject[:SUBJECT_LIMIT - 3].rstrip() + "..."
    body = ["Committed by LLM Bridge after the verifier passed.", "",
            f"Mission: {mission['id']}", f"Task: {task['ordinal']} (revision {task.get('revision', 1)})"]
    if summary:
        body += ["", "Verifier: " + " ".join(summary.split())[:400]]
    return subject + "\n\n" + "\n".join(body) + "\n"


def _event(store: Any, mission_id: str, kind: str, data: dict[str, Any]) -> None:
    store.record_runtime_event(mission_id, kind, data)


def hold_mission(store: Any, mission_id: str, reason: str, detail: str, **extra: Any) -> None:
    """Hold the mission for the operator: ``PAUSED`` plus a saved reason, in one transaction; status and the console show it."""
    store.hold_mission(mission_id, {"hold_reason": reason, "hold_detail": detail[:500], **extra})


def work_tree(mission: dict[str, Any]) -> str:
    """The top of the mission's worktree, or an empty string for a mission that is not isolated."""
    return str(mission.get("worktree_path") or "") if mission.get("isolation") == "worktree" else ""


def _repo_state(cwd: str) -> tuple[str | None, str]:
    """``(branch, error)``: the checked-out branch name, or an error for a non-repository or detached HEAD."""
    top = git(cwd, "rev-parse", "--show-toplevel")
    if top.code != 0:
        return None, f"{cwd} is not inside a git repository: {top.err or top.out}"
    try:
        same = Path(top.out).resolve() == Path(cwd).resolve()
    except OSError:
        same = False
    if not same:
        return None, f"{cwd} is not the top of a git work tree (it is inside {top.out}); Bridge commits only a mission's own work tree"
    head = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    if head.code != 0 or not head.out:
        return None, "HEAD is detached; check out a branch first"
    return head.out, ""


def commit_task(store: Any, mission: dict[str, Any], task: dict[str, Any], summary: str) -> bool:
    """Commit the working directory after a verifier pass when the policy asks for it; returns False when it failed (mission held)."""
    policy = policy_of(mission.get("config"))
    if policy["commit"] != "per-task":
        return True
    mission_id = str(mission["id"])
    cwd = work_tree(mission)
    task_id = int(task["id"])

    def fail(reason: str) -> bool:
        _event(store, mission_id, "task-commit-failed", {"task_id": task_id, "reason": reason[:500]})
        hold_mission(store, mission_id, HOLD_COMMIT_FAILED, reason, pending_publish={"task_id": task_id, "stage": "commit"})
        return False

    if not cwd:
        return fail("this mission is not isolated; Bridge commits only in a worktree (create the mission with --isolate worktree)")
    branch, error = _repo_state(cwd)
    if branch is None:
        return fail(error)
    added = git(cwd, "add", "-A")
    if added.code != 0:
        return fail(f"git add failed: {added.err or added.out}")
    if git(cwd, "diff", "--cached", "--quiet").code == 0:
        head = git(cwd, "rev-parse", "HEAD")
        _event(store, mission_id, "task-committed", {"task_id": task_id, "committed": False, "sha": head.out or None, "branch": branch, "reason": "nothing to commit"})
    else:
        done = git(cwd, "commit", "-q", "-m", commit_message(mission, task, summary))
        if done.code != 0:
            return fail(f"git commit failed: {done.err or done.out}")
        sha = git(cwd, "rev-parse", "HEAD").out
        files = [line for line in git(cwd, "show", "--name-only", "--pretty=format:", "HEAD").out.splitlines() if line][:50]
        _event(store, mission_id, "task-committed", {"task_id": task_id, "committed": True, "sha": sha, "branch": branch, "files": files})
    if policy["push"] == "per-task":
        return push_branch(store, mission, task_id=task_id)
    return True


def default_branch(cwd: str, remote: str) -> tuple[str | None, str]:
    """``(name, "")`` of the remote's default branch from ``git ls-remote --symref REMOTE HEAD``, else ``(None, why)``.

    The local ``refs/remotes/REMOTE/HEAD`` is not used and there is no guess from branch names such as main.
    """
    done = git(cwd, "ls-remote", "--symref", remote, "HEAD", timeout=LS_REMOTE_TIMEOUT_SECONDS)
    if done.code != 0:
        return None, f"git ls-remote failed: {done.err or done.out or 'no output'}"
    for line in done.out.splitlines():
        if line.startswith("ref: refs/heads/") and line.rstrip().endswith("HEAD"):
            return line[len("ref: refs/heads/"):].split("\t")[0].strip(), ""
    return None, "the remote did not report where HEAD points"


def push_target(cwd: str, branch: str) -> tuple[str, str] | str:
    """``(remote, branch on the remote)`` from configuration, or a message saying nothing is configured."""
    remote = git(cwd, "config", "--get", f"branch.{branch}.remote").out
    merge = git(cwd, "config", "--get", f"branch.{branch}.merge").out
    if remote and merge.startswith("refs/heads/"):
        return remote, merge[len("refs/heads/"):]
    remote = git(cwd, "config", "--get", f"branch.{branch}.pushRemote").out or git(cwd, "config", "--get", "remote.pushDefault").out
    if remote:
        return remote, branch
    return (f"branch {branch} has no upstream and no push remote is configured; set one with "
            f"git branch --set-upstream-to or git config branch.{branch}.pushRemote REMOTE")


def push_branch(store: Any, mission: dict[str, Any], *, task_id: int | None = None) -> bool:
    """Push HEAD to the configured target. Never forced. Returns False after holding the mission."""
    mission_id = str(mission["id"])
    cwd = work_tree(mission)
    policy = policy_of(mission.get("config"))

    def refuse(reason: str, hold: str = HOLD_PUSH_REFUSED, **extra: Any) -> bool:
        _event(store, mission_id, "push-failed", {"task_id": task_id, "reason": reason[:500], "refused": hold == HOLD_PUSH_REFUSED, **extra})
        pending = {"pending_publish": {"task_id": task_id, "stage": "push"}} if task_id is not None else {}
        hold_mission(store, mission_id, hold, reason, **pending)
        return False

    if not cwd:
        return refuse("this mission is not isolated; Bridge pushes only from a worktree (create the mission with --isolate worktree)")
    branch, error = _repo_state(cwd)
    if branch is None:
        return refuse(error)
    target = push_target(cwd, branch)
    if isinstance(target, str):
        return refuse(target)
    remote, remote_branch = target
    default, why = default_branch(cwd, remote)
    if default is None:
        if not policy["push_default_branch"]:
            return refuse(f"refusing to push: the default branch of {remote} could not be determined ({why}), so Bridge cannot tell whether {remote_branch} is it", remote=remote, branch=remote_branch)
    elif remote_branch == default and not policy["push_default_branch"]:
        return refuse(f"refusing to push to the default branch {remote_branch} of {remote}; create the mission with --push-default-branch to allow it", remote=remote, branch=remote_branch)
    done = git(cwd, "push", remote, f"HEAD:refs/heads/{remote_branch}", timeout=PUSH_TIMEOUT_SECONDS)
    if done.code != 0:
        return refuse(f"git push to {remote} {remote_branch} failed: {done.err or done.out}", HOLD_PUSH_FAILED, remote=remote, branch=remote_branch)
    _event(store, mission_id, "task-pushed", {"task_id": task_id, "remote": remote, "branch": remote_branch, "sha": git(cwd, "rev-parse", "HEAD").out, "scope": "task" if task_id is not None else "mission"})
    return True


def orchestration_settings(config: dict[str, Any] | None) -> dict[str, Any]:
    """Commit, push, budget and hold values as ``mission settings`` and the status projection show them."""
    from .bridge_budget import budget_of

    config = config if isinstance(config, dict) else {}
    return {**policy_of(config), "budget": budget_of(config),
            "hold_reason": str(config.get("hold_reason") or "") or None, "hold_detail": str(config.get("hold_detail") or "") or None}


def retry_pending(store: Any, mission: dict[str, Any]) -> bool:
    """After a resume: redo the commit or push that failed, for its own task, before any later task starts.

    Returns False when it failed again (the mission is held again). The saved ``pending_publish`` is cleared on success.
    """
    pending = (mission.get("config") or {}).get("pending_publish")
    if not isinstance(pending, dict) or pending.get("task_id") is None:
        return True
    mission_id = str(mission["id"])
    task = store.get_task(int(pending["task_id"]))
    if pending.get("stage") == "commit":
        evidence = task.get("evidence") or []
        summary = str(evidence[-1].get("summary", "")) if evidence and isinstance(evidence[-1], dict) else ""
        ok = commit_task(store, mission, task, summary)
    else:
        ok = push_branch(store, mission, task_id=int(task["id"]))
    if ok:
        store.set_mission_config_values(mission_id, {"pending_publish": None}, event_kind="mission-hold-released")
    return ok


def push_mission(store: Any, mission: dict[str, Any]) -> bool:
    """The ``push = per-mission`` push when the last task has passed; True when nothing is to be done or it succeeded."""
    if policy_of(mission.get("config"))["push"] != "per-mission":
        return True
    return push_branch(store, mission)
