"""Worktree isolation for missions (ORCH-001).

``mission create --isolate worktree`` runs the mission in its own git worktree, created under the
Bridge state directory on the branch ``bridge/<mission-slug>`` from the commit that is checked out
when the mission is created. The branch lives in the operator's repository, so the work comes back
with an ordinary ``git merge bridge/<slug>`` (or a pull request) from that repository.

All git calls go through ``git -C <dir>`` with a timeout and return plain values; nothing here
touches the database.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

ISOLATION_NONE = "none"
ISOLATION_WORKTREE = "worktree"
ISOLATION_CHOICES = (ISOLATION_NONE, ISOLATION_WORKTREE)
BRANCH_PREFIX = "bridge/"
_GIT_TIMEOUT_SECONDS = 30


class IsolationError(ValueError):
    """The mission cannot be isolated; the message says why and what to do."""


def _git(directory: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(directory), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=_GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolationError(f"git could not run in {directory}: {exc}") from exc


def mission_slug(name: str) -> str:
    """A lower-case branch-safe form of the mission name (at most 40 characters)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return slug or "mission"


def branch_name(name: str, mission_id: str) -> str:
    """``bridge/<slug>-<first 8 characters of the mission id>``; the id suffix keeps two missions with one name apart."""
    return f"{BRANCH_PREFIX}{mission_slug(name)}-{mission_id[:8]}"


def worktrees_root(state_dir: Path) -> Path:
    return state_dir / "worktrees"


def prepare(origin_cwd: str | Path, *, mission_id: str, name: str, state_dir: Path) -> dict[str, Any]:
    """Create the worktree and branch for a new mission and return its facts.

    Refuses a folder that is not in a git repository, a repository without a commit, and a working
    tree with uncommitted or untracked changes (they would not be in the worktree). The returned
    ``cwd`` is the folder the providers work in: the worktree root, or the same sub-folder of it when
    ``origin_cwd`` is below the repository root.
    """
    origin = Path(origin_cwd).expanduser().resolve(strict=True)
    top = _git(origin, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise IsolationError(f"cannot isolate {origin}: it is not inside a git repository; run the mission in place (omit --isolate) or run git init first")
    root = Path(top.stdout.strip()).resolve()
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        raise IsolationError(f"cannot isolate {root}: the repository has no commit yet; commit first or omit --isolate")
    dirty = _git(root, "status", "--porcelain")
    if dirty.returncode != 0:
        raise IsolationError(f"cannot read the git status of {root}: {dirty.stderr.strip()}")
    if dirty.stdout.strip():
        raise IsolationError(f"cannot isolate {root}: the working tree has uncommitted changes, which the worktree would not contain; commit or stash them, or omit --isolate")
    base = head.stdout.strip()
    current = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    origin_branch = current.stdout.strip() if current.returncode == 0 else ""
    branch = branch_name(name, mission_id)
    path = worktrees_root(state_dir) / mission_id
    if path.exists():
        raise IsolationError(f"cannot isolate: {path} already exists")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    made = _git(root, "worktree", "add", "-b", branch, str(path), base)
    if made.returncode != 0:
        raise IsolationError(f"git could not create the worktree: {made.stderr.strip() or made.stdout.strip()}")
    try:
        relative = origin.relative_to(root)
    except ValueError:
        relative = Path(".")
    effective = (path / relative).resolve()
    return {"isolation": ISOLATION_WORKTREE, "origin_cwd": str(origin), "branch": branch, "base_commit": base, "origin_branch": origin_branch or None,
            "worktree_path": str(path), "cwd": str(effective)}


def discard(origin_cwd: str | Path, worktree: str | Path, branch: str) -> None:
    """Undo ``prepare`` after a later step failed; never raises."""
    try:
        _git(origin_cwd, "worktree", "remove", "--force", str(worktree))
        _git(origin_cwd, "branch", "-D", branch)
    except IsolationError:
        pass
    shutil.rmtree(worktree, ignore_errors=True)


def unmerged_reason(mission: dict[str, Any]) -> str | None:
    """Why deleting the mission's worktree would lose work or cannot be shown safe; ``None`` when it is clean and merged.

    The worktree must have no uncommitted changes (``git status --porcelain``: untracked files that are not
    git-ignored count, ignored ones do not), and the branch must be an ancestor of the branch the mission was created
    from (``origin_branch``; the repository's checked-out commit when that was a detached HEAD).
    """
    path = Path(str(mission.get("worktree_path") or ""))
    branch = str(mission.get("branch") or "")
    origin = str(mission.get("origin_cwd") or "")
    if not origin or not Path(origin).is_dir():
        return f"the source repository {origin or '(unknown)'} is not available to check for unmerged work"
    target = str(mission.get("origin_branch") or "")
    try:
        if path.is_dir():
            state = _git(path, "status", "--porcelain")
            if state.returncode != 0:
                return f"git status failed in the worktree: {state.stderr.strip()}"
            if state.stdout.strip():
                return "the worktree has uncommitted changes"
        if _git(origin, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
            return None  # the branch is already gone; nothing is left to lose
        into = f"refs/heads/{target}" if target else "HEAD"
        if target and _git(origin, "rev-parse", "--verify", "--quiet", into).returncode != 0:
            return f"branch {target}, which the mission was created from, no longer exists, so merging cannot be checked"
        if _git(origin, "merge-base", "--is-ancestor", f"refs/heads/{branch}", into).returncode != 0:
            return f"branch {branch} has commits that are not merged into {target or 'the checked-out commit'}"
    except IsolationError as exc:
        return str(exc)
    return None


def release(mission: dict[str, Any]) -> dict[str, Any]:
    """Remove a deleted mission's worktree and branch when ``unmerged_reason`` finds nothing to lose; otherwise keep them.

    The result has ``removed`` (bool), ``path``, ``branch`` and a ``reason`` when kept.
    """
    path = Path(str(mission.get("worktree_path") or ""))
    branch = str(mission.get("branch") or "")
    origin = str(mission.get("origin_cwd") or "")
    result: dict[str, Any] = {"removed": False, "path": str(path), "branch": branch}
    reason = unmerged_reason(mission)
    if reason:
        result["reason"] = reason
        return result
    try:
        if path.is_dir():
            gone = _git(origin, "worktree", "remove", str(path))
            if gone.returncode != 0:
                result["reason"] = f"git could not remove the worktree: {gone.stderr.strip()}"
                return result
        _git(origin, "worktree", "prune")
        _git(origin, "branch", "-D", branch)  # unmerged_reason showed the branch holds nothing the origin branch lacks
    except IsolationError as exc:
        result["reason"] = str(exc)
        return result
    result["removed"] = True
    return result
