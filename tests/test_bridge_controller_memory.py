"""The mission controller must not keep one new string per provider run (PRD-007 soak finding).

CPython 3.12 makes every interned string immortal, and ``pathlib`` interns each path component it parses.
A controller that parsed the iteration directory, the verifier files and the pid of each provider run
therefore kept about five strings per run for as long as it lived.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from llm_tools import bridge_runtime, common, fileio
from llm_tools.bridge_runtime import BridgeMissionHooks, run_controller
from llm_tools.bridge_store import BridgeStore

def _plain_pathlib_interns() -> bool:
    """Whether this interpreter's pathlib keeps every path component as an immortal interned string.

    CPython 3.12 makes interned strings immortal, and its pathlib interned each component when
    parsing. Later 3.12 patch releases (3.12.16 on the CI runners, for example) no longer intern
    in pathlib, so the leak and the measurements below exist only on the earlier ones (3.12.3 here).
    """
    if not hasattr(sys, "getunicodeinternedsize") or sys.version_info[:2] != (3, 12):
        return False
    before = sys.getunicodeinternedsize()
    for number in range(8):
        str(Path("/", f"pathlib-intern-probe-{os.getpid()}-{number}"))
    return sys.getunicodeinternedsize() - before >= 8


needs_interned_count = pytest.mark.skipif(
    not _plain_pathlib_interns(),
    reason="only CPython 3.12 releases whose pathlib interns path components keep run-specific names forever",
)
needs_proc = pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads process state from /proc (Linux only)")


class _NoWorker:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


@needs_interned_count
def test_run_path_does_not_intern_run_specific_names() -> None:
    before = sys.getunicodeinternedsize()
    for number in range(50):
        path = common.RunPath("/tmp", f"run-specific-name-{number}-{id(object())}", "attempt-1.out")
        assert path.name == "attempt-1.out"
        assert path.parent.name.startswith("run-specific-name-")
        assert str(path).count("/") == 3
        assert path.with_suffix(".json").suffix == ".json"
        assert isinstance(path.parent, common.RunPath)
        assert isinstance(path / "x", common.RunPath)
    assert sys.getunicodeinternedsize() - before <= 2  # not the 100 that plain pathlib keeps


@needs_interned_count
def test_plain_pathlib_interns_the_same_names() -> None:
    """The control for the test above: plain ``Path`` parsing does keep the names."""
    before = sys.getunicodeinternedsize()
    for number in range(50):
        str(Path("/tmp", f"control-specific-name-{number}-{id(object())}", "attempt-1.out"))
    assert sys.getunicodeinternedsize() - before >= 50


def test_run_path_behaves_like_path(tmp_path: Path) -> None:
    run = common.RunPath(tmp_path, "iter-001-codex")
    run.mkdir()
    (run / "attempt-1.out").write_text("x", encoding="utf-8")
    assert run == Path(tmp_path / "iter-001-codex")
    assert hash(run) == hash(Path(tmp_path / "iter-001-codex"))
    assert sorted(item.name for item in run.glob("attempt-*.out")) == ["attempt-1.out"]
    assert (run / "attempt-1.out").relative_to(tmp_path) == Path("iter-001-codex/attempt-1.out")
    assert (run / ".." / "iter-001-codex").resolve() == run.resolve()
    assert common.RunPath("a//b/./c").parts == ("a", "b", "c")
    assert common.RunPath("").parts == ()
    assert common.RunPath("/").parts == ("/",)


@needs_proc
def test_proc_files_are_read_by_string_name() -> None:
    assert fileio.read_proc_text(f"/proc/{os.getpid()}/stat").startswith(f"{os.getpid()} ")


def _start_controller_with_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tasks: int) -> tuple[Path, str]:
    state = tmp_path / "state"
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    provider = tmp_path / "provider.py"
    provider.write_text("print('provider run complete')\n", encoding="utf-8")
    for name, value in {
        "LLM_BRIDGE_STATE_DIR": str(state), "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"), "LLM_TOOLS_NO_INHIBIT": "1", "LLM_USAGE_NO_SERVICE": "1",
        "LLM_SCHEDULER_NO_ACTUAL_SUSPEND": "1", "LLM_BRIDGE_NO_SIGNIN_CHECK": "1",
        "LLM_SCHEDULER_USAGE_JSON": json.dumps({"codex": {"available": True, "five_hour": {"remaining": 80, "resets_at": 4102444800}, "week": {"remaining": 60, "resets_at": 4102444800}}}),
    }.items():
        monkeypatch.setenv(name, value)
    work = tmp_path / "work"
    work.mkdir()
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission(
            "memory", "objective", cwd=work,
            config={
                "headless": True, "providers": "codex", "command_template": shlex.join([sys.executable, str(provider)]),
                "command_template_providers": "codex", "verifier": {"argv": [sys.executable, "-c", "pass"], "timeout_seconds": 30},
                "frozen_tool_config": "[ralph]\nproviders = ['codex']\n",
            },
        )
        for index in range(tasks):
            store.add_task(mission["id"], f"work {index}")
    monkeypatch.setattr(bridge_runtime, "CapacityRefreshWorker", _NoWorker)
    return state, str(mission["id"])


@needs_interned_count
@needs_proc
def test_controller_keeps_no_strings_per_provider_run(tmp_path, monkeypatch) -> None:
    """Real controller code path: iteration directory, provider pid, verifier files and log pruning, 24 runs.

    Before the fix each run left five interned strings (the iteration directory, two pids and two verifier
    file names), so the count grew by about 100 between the 6th and the 24th run.
    """
    runs = 24
    warm_up = 6
    state, mission_id = _start_controller_with_tasks(tmp_path, monkeypatch, runs)
    sizes: list[int] = []
    original = BridgeMissionHooks.after_iteration

    def record(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        sizes.append(sys.getunicodeinternedsize())
        return result

    monkeypatch.setattr(BridgeMissionHooks, "after_iteration", record)
    assert run_controller(mission_id, headless=True) == 0
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mission_id)["status"] == "COMPLETED"
    assert len(sizes) >= runs
    growth = sizes[-1] - sizes[warm_up - 1]
    assert growth <= 4, f"{growth} interned strings after {len(sizes) - warm_up} more provider runs"
