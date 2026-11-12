"""ORCH-001 worktree isolation and ORCH-002 several active missions per repository.

Every repository here is a throw-away one under ``tmp_path``; Bridge state and the runtime directory are private
temporary directories. Nothing touches the real repository or the operator's Bridge state.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from llm_tools import bridge_isolation
from llm_tools.bridge import main
from llm_tools.bridge_commands import OPERATIONS
from llm_tools.bridge_runtime import ralph_argv
from llm_tools.bridge_store import BridgeStore, TransitionError


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    return directory


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture()
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "first")
    return path


def _create(capsys, repo: Path, *extra: str, name: str = "Fix the tests") -> dict:
    assert main(["mission", "create", "--prompt", "Do it", "--name", name, "--cwd", str(repo), *extra, "--json"]) == 0
    return json.loads(capsys.readouterr().out)["mission"]


def _activate(mission_id: str) -> None:
    with BridgeStore(_path()) as store:
        store.set_mission_status(mission_id, "RUNNING")


def _path() -> Path:
    from llm_tools.bridge_store import store_path

    return store_path()


def test_create_isolated_makes_a_worktree_on_a_bridge_branch_from_the_current_commit(repo, state, capsys) -> None:
    mission = _create(capsys, repo, "--isolate", "worktree")
    worktree = Path(mission["worktree_path"])
    assert mission["isolation"] == "worktree"
    assert worktree == state / "worktrees" / mission["id"]
    assert mission["cwd"] == str(worktree)
    assert mission["branch"].startswith("bridge/fix-the-tests-")
    assert (worktree / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == mission["branch"]
    assert _git(worktree, "rev-parse", "HEAD") == _git(repo, "rev-parse", "HEAD") == mission["base_commit"]
    assert mission["origin_cwd"] == str(repo)
    assert mission["branch"] in _git(repo, "branch", "--list")  # the operator gets the work back from this repository
    assert _git(repo, "status", "--porcelain") == ""  # the source checkout is untouched


def test_default_runs_in_place(repo, state, capsys) -> None:
    mission = _create(capsys, repo)
    assert mission["isolation"] == "none" and mission["cwd"] == str(repo)
    assert mission["branch"] is None and not (state / "worktrees").exists()


def test_the_launched_provider_works_in_the_worktree(repo, state, capsys) -> None:
    mission = _create(capsys, repo, "--isolate", "worktree")
    with BridgeStore(_path(), readonly=True) as store:
        stored = store.get_mission(mission["id"])
    argv = ralph_argv(stored, state / "runtime", providers="codex", routes="")
    assert argv[argv.index("--cwd") + 1] == mission["worktree_path"] != str(repo)


def test_isolating_from_a_subfolder_keeps_the_subfolder(repo, state, capsys) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg" / "a.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "pkg")
    _git(repo, "commit", "-q", "-m", "pkg")
    mission = _create(capsys, repo / "pkg", "--isolate", "worktree")
    assert mission["cwd"] == str(Path(mission["worktree_path"]) / "pkg")


def test_refuses_a_dirty_tree_a_non_git_folder_and_an_empty_repository(repo, state, tmp_path, capsys) -> None:
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    assert main(["mission", "create", "--prompt", "Do it", "--cwd", str(repo), "--isolate", "worktree"]) != 0
    assert "uncommitted changes" in capsys.readouterr().err
    plain = tmp_path / "plain"
    plain.mkdir()
    assert main(["mission", "create", "--prompt", "Do it", "--cwd", str(plain), "--isolate", "worktree"]) != 0
    assert "not inside a git repository" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    assert main(["mission", "create", "--prompt", "Do it", "--cwd", str(empty), "--isolate", "worktree"]) != 0
    assert "no commit yet" in capsys.readouterr().err
    assert "branch" not in _git(repo, "branch", "--list", "bridge/*")  # nothing was left behind
    assert not (state / "worktrees").exists() or not any((state / "worktrees").iterdir())


def test_a_failed_creation_removes_the_worktree_and_branch(repo, state, monkeypatch) -> None:
    def boom(self, *args, **kwargs):
        raise TransitionError("boom")

    monkeypatch.setattr(BridgeStore, "create_mission", boom)
    assert main(["mission", "create", "--prompt", "Do it", "--cwd", str(repo), "--isolate", "worktree"]) != 0
    assert _git(repo, "branch", "--list", "bridge/*") == ""
    assert list((state / "worktrees").iterdir()) == []


def test_two_isolated_missions_on_one_repository_run_at_once(repo, state, capsys) -> None:
    first = _create(capsys, repo, "--isolate", "worktree", name="Same name")
    second = _create(capsys, repo, "--isolate", "worktree", name="Same name")
    assert first["cwd"] != second["cwd"] and first["branch"] != second["branch"]
    assert first["repo_identity"] != second["repo_identity"]  # the uniqueness rule sees two directories
    _activate(first["id"])
    _activate(second["id"])
    with BridgeStore(_path(), readonly=True) as store:
        assert {m["status"] for m in store.list_missions()} == {"RUNNING"}


def test_two_in_place_missions_still_conflict(repo, state, capsys) -> None:
    first = _create(capsys, repo)
    _activate(first["id"])
    assert main(["mission", "create", "--prompt", "Do it", "--cwd", str(repo)]) != 0
    assert "an active mission already owns worktree" in capsys.readouterr().err


def test_an_isolated_and_an_in_place_mission_can_share_a_repository(repo, state, capsys) -> None:
    placed = _create(capsys, repo)
    _activate(placed["id"])
    isolated = _create(capsys, repo, "--isolate", "worktree")
    _activate(isolated["id"])


def test_the_uniqueness_index_uses_the_effective_directory(repo, state, capsys) -> None:
    """Two active missions cannot share one working directory, even when one of them is isolated."""
    isolated = _create(capsys, repo, "--isolate", "worktree")
    _activate(isolated["id"])
    with pytest.raises(TransitionError, match="already owns worktree"):
        with BridgeStore(_path()) as store:
            store.create_mission("again", "x", cwd=isolated["cwd"])
    with BridgeStore(_path()) as store:
        other = store.create_mission("elsewhere", "x", cwd=repo)  # the source repository is a different directory
        store.set_mission_status(other["id"], "RUNNING")


def test_delete_removes_a_worktree_without_unmerged_work(repo, state, capsys) -> None:
    mission = _create(capsys, repo, "--isolate", "worktree")
    assert main(["mission", "delete", "--mission", mission["id"], "--yes", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["worktree"]["removed"] is True
    assert not Path(mission["worktree_path"]).exists()
    assert _git(repo, "branch", "--list", "bridge/*") == ""
    assert "removed worktree" in result["detail"]


def _commit_in(worktree: Path) -> None:
    (worktree / "new.txt").write_text("work\n", encoding="utf-8")
    _git(worktree, "add", "new.txt")
    _git(worktree, "commit", "-q", "-m", "work")


def _still_exists(mission_id: str) -> bool:
    with BridgeStore(_path(), readonly=True) as store:
        return any(m["id"] == mission_id for m in store.list_missions())


def test_delete_refuses_unmerged_commits_naming_worktree_branch_and_commands(repo, state, capsys) -> None:
    mission = _create(capsys, repo, "--isolate", "worktree")
    worktree = Path(mission["worktree_path"])
    _commit_in(worktree)
    assert main(["mission", "delete", "--mission", mission["id"], "--yes"]) == 4
    error = capsys.readouterr().err
    assert str(worktree) in error and mission["branch"] in error
    assert f"git merge {mission['branch']}" in error and "git worktree remove --force" in error and "--force" in error
    assert _still_exists(mission["id"]) and (worktree / "new.txt").exists()


def test_delete_refuses_uncommitted_changes(repo, state, capsys) -> None:
    mission = _create(capsys, repo, "--isolate", "worktree")
    (Path(mission["worktree_path"]) / "scratch.txt").write_text("wip\n", encoding="utf-8")
    assert main(["mission", "delete", "--mission", mission["id"], "--yes"]) == 4
    assert "uncommitted" in capsys.readouterr().err
    assert _still_exists(mission["id"])


def test_delete_force_removes_the_mission_and_keeps_the_worktree_and_branch(repo, state, capsys) -> None:
    mission = _create(capsys, repo, "--isolate", "worktree")
    worktree = Path(mission["worktree_path"])
    _commit_in(worktree)
    assert main(["mission", "delete", "--mission", mission["id"], "--yes", "--force", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["worktree"]["removed"] is False and result["worktree"]["path"] == str(worktree)
    assert str(worktree) in result["detail"] and mission["branch"] in result["detail"]
    assert (worktree / "new.txt").exists() and mission["branch"] in _git(repo, "branch", "--list")
    assert not _still_exists(mission["id"])


def test_force_keeps_uncommitted_work_too(repo, state, capsys) -> None:
    mission = _create(capsys, repo, "--isolate", "worktree")
    (Path(mission["worktree_path"]) / "scratch.txt").write_text("wip\n", encoding="utf-8")
    assert main(["mission", "delete", "--mission", mission["id"], "--yes", "--force", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["worktree"]["removed"] is False
    assert (Path(mission["worktree_path"]) / "scratch.txt").exists()


def test_merged_means_merged_into_the_branch_the_mission_was_created_from(repo, state, capsys) -> None:
    _git(repo, "checkout", "-q", "-b", "dev")
    mission = _create(capsys, repo, "--isolate", "worktree")
    assert mission["origin_branch"] == "dev"
    worktree = Path(mission["worktree_path"])
    _commit_in(worktree)
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", mission["branch"])  # merged into main, which is checked out now, but not into dev
    assert main(["mission", "delete", "--mission", mission["id"], "--yes"]) == 4
    assert "not merged into dev" in capsys.readouterr().err
    _git(repo, "checkout", "-q", "dev")
    _git(repo, "merge", "-q", "--ff-only", mission["branch"])
    _git(repo, "checkout", "-q", "main")
    assert main(["mission", "delete", "--mission", mission["id"], "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["worktree"]["removed"] is True and not worktree.exists()


def test_git_ignored_untracked_files_do_not_block_isolation(repo, state, capsys) -> None:
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    (repo / "build.log").write_text("x\n", encoding="utf-8")
    assert _create(capsys, repo, "--isolate", "worktree")["isolation"] == "worktree"
    (repo / "stray.txt").write_text("x\n", encoding="utf-8")  # untracked and not ignored: refused
    assert main(["mission", "create", "--prompt", "Do it", "--cwd", str(repo), "--isolate", "worktree"]) != 0


def test_delete_removes_a_worktree_whose_branch_was_merged(repo, state, capsys) -> None:
    mission = _create(capsys, repo, "--isolate", "worktree")
    worktree = Path(mission["worktree_path"])
    (worktree / "new.txt").write_text("work\n", encoding="utf-8")
    _git(worktree, "add", "new.txt")
    _git(worktree, "commit", "-q", "-m", "work")
    _git(repo, "merge", "-q", "--ff-only", mission["branch"])
    assert main(["mission", "delete", "--mission", mission["id"], "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["worktree"]["removed"] is True
    assert (repo / "new.txt").exists() and not worktree.exists()


def test_status_and_list_show_the_isolation(repo, state, capsys) -> None:
    mission = _create(capsys, repo, "--isolate", "worktree")
    assert main(["status", "--mission", mission["id"]]) == 0
    text = capsys.readouterr().out
    assert f"Isolation   worktree on branch {mission['branch']}" in text and f"git merge {mission['branch']}" in text
    assert main(["mission", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["missions"][0]
    assert listed["isolation"] == "worktree" and listed["branch"] == mission["branch"]


def test_typed_command_accepts_isolate_and_the_remote_cli_still_names_it_unsupported() -> None:
    from llm_tools import bridge_remote
    from llm_tools.bridge import _build_parser

    assert "isolate" in OPERATIONS["mission.create"].params
    parsed = _build_parser().parse_args(["mission", "create", "--prompt", "x", "--cwd", ".", "--isolate", "worktree"])
    request, _ = bridge_remote.request_for(parsed, "ws")
    assert request.params["unsupported_options"] == ["--isolate"]  # remote acceptance is ORCH-008


def test_slug_and_branch_names() -> None:
    assert bridge_isolation.mission_slug("  Fix: the/Tests!! ") == "fix-the-tests"
    assert bridge_isolation.mission_slug("???") == "mission"
    assert bridge_isolation.branch_name("A b", "0123456789abcdef") == "bridge/a-b-01234567"
