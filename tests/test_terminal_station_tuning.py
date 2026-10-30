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


def _attached_status_row(backend, handle, tmp_path: Path, width: int, context: str) -> str:
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
        assert _wait_for(lambda: "Provider: Ctrl-] then P" in row(), timeout=8.0)
        return row()
    finally:
        subprocess.run([*base, "kill-server"], check=False, capture_output=True)


def test_status_row_separates_context_and_truncates_long_context_at_80_columns(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(backend, tmp_path, "layout", size=(120, 30))
    try:
        short = _attached_status_row(backend, handle, tmp_path, 80, "Task 1 RUNNING")
        assert short.count("│") == 2
        assert "Bridge: Ctrl-] then B │" in short and "│ ▶ Provider: Ctrl-] then P" in short
        assert "Task 1 RUNNING" in short and len(short) <= 80

        long = _attached_status_row(backend, handle, tmp_path, 80,
                                    "Task 12345678 VERIFYING / Mission COMPLETED with a very long tail")
        assert len(long) <= 80
        assert "…" in long and "Provider: Ctrl-] then P" in long and "Bridge: Ctrl-] then B" in long

        wide = _attached_status_row(backend, handle, tmp_path, 140, "Task 1 RUNNING / Mission RUNNING")
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
