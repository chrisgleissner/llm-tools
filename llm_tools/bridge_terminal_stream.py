"""Remote terminal sessions for the worker service (PROTO-009, SEC-006, WEB-008).

A session streams one mission station's provider pane to the coordinator over
its own WebSocket, separate from the control connection. Output comes from a
read-only tmux control-mode client (``%output`` notifications), so the station
is never resized or written by a viewer. Input is accepted only from the single
session that holds the mission's writer role, and only while no local tmux
client is attached; it is typed with ``send-keys -H`` (exact bytes, no shell).
See ``docs/distributed/protocol.md`` (Terminal sessions) for the frames.
"""

from __future__ import annotations

import base64
import json
import os
import select
import subprocess
import threading
import time
from typing import Any, Callable, Protocol

PAUSE_AFTER_SECONDS = 5  # tmux pauses a control client this far behind; the session then resyncs
MAX_INPUT = 64 * 1024
CLEAR_SCREEN = b"\x1b[H\x1b[2J"
WRITER_CHECK_SECONDS = 1.0


def decode_control_output(escaped: bytes) -> bytes:
    """Undo tmux control-mode escaping: ``\\ooo`` octal for control bytes and backslash."""
    out = bytearray()
    index = 0
    while index < len(escaped):
        byte = escaped[index]
        digits = escaped[index + 1:index + 4]
        if byte == 0x5C and len(digits) == 3 and all(0x30 <= value <= 0x37 for value in digits):
            out.append(int(escaped[index + 1:index + 4], 8) & 0xFF)
            index += 4
            continue
        out.append(byte)
        index += 1
    return bytes(out)


def parse_control_line(line: bytes, pane_id: str) -> bytes | None:
    """Output bytes for ``pane_id`` from one control-mode line, or ``None``."""
    pane = pane_id.encode("ascii")
    prefix = b"%output " + pane + b" "
    if line.startswith(prefix):
        return decode_control_output(line[len(prefix):].rstrip(b"\r\n"))
    # With pause-after, tmux reports output as ``%extended-output %pane age ... : data``.
    extended = b"%extended-output " + pane + b" "
    if line.startswith(extended) and b" : " in line:
        return decode_control_output(line.split(b" : ", 1)[1].rstrip(b"\r\n"))
    return None


class WriterRegistry:
    """At most one writer session per mission, process-wide (SEC-006)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._writers: dict[str, str] = {}

    def acquire(self, mission_id: str, session_id: str) -> bool:
        with self._lock:
            holder = self._writers.get(mission_id)
            if holder is None or holder == session_id:
                self._writers[mission_id] = session_id
                return True
            return False

    def release(self, mission_id: str, session_id: str) -> None:
        with self._lock:
            if self._writers.get(mission_id) == session_id:
                del self._writers[mission_id]

    def holder(self, mission_id: str) -> str | None:
        with self._lock:
            return self._writers.get(mission_id)


WRITERS = WriterRegistry()


class PaneSource(Protocol):
    """What a session needs from a station; ``TmuxPaneSource`` is the real one."""

    def snapshot(self) -> bytes: ...
    def size(self) -> tuple[int, int]: ...
    def local_clients(self) -> int: ...
    def send_input(self, data: bytes) -> None: ...
    def resize(self, columns: int, rows: int) -> None: ...
    def read_output(self, timeout: float) -> bytes | None: ...
    def take_resync(self) -> bool: ...
    def close(self) -> None: ...


class TmuxPaneSource:
    """The provider pane of a live mission station."""

    def __init__(self, mission_id: str) -> None:
        from .bridge import _load_handle
        from .terminal import TmuxTerminalBackend

        self.backend = TmuxTerminalBackend()
        self.handle = _load_handle(mission_id)
        self.resized = False
        self.process = subprocess.Popen(self.backend.control_client_argv(self.handle), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self._buffer = b""
        self._paused = False

    def snapshot(self) -> bytes:
        text = self.backend.capture(self.handle, pane="provider", ansi=True)
        return text.replace("\n", "\r\n").encode("utf-8", "replace")

    def size(self) -> tuple[int, int]:
        return self.backend.pane_size(self.handle, pane="provider")

    def local_clients(self) -> int:
        return len(self.backend.attached_clients(self.handle))

    def send_input(self, data: bytes) -> None:
        self.backend.send_bytes(self.handle, pane="provider", data=data)

    def resize(self, columns: int, rows: int) -> None:
        self.backend.resize_station(self.handle, columns=columns, rows=rows)
        self.resized = True

    def read_output(self, timeout: float) -> bytes | None:
        """Pane output that arrived within ``timeout``; ``b""`` when none; ``None`` when the client ended."""
        assert self.process.stdout is not None
        readable, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not readable:
            return b""
        chunk = os.read(self.process.stdout.fileno(), 65536)
        if not chunk:
            return None
        self._buffer += chunk
        lines = self._buffer.split(b"\n")
        self._buffer = lines.pop()
        out = bytearray()
        for line in lines:
            data = parse_control_line(line, self.handle.provider_pane)
            if data is not None:
                out += data
            elif line.startswith(b"%pause " + self.handle.provider_pane.encode("ascii")):
                self._paused = True
            elif line.startswith(b"%exit"):
                return None
        return bytes(out)

    def take_resync(self) -> bool:
        """Whether tmux paused this client for lagging; resumes it so the caller can resend the screen."""
        if not self._paused:
            return False
        self._paused = False
        assert self.process.stdin is not None
        self.process.stdin.write(f"refresh-client -A '{self.handle.provider_pane}:continue'\n".encode("ascii"))
        self.process.stdin.flush()
        return True

    def close(self) -> None:
        if self.resized:
            self.backend.restore_station_sizing(self.handle)
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()  # detaches the control client
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=2)


class Connection(Protocol):
    def send_text(self, text: str) -> None: ...
    def receive_text(self, timeout: float | None = None) -> str | None: ...
    def close(self, code: int = 1000, reason: str = "") -> None: ...


class TerminalSession:
    """One remote terminal session; ``run`` blocks until it ends."""

    def __init__(self, session_id: str, mission_id: str, requested_mode: str, source: PaneSource, connection: Connection,
                 *, writers: WriterRegistry = WRITERS, allow_write: bool = True, stop: threading.Event | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.session_id = session_id
        self.mission_id = mission_id
        self.source = source
        self.connection = connection
        self.writers = writers
        self.stop = stop or threading.Event()
        self.clock = clock
        self.wants_write = requested_mode == "write" and allow_write
        self.mode = "view"
        self.reason = "" if requested_mode == "view" else ("" if allow_write else "this worker accepts read-only remote control")
        self.size: tuple[int, int] = (0, 0)

    def _send(self, frame: dict[str, Any]) -> None:
        self.connection.send_text(json.dumps(frame, separators=(",", ":")))

    def _output(self, data: bytes) -> None:
        """Send output; sends block, so a slow peer slows reading and tmux pauses us (bounded)."""
        if data:
            self._send({"t": "o", "d": base64.b64encode(data).decode("ascii")})

    def _refresh_role(self) -> None:
        """Grant or drop the writer role and report mode or size changes."""
        mode, reason = "view", self.reason if not self.wants_write else ""
        if self.wants_write:
            if self.source.local_clients():
                self.writers.release(self.mission_id, self.session_id)
                reason = "a local terminal is attached to this station; viewing only"
            elif self.writers.acquire(self.mission_id, self.session_id):
                mode = "write"
            else:
                reason = "another session holds the writer role; viewing only"
        size = self.source.size()
        if (mode, reason, size) != (self.mode, self.reason, self.size):
            self.mode, self.reason, self.size = mode, reason, size
            self._send({"t": "s", "cols": size[0], "rows": size[1], "mode": mode, "reason": reason})

    def _handle_frame(self, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        if frame.get("t") == "i" and self.mode == "write" and isinstance(frame.get("d"), str):
            try:
                data = base64.b64decode(frame["d"], validate=True)
            except ValueError:
                return
            if 0 < len(data) <= MAX_INPUT:
                self.source.send_input(data)
        elif frame.get("t") == "r" and self.mode == "write":
            columns, rows = frame.get("cols"), frame.get("rows")
            if isinstance(columns, int) and isinstance(rows, int) and not isinstance(columns, bool) and 1 <= columns <= 1000 and 1 <= rows <= 1000:
                if not self.source.local_clients():
                    self.source.resize(columns, rows)

    def run(self) -> str:
        """Stream until the station, the peer or ``stop`` ends the session; return the reason."""
        reason = "closed"
        try:
            self._refresh_role()
            self._output(CLEAR_SCREEN + self.source.snapshot())
            last_check = self.clock()
            while not self.stop.is_set():
                data = self.source.read_output(0.05)
                if data is None:
                    reason = "station ended"
                    break
                if self.source.take_resync():
                    data = CLEAR_SCREEN + self.source.snapshot()
                self._output(data)
                text = self.connection.receive_text(timeout=0.01)
                if text is not None:
                    self._handle_frame(text)
                now = self.clock()
                if now - last_check >= WRITER_CHECK_SECONDS:
                    last_check = now
                    self._refresh_role()
        except Exception as exc:  # noqa: BLE001 - a session failure must never reach the worker loop
            reason = f"{type(exc).__name__}"
        finally:
            self.writers.release(self.mission_id, self.session_id)
            try:
                self._send({"t": "x", "reason": reason})
            except Exception:  # noqa: BLE001
                pass
            self.source.close()
            self.connection.close()
        return reason
