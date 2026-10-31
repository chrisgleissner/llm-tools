"""Nothing in the product may change the process-wide umask.

The umask is shared by every thread. Bridge runs background reads and capacity
refreshes next to the console, so a temporary ``os.umask`` in one thread gives
files and directories created concurrently by another thread the wrong mode.
"""
from __future__ import annotations

import os
import re
import stat
import threading
from pathlib import Path

import pytest

from llm_tools import common, usage_service

PACKAGE = Path(common.__file__).resolve().parent
UMASK_CALL = re.compile(r"\bos\.umask\s*\(|\bfrom\s+os\s+import\b[^\n]*\bumask\b")


def test_no_module_in_the_package_calls_umask() -> None:
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if UMASK_CALL.search(line.split("#", 1)[0]):
                offenders.append(f"{path.relative_to(PACKAGE.parent)}:{number}")
    assert offenders == []


class _Sampler:
    def snapshot(self, max_age=None):  # type: ignore[no-untyped-def]
        return {}


def _concurrent_creator(root: Path, stop: threading.Event, started: threading.Event, wrong: list[str]) -> threading.Thread:
    """Keep creating 0755 directories and 0644 files with an explicit mode, as another Bridge thread would.

    With the process umask at 022 those modes must come out unchanged. A temporary
    ``os.umask(0o077)`` elsewhere in the process would turn them into 0700 and 0600.
    """

    def run() -> None:
        started.set()
        count = 0
        while not stop.is_set():
            count += 1
            directory = root / f"d{count}"
            os.mkdir(directory, 0o755)
            dir_mode = stat.S_IMODE(directory.stat().st_mode)
            if dir_mode != 0o755:
                wrong.append(f"dir {dir_mode:o}")
            file_path = root / f"f{count}"
            os.close(os.open(file_path, os.O_WRONLY | os.O_CREAT, 0o644))
            file_mode = stat.S_IMODE(file_path.stat().st_mode)
            if file_mode != 0o644:
                wrong.append(f"file {file_mode:o}")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_modes_of_entries_created_by_another_thread_are_unchanged_by_run_log_setup(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        other = tmp_path / "other"
        other.mkdir()
        stop, started, wrong = threading.Event(), threading.Event(), []
        thread = _concurrent_creator(other, stop, started, wrong)
        assert started.wait(5)
        try:
            for index in range(300):
                logs = common.setup_run_logs(tmp_path / "logs" / f"n{index}", "t", provider_link="x")
                assert stat.S_IMODE(logs.run_dir.stat().st_mode) == 0o700
                assert stat.S_IMODE(logs.text_log.stat().st_mode) == 0o600
                assert stat.S_IMODE(logs.event_log.stat().st_mode) == 0o600
        finally:
            stop.set()
            thread.join(5)
        assert wrong == []
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(old)


def test_modes_of_entries_created_by_another_thread_are_unchanged_while_a_socket_is_bound(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        other = tmp_path / "other"
        other.mkdir()
        private = tmp_path / "svc"
        private.mkdir(mode=0o700)
        stop, started, wrong = threading.Event(), threading.Event(), []
        thread = _concurrent_creator(other, stop, started, wrong)
        assert started.wait(5)
        try:
            for index in range(100):
                sock = private / f"s{index}.sock"
                server = usage_service.ThreadingUnixServer(str(sock), _Sampler(), threading.Event())  # type: ignore[arg-type]
                try:
                    assert stat.S_IMODE(sock.stat().st_mode) == 0o600
                    assert server.server_address == str(sock)
                finally:
                    server.server_close()
        finally:
            stop.set()
            thread.join(5)
        assert wrong == []
        assert [p.name for p in private.iterdir() if p.name.startswith(".b")] == []
    finally:
        os.umask(old)


def test_modes_of_entries_created_by_another_thread_are_unchanged_by_shared_snapshot_setup(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        other = tmp_path / "other"
        other.mkdir()
        stop, started, wrong = threading.Event(), threading.Event(), []
        thread = _concurrent_creator(other, stop, started, wrong)
        assert started.wait(5)
        try:
            for index in range(200):
                env = {"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / f"cache{index}")}
                value = common.shared_usage_snapshot("probe", env, 60.0, lambda: {"available": True})
                assert value == {"available": True}
                directory = common.shared_snapshot_dir(env)
                assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        finally:
            stop.set()
            thread.join(5)
        assert wrong == []
    finally:
        os.umask(old)


def test_make_private_dirs_is_idempotent_and_rejects_a_file(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        target = tmp_path / "a" / "b" / "c"
        common.make_private_dirs(target)
        common.make_private_dirs(target)
        for part in (target, target.parent, target.parent.parent):
            assert stat.S_IMODE(part.stat().st_mode) == 0o700
        blocker = tmp_path / "file"
        blocker.write_text("x")
        with pytest.raises(OSError):
            common.make_private_dirs(blocker)
    finally:
        os.umask(old)


def test_touch_private_file_tightens_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_text("keep")
    path.chmod(0o666)
    common.touch_private_file(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == "keep"
