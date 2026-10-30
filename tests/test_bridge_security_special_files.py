"""A FIFO, device or oversized file must not hang or exhaust a Bridge reader."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_sessions, bridge_viewer, common
from llm_tools.bridge_progress import repository_checkpoint


@contextmanager
def deadline(seconds: int = 8) -> Iterator[None]:
    """Fail instead of hanging when the code under test blocks on a FIFO."""
    def expired(*_args: object) -> None:
        raise TimeoutError("blocked on a special file")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def fifo(path: Path) -> Path:
    os.mkfifo(path)
    return path


def test_open_regular_file_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    pipe = fifo(tmp_path / "pipe")
    with deadline(), pytest.raises(OSError):
        common.open_regular_file(pipe)
    plain = tmp_path / "plain"
    plain.write_bytes(b"line one\nline two\n")
    with common.open_regular_file(plain) as stream:
        assert stream.readline() == b"line one\n"
    link = tmp_path / "link"
    link.symlink_to(plain)
    with pytest.raises(OSError):
        common.open_regular_file(link, follow_symlinks=False)


def test_view_refuses_a_fifo_a_device_and_a_directory(tmp_path: Path) -> None:
    pipe = fifo(tmp_path / "pipe")
    with deadline():
        for special in (pipe, Path("/dev/zero"), tmp_path):
            with pytest.raises(ValueError, match="not a regular file"):
                bridge_viewer.read_source(special)


def test_read_source_is_bounded_and_keeps_line_endings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document = tmp_path / "doc.md"
    document.write_bytes("a\r\nbé\n".encode())
    assert bridge_viewer.read_source(document) == "a\r\nbé\n"
    monkeypatch.setattr(bridge_viewer, "MAX_SOURCE_BYTES", 8)
    document.write_bytes(b"123456789")
    with pytest.raises(ValueError, match="larger than"):
        bridge_viewer.read_source(document)


def test_view_command_reports_a_fifo_as_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    pipe = fifo(tmp_path / "pipe")
    with deadline():
        assert bridge.main(["view", "--file", str(pipe)]) != 0
    assert "not a regular file" in capsys.readouterr().err


def test_session_scan_survives_a_file_replaced_by_a_fifo_after_it_was_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = tmp_path / "sessions" / "2026" / "10" / "30"
    sessions.mkdir(parents=True)
    pipe = fifo(sessions / "rollout-swapped.jsonl")
    regular = tmp_path / "regular.jsonl"
    regular.write_text(json.dumps({"type": "session_meta"}) + "\n", encoding="utf-8")
    real_stat = Path.stat

    def stale_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        # The check saw a regular file; the path became a FIFO before it was opened.
        return real_stat(regular) if self == pipe else real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", stale_stat)
    started = time.monotonic()
    with deadline():
        assert bridge_sessions.codex_session_records(tmp_path / "sessions", cwd=str(tmp_path), max_files=10) == []
    # The deadline's TimeoutError is an OSError that the scan would swallow, so time it as well.
    assert time.monotonic() - started < 4


def test_repository_checkpoint_does_not_block_on_a_tracked_file_swapped_for_a_fifo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *argv], check=True)
    target = repo / "tracked.txt"
    target.write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "x"], check=True)
    before = target.lstat()
    target.unlink()
    fifo(target)
    real_lstat = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda self: before if self == target else real_lstat(self))
    started = time.monotonic()
    with deadline():
        result = repository_checkpoint(repo)
    assert result["fingerprint"] is None
    assert time.monotonic() - started < 5
