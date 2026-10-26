"""Bridge-only output retention stays bounded and leaves no open sink."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from llm_tools.bridge_logs import _append_rotating, capture_process_output
from llm_tools.bridge_log_sink import main as log_sink_main
from llm_tools.bridge_runtime import _prune_old_run_logs
from llm_tools.bridge_store import BridgeStore
from llm_tools import common


def test_controller_log_sink_rotates_and_closes_fds(tmp_path: Path) -> None:
    path = tmp_path / "controller.log"
    with capture_process_output(path, max_bytes=1024, rotations=2):
        os.write(1, b"prefix\n" + b"x" * 5000 + b"\nlast-line\n")
        os.write(2, b"stderr-line\n")

    segments = [path, path.with_name(path.name + ".1"), path.with_name(path.name + ".2")]
    existing = [segment for segment in segments if segment.exists()]
    assert existing
    assert sum(segment.stat().st_size for segment in existing) <= 3 * 1024
    assert all(segment.stat().st_size <= 1024 for segment in existing)
    assert b"last-line" in path.read_bytes()
    assert b"stderr-line" in path.read_bytes()
    # A fresh write after context exit reaches pytest's original stdout, not a
    # dangling closed pipe owned by the rotation thread.
    os.write(1, b"")


def test_tmux_sink_cli_rotates_input_without_unbounded_append(tmp_path: Path) -> None:
    path = tmp_path / "attempt-1.out"
    result = subprocess.run(
        [sys.executable, "-m", "llm_tools.bridge_log_sink", "--path", str(path), "--max-bytes", "1024", "--rotations", "1"],
        input=b"a" * 3000 + b"THE-END",
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert path.stat().st_size <= 1024
    assert path.with_name(path.name + ".1").stat().st_size <= 1024
    assert b"THE-END" in path.read_bytes()


def test_bridge_headless_pty_capture_keeps_only_bounded_tail(tmp_path: Path) -> None:
    output = tmp_path / "attempt.out"
    status = tmp_path / "attempt.status"
    code, text = common.run_pty_capture(
        [sys.executable, "-c", "import sys; sys.stdout.write('a'*20000+'FINAL')"],
        tmp_path,
        10,
        stream=False,
        auto_confirm=False,
        output_path=output,
        status_path=status,
        max_output_bytes=1024,
    )
    assert code == 0
    assert output.stat().st_size < 1200
    assert "FINAL" in text
    assert "Earlier bytes were discarded" in text


def test_run_retention_prunes_raw_logs_but_keeps_digest_event(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("mission", "objective", cwd=tmp_path)
        task_id = store.add_task(mission["id"], "work")
        store.set_mission_status(mission["id"], "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.mark_operation_running(operation)
        store.finish_operation(operation, exit_code=0)
        log_root = tmp_path / "logs"
        old = log_root / "old-run"
        new = log_root / "new-run"
        old.mkdir(parents=True)
        new.mkdir()
        old_log = old / "attempt-1.out"
        old_log.write_bytes(b"retained audit material")
        new_log = new / "attempt-2.out"
        new_log.write_bytes(b"new")
        with store._transaction():
            store.db.execute("UPDATE operations SET logs_path=? WHERE id=?", (str(old_log), operation))
        os.utime(old, ns=(1_000_000_000, 1_000_000_000))

        _prune_old_run_logs(store, mission["id"], log_root, retain=1)

        assert old.exists() is False
        assert new.exists()
        assert store.get_operation(operation)["logs_path"] is None
        event = next(event for event in store.events(mission["id"]) if event["kind"] == "operation-log-pruned")
        assert event["data"]["size_bytes"] == len(b"retained audit material")
        assert len(event["data"]["sha256"]) == 64


def test_log_sink_cli_validates_bounds_and_consumes_stdin(tmp_path: Path, monkeypatch, capsys) -> None:
    data = iter((b"bounded input", b""))
    monkeypatch.setattr("llm_tools.bridge_logs.os.read", lambda _fd, _size: next(data))
    path = tmp_path / "cli.out"

    assert log_sink_main(["--path", str(path), "--max-bytes", "1024", "--rotations", "1"]) == 0
    assert path.read_bytes() == b"bounded input"
    assert log_sink_main(["--path", str(path), "--max-bytes", "1", "--rotations", "1"]) == 2
    assert "invalid Bridge log retention values" in capsys.readouterr().err


def test_log_sink_rotation_failure_discards_old_segment_and_keeps_new_output(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "attempt.out"
    path.write_bytes(b"o" * 1024)

    def fail_replace(*_args):
        raise PermissionError("rotation blocked")

    monkeypatch.setattr("llm_tools.bridge_logs.os.replace", fail_replace)
    _append_rotating(path, b"new-output", max_bytes=1024, rotations=2)

    assert path.read_bytes() == b"new-output"
    assert path.stat().st_size <= 1024


def test_rotating_sink_retries_short_os_writes(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "short-write.out"
    actual_write = os.write

    def short_write(fd: int, data: bytes | memoryview) -> int:
        return actual_write(fd, data[: max(1, len(data) // 2)])

    monkeypatch.setattr("llm_tools.bridge_logs.os.write", short_write)
    _append_rotating(path, b"complete output", max_bytes=1024, rotations=1)

    assert path.read_bytes() == b"complete output"
