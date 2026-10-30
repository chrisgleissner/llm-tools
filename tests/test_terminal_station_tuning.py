"""Station tmux tuning: provider-terminal behaviour should match a direct CLI launch."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_tools.terminal import (
    STATUS_CONTEXT_FORMAT,
    STATUS_REMAIN_ON_EXIT_FORMAT,
    TmuxTerminalBackend,
    station_status_formats,
)
from tests.conftest import wait_for as _shared_wait_for


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")

_SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


def _wait_for(predicate, timeout: float = 4.0) -> bool:
    return bool(_shared_wait_for(predicate, timeout, 0.05))


def _start(backend: TmuxTerminalBackend, tmp_path: Path, name: str, *, provider=None, size=(100, 30)):
    return backend.start(
        provider_argv=provider or _SLEEP,
        bridge_argv=_SLEEP,
        cwd=tmp_path,
        socket_path=tmp_path / f"{name}.sock",
        session_name=name,
        size=size,
    )


def _tmux(handle, *args: str) -> str:
    return subprocess.run(
        ["tmux", "-S", str(handle.socket_path), *args], check=True, text=True, capture_output=True,
    ).stdout.strip()


def test_station_config_sets_provider_terminal_options(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(backend, tmp_path, "tuning")
    try:
        assert _tmux(handle, "show-options", "-sv", "escape-time") == "10"
        assert _tmux(handle, "show-options", "-sv", "extended-keys") == "on"
        assert "xterm*:extkeys" in _tmux(handle, "show-options", "-sv", "terminal-features")
        assert _tmux(handle, "show-options", "-sv", "set-clipboard") == "external"
        assert _tmux(handle, "show-options", "-gv", "history-limit") == "50000"
        assert _tmux(handle, "show-options", "-gv", "window-size") == "latest"
        assert _tmux(handle, "show-options", "-gwv", "aggressive-resize") == "off"
        # Existing behaviour that must not regress.
        assert _tmux(handle, "show-options", "-gv", "prefix") == "C-]"
        assert _tmux(handle, "show-options", "-gv", "mouse") == "on"
        root = _tmux(handle, "list-keys", "-T", "root")
        assert "MouseDown1Pane" in root and "WheelUpPane" in root
        assert "send-prefix" in _tmux(handle, "list-keys", "-T", "prefix")
    finally:
        backend.stop(handle)


def test_tuning_options_are_quiet_so_unknown_options_cannot_abort_creation(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(backend, tmp_path, "quiet")
    try:
        config = handle.config_path.read_text(encoding="utf-8")
    finally:
        backend.stop(handle)
    tuned = ("escape-time", "extended-keys", "terminal-features", "set-clipboard",
             "history-limit", "window-size", "aggressive-resize", "remain-on-exit-format")
    for option in tuned:
        lines = [line for line in config.splitlines() if re.search(rf"\b{re.escape(option)}\b", line)
                 and line.startswith("set-option")]
        assert lines, option
        for line in lines:
            flags = line.split()[1]
            assert flags.startswith("-") and "q" in flags, line

    # tmux itself: -q silences an unknown option and the server still starts,
    # which is what happens on a tmux release without one of these options.
    socket = tmp_path / "probe.sock"
    conf = tmp_path / "probe.conf"
    conf.write_text("set-option -sq no-such-option 1\nset-option -gq no-such-session-option 1\n", encoding="utf-8")
    try:
        subprocess.run(["tmux", "-S", str(socket), "-f", str(conf), "new-session", "-d", "-s", "p", "sleep 5"],
                       check=True, capture_output=True)
        assert subprocess.run(["tmux", "-S", str(socket), "has-session", "-t", "p"]).returncode == 0
    finally:
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], check=False, capture_output=True)


def test_idle_placeholder_pane_is_blank_and_real_exit_shows_friendly_message(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(backend, tmp_path, "banner", provider=["/bin/sh", "-c", "exit 0"])
    try:
        assert _wait_for(lambda: _tmux(handle, "display-message", "-p", "-t", handle.provider_pane,
                                       "#{pane_dead}") == "1")
        placeholder = backend.capture(handle, pane="provider")
        assert "Pane is dead" not in placeholder
        assert placeholder.strip() == ""

        _tmux(handle, "respawn-pane", "-k", "-t", handle.provider_pane, "sh -c 'exit 3'")
        assert _wait_for(lambda: "Provider exited (status 3)" in backend.capture(handle, pane="provider"))
        message = backend.capture(handle, pane="provider")
        assert "Ctrl-] then B for Bridge controls" in message
        assert "Pane is dead" not in message
    finally:
        backend.stop(handle)
    assert "pane_dead_status" in STATUS_REMAIN_ON_EXIT_FORMAT


def test_status_formats_keep_explicit_hint_wording_and_separators() -> None:
    left, right = station_status_formats()
    words = {side: re.sub(r"#\[[^\]]*\]", "", text) for side, text in (("left", left), ("right", right))}
    assert "Bridge: Ctrl-] then B" in words["left"] and "│" in left
    assert "Provider: Ctrl-] then P" in words["right"] and "│" in right
    # Key names use the `key` role, the surrounding words `muted`.
    from llm_tools.bridge_theme import tmux_role_markup

    assert tmux_role_markup("key") + "Ctrl-] " in left and tmux_role_markup("muted") + "Bridge: " in left
    assert "@llm_bridge_context" in STATUS_CONTEXT_FORMAT and "client_width" in STATUS_CONTEXT_FORMAT


def _attached_status_row(backend, handle, tmp_path: Path, width: int, context: str | None = None, *, wait_for: str = "Provider") -> str:
    if context is not None:
        backend.set_status_context(handle, context)
    outer = tmp_path / f"outer{width}.sock"
    base = ["tmux", "-S", str(outer), "-f", "/dev/null"]
    subprocess.run([*base, "new-session", "-d", "-s", "o", "-x", str(width), "-y", "24",
                    f"env TERM=xterm-256color tmux -S {handle.socket_path} attach -t {handle.session_name}"],
                   check=True)
    try:
        def row() -> str:
            screen = subprocess.run([*base, "capture-pane", "-p", "-t", "o"],
                                    text=True, capture_output=True).stdout.rstrip("\n").split("\n")
            return screen[-1]
        assert _wait_for(lambda: wait_for in row(), timeout=8.0)
        return row()
    finally:
        subprocess.run([*base, "kill-server"], check=False, capture_output=True)


def test_status_row_separates_context_and_truncates_long_context_at_80_columns(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(backend, tmp_path, "layout", size=(120, 30))
    try:
        short = _attached_status_row(backend, handle, tmp_path, 80, "Task 1 RUNNING", wait_for="Provider: Ctrl-] then P")
        assert short.count("│") == 2
        assert "Bridge: Ctrl-] then B │" in short and "│ ▶ Provider: Ctrl-] then P" in short
        assert "Task 1 RUNNING" in short and len(short) <= 80

        long = _attached_status_row(backend, handle, tmp_path, 80,
                                    "Task 12345678 VERIFYING / Mission COMPLETED with a very long tail")
        assert len(long) <= 80
        # Too long for the full hints: they give way to the pane names, and the text is cut with an ellipsis.
        assert "…" in long and "Provider" in long and "Bridge" in long and "Ctrl-]" not in long and "<" not in long

        wide = _attached_status_row(backend, handle, tmp_path, 140, "Task 1 RUNNING / Mission RUNNING", wait_for="Provider: Ctrl-] then P")
        assert "Task 1 RUNNING / Mission RUNNING" in wide and "…" not in wide
    finally:
        backend.stop(handle)


def test_status_context_keeps_middle_dot_separator(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(backend, tmp_path, "dot")
    try:
        backend.set_status_context(handle, "Task 1 RUNNING · Mission #{x}")
        assert _tmux(handle, "show-options", "-gv", "@llm_bridge_context") == "Task 1 RUNNING · Mission x"
    finally:
        backend.stop(handle)


def test_modified_keys_reach_a_provider_that_requests_them(tmp_path: Path) -> None:
    out = tmp_path / "keys.txt"
    program = tmp_path / "bytes.py"
    program.write_text(
        "import os, sys, time, tty, select\n"
        "tty.setraw(0)\n"
        "sys.stdout.write('\\x1b[>4;2m'); sys.stdout.flush()\n"  # request modifyOtherKeys, like a modern TUI
        "log = open(sys.argv[1], 'a')\n"
        "open(sys.argv[1] + '.ready', 'w').close()\n"
        "end = time.time() + 20\n"
        "while time.time() < end:\n"
        "    if select.select([0], [], [], 0.1)[0]:\n"
        "        log.write(repr(os.read(0, 64)) + '\\n'); log.flush()\n",
        encoding="utf-8",
    )
    backend = TmuxTerminalBackend()
    handle = _start(backend, tmp_path, "keys", provider=[sys.executable, str(program), str(out)])
    try:
        assert _wait_for(lambda: Path(str(out) + ".ready").exists(), timeout=10.0)  # raw mode and key request are set
        _tmux(handle, "send-keys", "-t", handle.provider_pane, "S-Enter")
        assert _wait_for(lambda: out.exists() and "27;2;13~" in out.read_text())
        _tmux(handle, "send-keys", "-t", handle.provider_pane, "Escape")
        assert _wait_for(lambda: out.read_text().rstrip().endswith("b'\\x1b'"), timeout=1.0)
    finally:
        backend.stop(handle)


_TASK_SEGMENTS = [("Task 1 ", "info"), ("● RUNNING", "state_running")]


def test_status_tier_drops_hints_first_then_shortens_the_state_text() -> None:
    from llm_tools.terminal import status_tier

    one = "Task 1 RUNNING"  # the plain @llm_bridge_context: 14 cells, 16 with the state glyph
    assert [status_tier(width, one) for width in (140, 120, 80, 70, 69, 60, 41, 40, 36)] == [
        "full", "full", "full", "full", "hints-short", "hints-short", "hints-short", "compact", "compact",
    ]
    two = "Task 12 RUNNING · STOPPING"  # two state glyphs: 30 cells
    assert status_tier(84, two) == "full" and status_tier(83, two) == "hints-short"
    assert status_tier(55, two) == "hints-short" and status_tier(54, two) == "compact"
    assert status_tier(54, two.replace("·", "|")) == "compact"


def test_compact_status_segments_keep_task_number_and_state_glyph() -> None:
    from llm_tools.terminal import compact_status_segments

    assert compact_status_segments(_TASK_SEGMENTS) == [("T1 ", "info"), ("●", "state_running")]
    both = [("Task 12 ", "info"), ("● RUNNING", "state_running"), (" · ", "dim"), ("‖ STOPPING", "state_waiting")]
    assert compact_status_segments(both) == [("T12 ", "info"), ("●", "state_running")]
    assert compact_status_segments([("Task 3 ", "info"), ("o WAITING", "state_waiting")]) == [("T3 ", "info"), ("o", "state_waiting")]
    assert compact_status_segments([("Task 3 ", "info"), ("no output for 30s", "muted")]) == [("T3 ", "info")]
    assert compact_status_segments([("Task 3 ", "info"), ("idle 12m", "muted")]) == [("T3 ", "info"), ("idle 12m", "muted")]
    assert compact_status_segments([("No task", "muted"), (" · ", "dim"), ("● PAUSED", "state_paused")]) == [("No task", "muted")]
    assert compact_status_segments([]) == []


def test_status_formats_switch_hints_on_the_same_threshold_as_the_state_text() -> None:
    left, right = station_status_formats()
    from llm_tools.terminal import _STATUS_IS_FULL

    assert left.startswith("#{?" + _STATUS_IS_FULL + ",") and right.startswith("#{?" + _STATUS_IS_FULL + ",")
    assert STATUS_CONTEXT_FORMAT.startswith("#{?" + _STATUS_IS_FULL + ",")
    assert "@llm_bridge_context_compact" in STATUS_CONTEXT_FORMAT


@pytest.mark.parametrize("width", (60, 80, 120))
def test_status_row_never_shows_scroll_markers_or_cut_words(tmp_path: Path, width: int) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(backend, tmp_path, f"narrow{width}", size=(120, 30))
    try:
        backend.publish_status(handle, label="release", context="Task 1 RUNNING", chip=3, segments=_TASK_SEGMENTS)
        row = _attached_status_row(backend, handle, tmp_path, width, wait_for="Provider")
        assert "<" not in row and ">" not in row and len(row) <= width
        assert "Task 1 ● RUNNING" in row and "release" in row
        assert ("Ctrl-] then B" in row) == (width >= 70)  # the hints are the first thing to go
        two = [("Task 12 ", "info"), ("● RUNNING", "state_running"), (" · ", "dim"), ("‖ STOPPING", "state_waiting")]
        backend.publish_status(handle, label="release-mission", context="Task 12 RUNNING · STOPPING", chip=3, segments=two)
        row = _attached_status_row(backend, handle, tmp_path, width, wait_for="Provider")
        assert "<" not in row and ">" not in row and len(row) <= width
        assert "Task 12 ● RUNNING · ‖ STOPPING" in row
    finally:
        backend.stop(handle)


def test_status_row_shortens_the_state_text_when_even_short_hints_leave_no_room(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(backend, tmp_path, "tiny", size=(120, 30))
    try:
        two = [("Task 12 ", "info"), ("● RUNNING", "state_running"), (" · ", "dim"), ("‖ STOPPING", "state_waiting")]
        backend.publish_status(handle, label="release-mission", context="Task 12 RUNNING · STOPPING", chip=3, segments=two)
        row = _attached_status_row(backend, handle, tmp_path, 44, wait_for="Provider")
        assert "T12 ●" in row and "Task" not in row and "RUNNING" not in row
        assert "<" not in row and ">" not in row and len(row) <= 44
    finally:
        backend.stop(handle)
