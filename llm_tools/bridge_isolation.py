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
    return {"isolation": ISOLATION_WORKTREE, "origin_cwd": str(origin), "branch": branch, "base_commit": base,
            "worktree_path": str(path), "cwd": str(effective)}


def discard(origin_cwd: str | Path, worktree: str | Path, branch: str) -> None:
    """Undo ``prepare`` after a later step failed; never raises."""
    try:
        _git(origin_cwd, "worktree", "remove", "--force", str(worktree))
        _git(origin_cwd, "branch", "-D", branch)
    except IsolationError:
        pass
    shutil.rmtree(worktree, ignore_errors=True)


def release(mission: dict[str, Any]) -> dict[str, Any]:
    """Remove a deleted mission's worktree and branch when no work would be lost.

    Work is lost when the worktree has uncommitted or untracked changes, or when the branch holds commits
    that the repository's checked-out commit does not contain (not merged). In either case, and when
    that cannot be established, the worktree and branch stay. The result has ``removed`` (bool), ``path``,
    ``branch`` and a ``reason`` when kept.
    """
    path = Path(str(mission.get("worktree_path") or ""))
    branch = str(mission.get("branch") or "")
    origin = str(mission.get("origin_cwd") or "")
    result: dict[str, Any] = {"removed": False, "path": str(path), "branch": branch}
    if not origin or not Path(origin).is_dir():
        result["reason"] = f"the source repository {origin or '(unknown)'} is not available to check for unmerged work"
        return result
    try:
        if path.is_dir():
            state = _git(path, "status", "--porcelain")
            if state.returncode != 0:
                result["reason"] = f"git status failed in the worktree: {state.stderr.strip()}"
                return result
            if state.stdout.strip():
                result["reason"] = "the worktree has uncommitted changes"
                return result
        exists = _git(origin, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if exists.returncode == 0:
            merged = _git(origin, "merge-base", "--is-ancestor", f"refs/heads/{branch}", "HEAD")
            if merged.returncode != 0:
                result["reason"] = f"branch {branch} has commits that are not merged into the repository's checked-out commit"
                return result
        if path.is_dir():
            gone = _git(origin, "worktree", "remove", str(path))
            if gone.returncode != 0:
                result["reason"] = f"git could not remove the worktree: {gone.stderr.strip()}"
                return result
        _git(origin, "worktree", "prune")
        if exists.returncode == 0:
            _git(origin, "branch", "-d", branch)
    except IsolationError as exc:
        result["reason"] = str(exc)
        return result
    result["removed"] = True
    return result
