"""Bounded log sinks used only by managed LLM Bridge missions."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
from typing import Iterator


def _rotate(path: Path, rotations: int) -> None:
    for index in range(max(0, rotations), 0, -1):
        source = path.with_name(f"{path.name}.{index - 1}") if index > 1 else path
        target = path.with_name(f"{path.name}.{index}")
        try:
            if target.exists():
                target.unlink()
            if source.exists():
                os.replace(source, target)
        except OSError:
            pass


def _append_rotating(path: Path, data: bytes, *, max_bytes: int, rotations: int) -> None:
    if max_bytes <= 0:
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    remaining = memoryview(data)
    while remaining:
        try:
            current = path.stat().st_size if path.exists() else 0
        except OSError:
            current = 0
        if current >= max_bytes:
            if rotations > 0:
                _rotate(path, rotations)
                try:
                    if path.exists() and path.stat().st_size >= max_bytes:
                        path.unlink()
                except OSError:
                    pass
            else:
                try:
                    path.unlink()
                except OSError:
                    pass
            current = 0
        chunk = remaining[: max(1, max_bytes - current)]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            pending = chunk
            while pending:
                written = os.write(fd, pending)
                pending = pending[written:]
        finally:
            os.close(fd)
        remaining = remaining[len(chunk):]


def sink_stdin(path: str | Path, *, max_bytes: int, rotations: int) -> int:
    """Consume a tmux pipe-pane stream into capped, rotating private files."""
    target = Path(path)
    while True:
        data = os.read(0, 65536)
        if not data:
            return 0
        _append_rotating(target, data, max_bytes=max_bytes, rotations=rotations)


@contextmanager
def capture_process_output(path: str | Path, *, max_bytes: int, rotations: int) -> Iterator[None]:
    """Redirect this process and descendants through a bounded rotating sink."""
    target = Path(path)
    read_fd, write_fd = os.pipe()
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    finished = threading.Event()

    def consume() -> None:
        try:
            while True:
                data = os.read(read_fd, 65536)
                if not data:
                    return
                _append_rotating(target, data, max_bytes=max_bytes, rotations=rotations)
        finally:
            finished.set()
            os.close(read_fd)

    reader = threading.Thread(target=consume, name="llm-bridge-log-sink", daemon=True)
    reader.start()
    os.dup2(write_fd, 1)
    os.dup2(write_fd, 2)
    os.close(write_fd)
    try:
        yield
    finally:
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)
        reader.join(timeout=5)
        if not finished.is_set():
            raise TimeoutError("Bridge log sink did not drain its output pipe")


__all__ = ["capture_process_output", "sink_stdin"]
