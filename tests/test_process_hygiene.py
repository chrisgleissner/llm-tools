"""The harness must stop, and fail, a test that leaves a live process behind."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests import process_hygiene as hygiene
from tests.conftest import ROOT, wait_for

SLEEPER = "import time\ntime.sleep(60)\n"


def sleeper(cwd: Path, **kwargs) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", SLEEPER], cwd=cwd, **kwargs)


def test_a_new_child_of_pytest_is_a_candidate_and_an_earlier_one_is_not(tmp_path: Path) -> None:
    old = sleeper(tmp_path)
    try:
        before = hygiene.live_pids()
        new = sleeper(tmp_path)
        try:
            found = hygiene.live_candidates(before, [])
            assert new.pid in found
            assert old.pid not in found
        finally:
            hygiene.terminate_process_tree(new)
    finally:
        hygiene.terminate_process_tree(old)


def test_a_detached_process_is_attributed_by_the_test_directory_it_references(tmp_path: Path) -> None:
    before = hygiene.live_pids()
    mine = tmp_path / "test_case1"
    other = tmp_path / "test_case10"
    mine.mkdir()
    other.mkdir()
    # A double fork detaches the process from pytest, as a tmux server does.
    launcher = textwrap.dedent(
        """
        import os, sys, time
        if os.fork():
            os._exit(0)
        os.setsid()
        open(sys.argv[1], "w").write(str(os.getpid()))
        time.sleep(60)
        """
    )
    pids = []
    for directory in (mine, other):
        subprocess.run([sys.executable, "-c", launcher, str(directory / "pid")], cwd=directory, check=True)
        wait_for(lambda d=directory: (d / "pid").exists() and (d / "pid").read_text(), 5)
        pids.append(int((directory / "pid").read_text()))
    try:
        found = hygiene.live_candidates(before, hygiene.path_patterns([str(mine)]))
        assert pids[0] in found
        assert pids[1] not in found  # test_case10 is not test_case1
        assert hygiene.reap_leaks(before, [str(mine)])  # reported, and stopped
        assert wait_for(lambda: not hygiene.is_alive(pids[0]), 5)
        assert hygiene.is_alive(pids[1])
    finally:
        hygiene.terminate_processes(pids, grace=1.0)


def test_exited_children_and_zombies_are_not_leaks(tmp_path: Path) -> None:
    before = hygiene.live_pids()
    done = subprocess.Popen([sys.executable, "-c", "pass"], cwd=tmp_path)
    wait_for(lambda: (info := hygiene.proc_stat(done.pid)) is None or info[0] == "Z", 5)  # exited, not yet reaped
    try:
        assert done.pid not in hygiene.live_candidates(before, [])
        assert hygiene.reap_leaks(before, [str(tmp_path)]) == []
    finally:
        done.wait()


def test_terminate_process_tree_stops_the_group_escalates_and_reaps(tmp_path: Path) -> None:
    program = textwrap.dedent(
        """
        import os, signal, subprocess, sys, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # only SIGKILL stops it
        grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        print(grandchild.pid, flush=True)
        time.sleep(60)
        """
    )
    process = subprocess.Popen([sys.executable, "-c", program], cwd=tmp_path, stdout=subprocess.PIPE, text=True,
                               start_new_session=True)
    assert process.stdout is not None
    grandchild = int(process.stdout.readline())
    assert hygiene.is_alive(grandchild)
    hygiene.terminate_process_tree(process, grace=0.5)
    assert process.poll() is not None  # reaped
    assert wait_for(lambda: not hygiene.is_alive(grandchild), 5)
    process.stdout.close()


def test_terminate_process_tree_is_harmless_for_an_exited_child(tmp_path: Path) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], cwd=tmp_path, start_new_session=True)
    process.wait()
    hygiene.terminate_process_tree(process)


def test_stale_runtime_dirs_stop_processes_of_dead_sessions_only(tmp_path: Path) -> None:
    parent = tmp_path / "tmp"
    parent.mkdir()
    dead = parent / "lbt-dead"
    live = parent / "lbt-live"
    young = parent / "lbt-young-no-owner"
    for directory in (dead, live, young):
        directory.mkdir()
    (dead / hygiene.OWNER_FILE).write_text("999999 1\n")  # no such process
    me = os.getpid()
    (live / hygiene.OWNER_FILE).write_text(f"{me} {hygiene.start_token(me)}\n")
    processes = {name: sleeper(directory, env=dict(os.environ, XDG_RUNTIME_DIR=str(directory)))
                 for name, directory in (("dead", dead), ("live", live), ("young", young))}
    try:
        assert hygiene.stale_runtime_dirs(str(parent)) == [str(dead)]
        assert hygiene.sweep_stale_runtime_dirs(str(parent)) == [str(dead)]
        processes["dead"].wait(timeout=5)  # stopped (and reaped) by the sweep
        assert processes["live"].poll() is None
        assert processes["young"].poll() is None  # too young to know that nobody owns it
    finally:
        for process in processes.values():
            hygiene.terminate_process_tree(process)


def test_an_ownerless_dir_is_stale_only_once_it_is_old(tmp_path: Path) -> None:
    parent = tmp_path / "tmp"
    parent.mkdir()
    old = parent / "lbt-old"
    old.mkdir()
    os.utime(old, (1, 1))
    assert hygiene.stale_runtime_dirs(str(parent)) == [str(old)]


def test_write_owner_records_this_process(tmp_path: Path) -> None:
    hygiene.write_owner(str(tmp_path))
    pid, token = (tmp_path / hygiene.OWNER_FILE).read_text().split()
    assert int(pid) == os.getpid()
    assert token == hygiene.start_token(os.getpid()) != ""


def test_the_session_guard_fails_a_test_that_leaves_a_process_and_stops_it(tmp_path: Path) -> None:
    pidfile = tmp_path / "leaked.pid"
    probe = tmp_path / "test_leaky.py"
    probe.write_text(textwrap.dedent(
        f"""
        import subprocess, sys

        def test_leaks_a_child():
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            open({str(pidfile)!r}, "w").write(str(child.pid))

        def test_cleans_up_after_itself():
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            child.kill()
            child.wait()
        """
    ))
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest",
         "--rootdir", str(tmp_path), str(probe)],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=120,
    )
    assert "2 passed, 1 error" in result.stdout, result.stdout + result.stderr  # the leak is a teardown error
    assert "test left live processes behind" in result.stdout
    leaked = int(pidfile.read_text())
    assert wait_for(lambda: not hygiene.is_alive(leaked), 5), "the guard must stop what the test left"
