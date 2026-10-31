"""Native terminal stations backed by an isolated tmux PTY server.

The provider owns its own real tmux pane and terminal. LLM Bridge never
emulates a VT screen or parses provider output to render it; attaching to the
private tmux server lets tmux forward terminal input and resize events to the
native process.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Literal

from .bridge_glyphs import glyph, localize
from .bridge_states import ENDED_MISSION_STATUSES
from .common import resolve_executable, tmux_literal, unix_socket_accepts
from .bridge_theme import mission_chip_style, resolve_cached, tmux_role_markup, tmux_style_markup


# Columns taken by the fixed status chrome: "▶ Bridge: Ctrl-] then B │ " on the
# left and " │ ▶ Provider: Ctrl-] then P" on the right, plus a little breathing
# room. The task/mission context gets the rest of the client width.
_STATUS_CHROME_COLUMNS = 54
# The same chrome without the key hints: "▶ Bridge │ " and " │ ▶ Provider ".
_STATUS_SHORT_CHROME_COLUMNS = 25
_STATUS_CONTEXT_MIN_COLUMNS = 8
# Cells reserved for the compact state text ("T1 ●") when the mission label is sized.
_STATUS_COMPACT_COLUMNS = 10
# The styled state text adds a state glyph and a space to the plain @llm_bridge_context,
# twice when a mission state follows the task state (" · " or " | " in ASCII mode).
_STATUS_GLYPH_COLUMNS = 2
_STATUS_TWO_STATES = ("\u00b7", "|")


def status_context_cells(context: str) -> int:
    """Width of the coloured state text for the plain `context`: its length plus the state glyphs."""
    two = any(f" {mark} " in context for mark in _STATUS_TWO_STATES)
    return len(context) + _STATUS_GLYPH_COLUMNS * (2 if two else 1)


def status_tier(client_width: int, context: str) -> str:
    """Which form the status row takes for a client of `client_width` cells and the plain state text `context`.

    * ``full``: the key hints on both sides and the whole state text;
    * ``hints-short``: the hints are dropped (``▶ Bridge │`` and ``│ ▶ Provider``), the state text stays whole;
    * ``compact``: the hints are dropped and the state text is shortened to ``T1 ●``.

    The tmux formats below apply the same thresholds, so the row never gets wider than the room
    tmux leaves between the two sides (when it would, tmux shows ``<`` and ``>`` and cuts mid-word).
    """
    need = status_context_cells(context)
    if client_width - _STATUS_CHROME_COLUMNS >= need:
        return "full"
    if client_width - _STATUS_SHORT_CHROME_COLUMNS >= need:
        return "hints-short"
    return "compact"


_STATUS_NEED = (
    "#{e|+:#{n:@llm_bridge_context},#{?#{||:#{m:* \u00b7 *,#{@llm_bridge_context}},#{m:* | *,#{@llm_bridge_context}}},"
    + str(2 * _STATUS_GLYPH_COLUMNS) + "," + str(_STATUS_GLYPH_COLUMNS) + "}}"
)
_STATUS_IS_FULL = "#{e|>=:#{e|-:#{client_width}," + str(_STATUS_CHROME_COLUMNS) + "}," + _STATUS_NEED + "}"
_STATUS_IS_SHORT = "#{e|>=:#{e|-:#{client_width}," + str(_STATUS_SHORT_CHROME_COLUMNS) + "}," + _STATUS_NEED + "}"


def _context_limit(chrome: int) -> str:
    return (
        "#{?#{e|>:#{e|-:#{client_width}," + str(chrome) + "}," + str(_STATUS_CONTEXT_MIN_COLUMNS) + "},"
        "#{e|-:#{client_width}," + str(chrome) + "}," + str(_STATUS_CONTEXT_MIN_COLUMNS) + "}"
    )


def _context_format(chrome: int, body: str, reserve: str) -> str:
    """The centre text: the mission label as a chip (it shrinks first and disappears below four cells), then `body`."""
    # The label gets what is left after the state text (`reserve` cells) and the chip's padding.
    label_limit = "#{e|-:#{e|-:" + _context_limit(chrome) + "," + reserve + "},3}"
    return (
        "#{?#{@llm_bridge_label},"
        "#{?#{e|>=:" + label_limit + ",4},#{@llm_bridge_chip} #{=/" + label_limit + "/…:@llm_bridge_label} #[default] ,},"
        "}#{=/" + _context_limit(chrome) + "/…:" + body + "}"
    )


# The label is drawn as a filled chip in the mission's identity colour
# (@llm_bridge_chip holds its tmux style). The state text is @llm_bridge_context,
# or its coloured form @llm_bridge_context_styled when the console published one;
# @llm_bridge_context_compact is the short coloured form ("T1 ●") for narrow clients.
_STATUS_FULL_BODY = "#{?#{@llm_bridge_context_styled},#{@llm_bridge_context_styled},#{@llm_bridge_context}}"
_STATUS_COMPACT_BODY = "#{?#{@llm_bridge_context_compact},#{@llm_bridge_context_compact}," + _STATUS_FULL_BODY + "}"
STATUS_CONTEXT_FORMAT = (
    "#{?" + _STATUS_IS_FULL + "," + _context_format(_STATUS_CHROME_COLUMNS, _STATUS_FULL_BODY, _STATUS_NEED) + ","
    "#{?" + _STATUS_IS_SHORT + "," + _context_format(_STATUS_SHORT_CHROME_COLUMNS, _STATUS_FULL_BODY, _STATUS_NEED) + ","
    + _context_format(_STATUS_SHORT_CHROME_COLUMNS, _STATUS_COMPACT_BODY, str(_STATUS_COMPACT_COLUMNS)) + "}}"
)
# Longest context kept in the option itself; the status row truncates further
# to the attached client's width.
_STATUS_CONTEXT_MAX_CHARS = 80
_STATUS_LABEL_MAX_CHARS = 20
_STATUS_CONTEXT_SAFE_RE = re.compile(r"[^\w .:/·…●○◆✓✕‖*=|-]", re.UNICODE)


# Terminal (tab/window) title: "Bridge · <mission label> · <state>". The initial
# context "Bridge ready ..." already starts with the word; any other gets it added.
STATION_TITLE_FORMAT = (
    "#{?#{m:Bridge ready*,#{@llm_bridge_context}},,Bridge · }"
    "#{?#{@llm_bridge_label},#{@llm_bridge_label} · ,}#{@llm_bridge_context}"
)

# Shortcut reminder shown by `Ctrl-] ?`.
STATION_HELP_MESSAGE = (
    "Bridge controls: press Ctrl-] (Control + ]) then B. Return to provider: Ctrl-] then P. "
    "Switch mission: Ctrl-] then M. Send Ctrl-] to provider: press Ctrl-] twice."
)


def tmux_version() -> tuple[int, int] | None:
    """Return the installed tmux ``(major, minor)``, or ``None`` when unknown."""
    try:
        result = subprocess.run([resolve_executable("tmux"), "-V"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    return (int(match.group(1)), int(match.group(2))) if match else None


# What was verified about a running station, kept for the life of this process
# so that each backend call does not start tmux again just to re-check it. An
# entry is only used while the socket is still the same file (device, inode and
# change time) and still accepts connections; a server that exited or was
# replaced fails one of the two and is verified again from scratch.
_SocketIdentity = tuple[int, int, int]
_CACHE_LOCK = threading.Lock()
_OWNED_SERVERS: dict[str, _SocketIdentity] = {}
_STATION_HANDLES: dict[tuple[str, str, int], tuple[_SocketIdentity, float, "TerminalHandle"]] = {}
_PUBLISHED_STATUS: dict[str, tuple[_SocketIdentity, dict[str, str]]] = {}
_HANDLE_CACHE_SECONDS = 60.0


def _socket_identity(path: Path) -> _SocketIdentity | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISSOCK(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_ctime_ns)


# Text shown at the bottom of an exited provider pane. The idle placeholder
# the controller parks in the provider pane before the first task starts (a
# shell running `exit 0`) exits immediately; tmux would otherwise print its
# "Pane is dead" banner there until the real provider replaces it, so the
# banner is left empty while the pane's start command is that placeholder.
# Marks the start command of the pane that shows a waiting message; the scheduler may replace it.
WAIT_MARKER = "llm-bridge-wait"

STATUS_REMAIN_ON_EXIT_FORMAT = (
    "#{?#{m:*exit 0*,#{pane_start_command}},,"
    "Provider exited (#{?#{!=:#{pane_dead_status},},status #{pane_dead_status},signal #{pane_dead_signal}}) · "
    "#{?#{&&:#{==:#{pane_dead_status},0},#{@llm_bridge_review_hint}},#{@llm_bridge_review_hint},Ctrl-] then B for Bridge controls}}"
)


_MISSION_SESSION_RE = re.compile(r"mission-([0-9a-f]{32})")


def detach_notice(session_name: str) -> str:
    """The line shown after detaching from a mission station, or ``""`` for any other session.

    ``Detached from mission f292f491 (name) · it keeps running · llm-bridge attach to return``;
    a finished mission says so instead of promising that it keeps running.
    """
    match = _MISSION_SESSION_RE.fullmatch(session_name)
    if match is None:
        return ""
    mission_id = match.group(1)
    name, status = "", ""
    try:
        from .bridge import open_store
        from .bridge_format import printable

        with open_store() as store:
            mission = store.get_mission(mission_id)
        name, status = " ".join(printable(mission.get("name") or "").split()), str(mission.get("status") or "")
    except Exception:  # the notice is a courtesy; never fail a detach over a missing name
        pass
    label = f"mission {mission_id[:8]}" + (f" ({name})" if name else "")
    if status in ENDED_MISSION_STATUSES:
        return f"Detached from {label} · the mission is {status.lower()}"
    return f"Detached from {label} · it keeps running · llm-bridge attach to return"


def station_status_formats() -> tuple[str, str]:
    """Return the tmux `(status-left, status-right)` formats for a station.

    The context between them is rendered by the window-status formats
    (`STATUS_CONTEXT_FORMAT`) and centred between the two hitboxes.
    """
    dim = tmux_role_markup("dim")
    muted = tmux_role_markup("muted")
    key = tmux_role_markup("key")
    play, bar = glyph("play"), glyph("vbar")
    # The hint reads "<verb> <key>": the key names are `key`, the words `muted`;
    # the pane that has focus gets the `accent` marker.
    accent = tmux_role_markup("accent")
    left_marker = f"{accent}#{{?#{{==:#{{pane_index}},1}},{play} ,}}"
    right_marker = f"{accent}#{{?#{{==:#{{pane_index}},0}},{play} ,}}"
    left = (
        f"{left_marker}{muted}Bridge: {key}Ctrl-] {muted}then {key}B "
        f"{dim}{bar} #[default]"
    )
    right = (
        f"{dim} {bar} {right_marker}"
        f"{muted}Provider: {key}Ctrl-] {muted}then {key}P #[default]"
    )
    # On a narrow client the hints go first: "▶ Bridge │" and "│ ▶ Provider" keep the
    # focus marker and the pane names, and leave the room to the task and state text.
    left_short = f"{left_marker}{muted}Bridge {dim}{bar} #[default]"
    right_short = f"{dim} {bar} {right_marker}{muted}Provider #[default]"
    return (
        "#{?" + _STATUS_IS_FULL + "," + left + "," + left_short + "}",
        "#{?" + _STATUS_IS_FULL + "," + right + "," + right_short + "}",
    )


def _clean_status_text(text: object, limit: int) -> str:
    return _STATUS_CONTEXT_SAFE_RE.sub("", " ".join(str(text).split()))[:limit]


def _styled_status_value(segments: list[tuple[str, str]]) -> str:
    """The state text with each (text, role) segment in its theme colour.

    Only the sanitized text of each segment is kept; the colour comes from the
    resolved theme, so mission-supplied text can never inject markup.
    """
    parts: list[str] = []
    for text, role in segments:
        safe = _clean_status_text(text, _STATUS_CONTEXT_MAX_CHARS)
        if not safe:
            continue
        if str(text)[:1].isspace():
            parts.append(" ")
        parts.append(f"{tmux_role_markup(role)}{safe}")
        if str(text)[-1:].isspace():
            parts.append(" ")
    value = "".join(parts)
    return value + "#[default]" if value else value


_TASK_SEGMENT_RE = re.compile(r"Task (\S+)\s*$")
_COMPACT_PHRASE_MAX_CELLS = 12


def compact_status_segments(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The state text for a narrow client: ``Task 1 ● RUNNING`` becomes ``T1 ●``.

    The task number stays, the state keeps its glyph and colour but loses the word, and
    everything after the task state (the mission state) is left out. A state that has no
    glyph (a silent provider's ``no output for 30s``) stays whole when it is short and is
    left out otherwise, so the text is never cut inside a word. Text without a task
    (``No task``) is returned unchanged.
    """
    first = segments[0] if segments else None
    match = _TASK_SEGMENT_RE.fullmatch(first[0]) if first else None
    if first is None or match is None:
        return list(segments[:1])
    out = [(f"T{match.group(1)} ", first[1])]
    if len(segments) > 1:
        text, role = segments[1]
        lead, _space, rest = text.partition(" ")
        if len(lead) == 1 and rest.isupper():  # "● RUNNING", or "o WAITING" in ASCII mode
            out.append((lead, role))
        elif len(text) <= _COMPACT_PHRASE_MAX_CELLS:
            out.append((text, role))
    return out


@dataclass(frozen=True)
class TerminalHandle:
    """Durable coordinates for one provider and one Bridge pane."""

    socket_path: Path
    config_path: Path
    session_name: str
    provider_pane: str
    bridge_pane: str
    bridge_rows: int


@dataclass(frozen=True)
class TerminalAction:
    """A requested terminal transition, safe to return from a UI worker."""

    mission_id: str
    client: str | None = None
    action: Literal["attach", "detach", "pick"] = "attach"


def _int_or_zero(text: str) -> int:
    return int(text) if text.isdigit() else 0


@dataclass(frozen=True)
class StationClient:
    """One terminal attached to a station, as tmux reports it."""

    name: str
    readonly: bool
    width: int
    height: int
    idle_seconds: int

    def describe(self) -> str:
        from .bridge_format import duration

        size = f"{self.width}x{self.height}" if self.width and self.height else "size unknown"
        flags = " · read-only, never detached" if self.readonly else ""
        idle = "active now" if self.idle_seconds < 5 else f"idle {duration(self.idle_seconds)}"
        return f"{self.name}  {size}  {idle}{flags}"


class DeadStationError(RuntimeError):
    """The station's socket had no listener; the dead artifacts were removed."""


class TmuxTerminalBackend:
    """Manage native terminal processes in a private, user-owned tmux server."""

    _COMMAND_TIMEOUT_SECONDS = 10
    _TEARDOWN_CONFIRM_TIMEOUT_SECONDS = 2.0
    _BRIDGE_CONFIG_MARKER = "set-option -g @llm_bridge_station 1\n"
    _SERVER_OWNED = "owned"
    _SERVER_FOREIGN = "foreign"
    _SERVER_ABSENT = "absent"
    _SERVER_UNKNOWN = "unknown"
    _PREFIX_RE = re.compile(
        r"(?:(?:C-|M-|S-)?(?:[A-Za-z0-9]|F(?:[1-9]|1[0-2])|"
        r"Space|Tab|Enter|Escape|[\[\]@_]))"
    )

    @staticmethod
    def _validate_socket_path(socket_path: Path, *, allow_missing: bool = False) -> None:
        """Reject sockets outside a private directory owned by this user."""
        parent = socket_path.parent
        try:
            parent_stat = parent.stat()
        except OSError as exc:
            raise PermissionError(f"private tmux socket directory is unavailable: {parent}") from exc
        if parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) & 0o077:
            raise PermissionError(f"tmux socket directory must be owned by this user and private: {parent}")
        try:
            socket_stat = socket_path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise FileNotFoundError(f"private tmux socket does not exist: {socket_path}")
        if (
            not stat.S_ISSOCK(socket_stat.st_mode)
            or socket_stat.st_uid != os.getuid()
            or stat.S_IMODE(socket_stat.st_mode) & 0o077
        ):
            raise PermissionError(f"tmux path is not a private socket owned by this user: {socket_path}")

    @staticmethod
    def _validate_config_path(socket_path: Path, config_path: Path) -> None:
        expected = socket_path.with_name(socket_path.name + ".conf")
        if config_path != expected:
            raise PermissionError(f"tmux configuration must be the private sibling of its socket: {expected}")
        try:
            info = config_path.lstat()
        except OSError as exc:
            raise PermissionError(f"private tmux configuration is unavailable: {config_path}") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise PermissionError(f"tmux configuration must be a private file owned by this user: {config_path}")

    @classmethod
    def _unlink_owned_socket(cls, socket_path: Path) -> None:
        try:
            cls._validate_socket_path(socket_path)
        except (FileNotFoundError, PermissionError):
            return
        try:
            socket_path.unlink()
        except OSError:
            pass

    @staticmethod
    def _unlink_owned_config(socket_path: Path, config_path: Path) -> None:
        try:
            TmuxTerminalBackend._validate_config_path(socket_path, config_path)
        except PermissionError:
            return
        try:
            config_path.unlink()
        except OSError:
            pass

    def _run(
        self,
        socket_path: Path,
        *args: str,
        config_path: Path | None = None,
        check: bool = True,
        capture: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self._validate_socket_path(socket_path, allow_missing=bool(args and args[0] == "new-session"))
        if config_path is not None and config_path != Path("/dev/null"):
            self._validate_config_path(socket_path, config_path)
        command = [resolve_executable("tmux"), "-S", str(socket_path), "-f", str(config_path or "/dev/null"), *args]
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            check=check,
            timeout=self._COMMAND_TIMEOUT_SECONDS,
            env=None if env is None else dict(env),
        )

    @staticmethod
    def _command(argv: list[str]) -> str:
        if not argv or any("\x00" in arg for arg in argv):
            raise ValueError("terminal command must contain a non-empty argv without NUL bytes")
        return "exec " + shlex.join(argv)

    def start(
        self,
        *,
        provider_argv: list[str],
        bridge_argv: list[str],
        cwd: str | Path,
        socket_path: str | Path,
        session_name: str,
        size: tuple[int, int] = (120, 40),
        bridge_rows: int = 6,
        prefix: str = "C-]",
        picker_argv: list[str] | None = None,
        server_env: Mapping[str, str] | None = None,
    ) -> TerminalHandle:
        """Start a provider and command pane on a private tmux server.

        `server_env` replaces the environment the tmux server starts with. Every
        pane inherits it, so a caller uses it to keep credentials out of panes.

        `size` is `(columns, rows)`. Existing socket paths are rejected so a
        stale or unrelated tmux server is never killed or commandeered.
        """
        socket = Path(socket_path).expanduser().absolute()
        workdir = Path(cwd).expanduser().absolute()
        cols, rows = size
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", session_name):
            raise ValueError("session_name may contain only letters, digits, dot, underscore, and hyphen")
        if not self._PREFIX_RE.fullmatch(prefix):
            raise ValueError("prefix must be one tmux key such as C-], C-a, or F1")
        if cols < 80 or rows < 24:
            raise ValueError("terminal size must be at least 80 columns by 24 rows")
        if bridge_rows < 5 or bridge_rows > 8 or bridge_rows >= rows - 8:
            raise ValueError("bridge_rows must be between 5 and 8 and leave at least 8 provider rows")
        if not workdir.is_dir():
            raise ValueError(f"working directory does not exist: {workdir}")
        socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._validate_socket_path(socket, allow_missing=True)
        # A socket with no listener (left behind by a killed server, or created
        # by a late `attach-session` that tmux turned into a server start) is a
        # dead artifact, not a collision. A live or unprovable path still is.
        try:
            self.reclaim_dead_station(socket)
        except (PermissionError, RuntimeError):
            pass
        if socket.exists():
            raise FileExistsError(f"private tmux socket already exists: {socket}")
        config_path = socket.with_name(socket.name + ".conf")
        if config_path.exists():
            raise FileExistsError(f"private tmux configuration already exists: {config_path}")
        provider_cmd = self._command(provider_argv)
        bridge_cmd = self._command(bridge_argv)
        bridge_status, provider_status = station_status_formats()
        config = (
            "set-option -g remain-on-exit on\n"
            f"set-option -gq remain-on-exit-format {shlex.quote(STATUS_REMAIN_ON_EXIT_FORMAT)}\n"
            "set-option -g mouse on\n"
            # Keep Esc responsive (providers use it to interrupt) while still
            # letting escape sequences split across an SSH packet boundary
            # parse as one key. Set explicitly: tmux < 3.5 defaults to 500 ms.
            "set-option -sq escape-time 10\n"
            # Pass Shift-Enter and other modified keys through to providers
            # that request them. `-q` keeps older tmux from aborting on
            # options it does not know.
            "set-option -sq extended-keys on\n"
            "set-option -asq terminal-features 'xterm*:extkeys'\n"
            # `external`: copies the user makes in tmux copy mode reach the
            # local clipboard (OSC 52), but an application running in a pane
            # cannot set the clipboard with its own OSC 52 sequence.
            "set-option -sq set-clipboard external\n"
            "set-option -gq history-limit 50000\n"
            # With several clients of different sizes attached, the most
            # recently active one decides the window size.
            "set-option -gq window-size latest\n"
            "set-option -gwq aggressive-resize off\n"
            f"set-option -g prefix {prefix}\n"
            "set-option -g focus-events on\n"
            # Show "Bridge · <mission> · <state>" in the terminal tab/window
            # title so several stations are distinguishable.
            "set-option -g set-titles on\n"
            f"set-option -g set-titles-string {shlex.quote(STATION_TITLE_FORMAT)}\n"
            "set-option -g @llm_bridge_station 1\n"
            'set-option -g default-terminal "screen-256color"\n'
            "set-option -g status on\n"
            "set-option -g status-style 'bg=default,fg=default'\n"
            "set-option -g status-left-length 32\n"
            "set-option -g status-right-length 32\n"
            "set-option -g @llm_bridge_context 'Bridge ready · no current task'\n"
            "set-option -g @llm_bridge_label ''\n"
            "set-option -g @llm_bridge_chip ''\n"
            "set-option -g @llm_bridge_context_styled ''\n"
            "set-option -g @llm_bridge_context_compact ''\n"
            "set-option -g status-justify centre\n"
            f"set-option -g window-status-format {shlex.quote(STATUS_CONTEXT_FORMAT)}\n"
            f"set-option -g window-status-current-format {shlex.quote(STATUS_CONTEXT_FORMAT)}\n"
            f"set-option -g status-left {shlex.quote(bridge_status)}\n"
            f"set-option -g status-right {shlex.quote(provider_status)}\n"
        )
        prompt_unbinds = "".join(
            f"unbind-key -T {table} {shlex.quote(key)}\n"
            for table, keys in self._PROMPT_KEY_UNBINDS
            for key in keys
        )
        config += (
            # tmux's default prefix table has 87 bindings, including `:`
            # (command prompt) and several that run shell commands. Input
            # pasted into a provider pane could reach them. Drop all of them
            # and bind only what the station documents.
            "unbind-key -a -T prefix\n"
            f"bind-key {prefix} send-prefix\n"
            "bind-key d detach-client\n"
            "bind-key z resize-pane -Z\n"
            "bind-key '[' copy-mode\n"
            f"{prompt_unbinds}"
            "bind-key -T root MouseDown1Pane select-pane -t = \\; send-keys -M \\; select-pane -t =\n"
            "bind-key -T root MouseUp1Pane select-pane -t = \\; send-keys -M\n"
            'bind-key -T root WheelUpPane select-pane -t = \\; if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" { send-keys -M } { copy-mode -e }\n'
            'bind-key -T root WheelDownPane select-pane -t = \\; if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" { send-keys -M } { send-keys -M }\n'
            f"bind-key -T prefix ? display-message {shlex.quote(STATION_HELP_MESSAGE)}\n"
        )
        # Bindings that need no pane ID belong to the config the server reads
        # at start; only those that name this launch's panes are sent afterwards.
        version = tmux_version()
        switcher_binds = self._switcher_binds("", workdir, picker_argv, version)
        config += "".join(self._config_line(args) for args in switcher_binds if "display-popup" in args)
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as config_file:
                config_file.write(config)
        except Exception:
            self._unlink_owned_config(socket, config_path)
            raise

        started = False
        try:
            started = True
            # One invocation creates the session and its command pane and
            # prints both pane IDs (provider first).
            created = self._run(
                socket,
                "new-session", "-d", "-P", "-F", "#{pane_id}",
                "-s", session_name, "-n", "station",
                "-x", str(cols), "-y", str(rows),
                "-c", tmux_literal(str(workdir)),
                provider_cmd,
                ";",
                "split-window", "-v", "-l", str(bridge_rows), "-P", "-F", "#{pane_id}",
                "-t", f"{session_name}:station.0",
                "-c", tmux_literal(str(workdir)),
                bridge_cmd,
                config_path=config_path,
                env=server_env,
                capture=True,
            )
            self._validate_socket_path(socket)
            ids = created.stdout.split()
            if len(ids) != 2 or not all(re.fullmatch(r"%\d+", pane_id) for pane_id in ids):
                raise RuntimeError("tmux did not report the provider and command pane IDs")
            pane, bridge_pane = ids
            # tmux's proportional layout resize can collapse the command pane
            # when an attached client shrinks. The hook below keeps the reserved
            # control area at its configured height and lets the native pane
            # absorb the remaining rows. Resizing a zoomed window unzooms it,
            # so a zoomed window is zoomed again on the pane that was active.
            resize_hook = self._client_resized_hook(socket, config_path, f"{session_name}:station", bridge_pane, bridge_rows, version)
            # Use pane IDs captured from this exact launch. Prefix shortcuts
            # are in tmux's prefix table, so native providers receive all keys
            # until the operator deliberately sends Ctrl-]. Status-line clicks
            # remain available even while the provider pane is zoomed.
            commands: list[list[str]] = [["set-hook", "-g", "client-resized", resize_hook]]
            for key, target in (("B", bridge_pane), ("b", bridge_pane), ("P", pane), ("p", pane)):
                commands.append(["bind-key", "-T", "prefix", key, "select-pane", "-t", target, "\\;", "resize-pane", "-Z", "-t", target])
            commands.extend(args for args in self._switcher_binds(bridge_pane, workdir, picker_argv, version) if "display-popup" not in args)
            for mouse_key, target in (
                # Handle a status click on release. Unzooming on mouse-down
                # changes the row under the pointer before tmux classifies the
                # matching release, so it can be mistaken for a pane click
                # and steal focus from the requested status target.
                ("MouseUp1StatusLeft", bridge_pane),
                ("MouseUp1StatusRight", pane),
                # tmux versions and terminals differ in how they classify the
                # right-hand status click. The generic status binding keeps
                # the provider target reliable outside the explicit left hitbox.
                ("MouseUp1Status", pane),
            ):
                commands.append(["bind-key", "-T", "root", mouse_key, "select-pane", "-t", target, "\\;", "resize-pane", "-Z", "-t", target])
            commands.append(["select-pane", "-t", pane])
            commands.append(["resize-pane", "-Z", "-t", pane])
            self._run(socket, *self._chain(commands))
            identity = _socket_identity(socket)
            if identity is not None:  # this process just created the server and set its marker
                with _CACHE_LOCK:
                    _OWNED_SERVERS[str(socket)] = identity
            return TerminalHandle(socket, config_path, session_name, pane, bridge_pane, bridge_rows)
        except Exception:
            if started:
                try:
                    self.stop(TerminalHandle(socket, config_path, session_name, "", "", bridge_rows))
                except (FileNotFoundError, PermissionError, RuntimeError, subprocess.SubprocessError):
                    pass
            else:
                self._unlink_owned_config(socket, config_path)
            raise

    # Default bindings (tmux 3.6) that open a command prompt or a menu. Typed or
    # pasted text reaching a prompt is parsed as tmux input, and a menu offers
    # pane and window commands. They are removed in the configuration file, so
    # starting a station costs no extra tmux process. Copy mode itself
    # (selection, scrolling, copy) keeps working. A key that a tmux version
    # does not bind is ignored, and a test lists the live tables so a version
    # that adds another prompt key fails it.
    _PROMPT_KEY_UNBINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("root", ("MouseDown3Pane", "MouseDown3Status", "MouseDown3StatusLeft",
                  "M-MouseDown3Pane", "M-MouseDown3Status", "M-MouseDown3StatusLeft")),
        ("copy-mode", ("F", "T", "f", "g", "t", "C-r", "C-s", *(f"M-{n}" for n in range(1, 10)))),
        ("copy-mode-vi", ("/", "?", ":", "F", "T", "f", "t", *(str(n) for n in range(1, 10)))),
    )

    @staticmethod
    def _client_resized_hook(
        socket: Path,
        config_path: Path,
        window: str,
        bridge_pane: str,
        bridge_rows: int,
        version: tuple[int, int] | None,
    ) -> str:
        """The ``client-resized`` hook command that restores the command pane's height.

        tmux 3.2 and newer run it as a delayed tmux command (``run-shell -C -d``),
        which starts no process. The short delay lets tmux finish its own
        layout of the resized window first; resizing at the instant of the
        event would be undone by that layout. Older tmux gets a shell job.
        """
        keep_height = f"resize-pane -t {bridge_pane} -y {bridge_rows}"
        if version is not None and version >= (3, 2):
            # ``##`` survives run-shell's own format expansion so that if-shell evaluates the flag.
            # Nothing is touched while the command pane already has its height: resizing a zoomed
            # window unzooms it, which sends the provider a resize it would have to repaint for.
            inner = (
                f"if-shell -F -t {bridge_pane} '##{{!=:##{{pane_height}},{bridge_rows}}}' "
                f"\"if-shell -F -t {window} '##{{window_zoomed_flag}}' "
                f"'{keep_height} ; resize-pane -Z -t {window}' '{keep_height}'\""
            )
            return f"run-shell -b -C -d 0.05 {shlex.quote(inner)}"
        tmux_command = shlex.join([resolve_executable("tmux"), "-S", tmux_literal(str(socket)), "-f", tmux_literal(str(config_path))])
        target_window = shlex.quote(window)
        script = (
            "sleep 0.05; "
            f"active_pane=$({tmux_command} display-message -p -t {target_window} '#{{pane_id}}'); "
            f"was_zoomed=$({tmux_command} display-message -p -t {target_window} '#{{window_zoomed_flag}}'); "
            f"height=$({tmux_command} display-message -p -t {shlex.quote(bridge_pane)} '#{{pane_height}}'); "
            f"[ \"$height\" = {bridge_rows} ] && exit 0; "
            f"{tmux_command} resize-pane -t {shlex.quote(bridge_pane)} -y {bridge_rows}; "
            f"if [ \"$was_zoomed\" = 1 ] && [ -n \"$active_pane\" ]; then "
            f"{tmux_command} resize-pane -Z -t \"$active_pane\"; fi"
        )
        return f"run-shell -b {shlex.quote(script)}"

    @staticmethod
    def _chain(commands: list[list[str]]) -> list[str]:
        """One tmux argument list that runs ``commands`` in order (separated by ``;``)."""
        chained: list[str] = []
        for command in commands:
            if chained:
                chained.append(";")
            chained.extend(command)
        return chained

    @staticmethod
    def _config_line(args: list[str]) -> str:
        """One tmux configuration line for a command; ``\\;`` words stay as the in-binding separator."""
        return " ".join(arg if arg == "\\;" else shlex.quote(arg) for arg in args) + "\n"

    @staticmethod
    def _switcher_binds(
        bridge_pane: str,
        workdir: Path,
        picker_argv: list[str] | None,
        version: tuple[int, int] | None = None,
    ) -> list[list[str]]:
        """The ``bind-key`` argument lists for ``prefix m`` and ``prefix M``.

        With tmux 3.2 or newer and a picker command the picker opens in a
        full-size popup, which needs no pane ID. Otherwise the Bridge pane is
        expanded and ``/missions`` is typed into its console.
        """
        version = version or tmux_version()
        binds: list[list[str]] = []
        for key in ("m", "M"):
            if picker_argv and version is not None and version >= (3, 2):
                popup = ["display-popup", "-E", "-w", "100%", "-h", "100%", "-d", tmux_literal(str(workdir))]
                if version >= (3, 3):
                    popup.insert(2, "-B")
                binds.append(["bind-key", "-T", "prefix", key, *popup, shlex.join(picker_argv)])
            else:
                binds.append([
                    "bind-key", "-T", "prefix", key,
                    "select-pane", "-t", bridge_pane, "\\;",
                    "resize-pane", "-Z", "-t", bridge_pane, "\\;",
                    "send-keys", "-t", bridge_pane, "C-u", "\\;",
                    "send-keys", "-l", "-t", bridge_pane, "/missions", "\\;",
                    "send-keys", "-t", bridge_pane, "Enter",
                ])
        return binds

    def _bind_mission_switcher(
        self,
        socket: Path,
        bridge_pane: str,
        workdir: Path,
        picker_argv: list[str] | None,
    ) -> None:
        """Bind ``prefix m`` to the mission picker (see ``_switcher_binds``)."""
        for args in self._switcher_binds(bridge_pane, workdir, picker_argv):
            self._run(socket, *args)

    @staticmethod
    def _target(handle: TerminalHandle, pane: str) -> str:
        if pane == "provider":
            return handle.provider_pane
        if pane == "bridge":
            return handle.bridge_pane
        raise ValueError("pane must be 'provider' or 'bridge'")

    def capture(
        self,
        handle: TerminalHandle,
        *,
        pane: str,
        alternate_screen: bool = False,
        ansi: bool = False,
    ) -> str:
        self._require_owned(handle)
        args = ["capture-pane", "-p", "-J"]
        if alternate_screen:
            args.append("-a")
        if ansi:
            args.append("-e")
        result = self._run(handle.socket_path, *args, "-t", self._target(handle, pane), check=False, capture=True)
        return result.stdout

    def send_keys(self, handle: TerminalHandle, *, pane: str, keys: str) -> None:
        self._require_owned(handle)
        if keys in {"Enter", "C-c", "C-d", "C-u", "Escape", "Tab", "BSpace", "Space"}:
            self._run(handle.socket_path, "send-keys", "-t", self._target(handle, pane), keys)
        else:
            self._run(handle.socket_path, "send-keys", "-l", "-t", self._target(handle, pane), keys)

    def focus(self, handle: TerminalHandle, *, pane: str, expand: bool = False) -> None:
        """Select an owned pane; optionally give its existing UI the full window.

        The layout changes go to tmux as one command list, so an attached client is drawn once,
        and nothing is sent when the pane is already in the requested state: unzooming and zooming
        again would resize the provider twice and repaint a two-pane screen in between.
        """
        self._require_owned(handle)
        target = self._target(handle, pane)
        state = self._run(handle.socket_path, "display-message", "-p", "-t", target,
                          "#{window_zoomed_flag} #{pane_active}", capture=True).stdout.split()
        zoomed, active = state[:1] == ["1"], state[1:] == ["1"]
        commands: list[list[str]] = []
        if expand and zoomed and active:
            pass  # already the full-window pane
        elif expand:
            if zoomed:
                commands.append(["resize-pane", "-Z", "-t", target])
            commands += [["select-pane", "-t", target], ["resize-pane", "-Z", "-t", target]]
        else:
            if zoomed:
                commands.append(["resize-pane", "-Z", "-t", target])
            commands.append(["select-pane", "-t", target])
        left, right = station_status_formats()
        commands += [["set-option", "-g", "status-left", left], ["set-option", "-g", "status-right", right]]
        self._run(handle.socket_path, *self._chain(commands))

    def set_status_context(self, handle: TerminalHandle, context: str) -> None:
        """Set the short, sanitized task/state label in the owned status row."""
        self._require_owned(handle)
        safe = _STATUS_CONTEXT_SAFE_RE.sub("", " ".join(str(context).split()))[:_STATUS_CONTEXT_MAX_CHARS]
        self._run(handle.socket_path, "set-option", "-g", "@llm_bridge_context", safe or "No current task")

    def set_review_hint(self, handle: TerminalHandle, text: str) -> bool:
        """Set the text the exited-provider footer shows after a clean (status 0) exit; empty restores the default.

        tmux expands the footer once, when the provider process dies, so call
        this before launching the provider. The pane keeps the provider's
        output; only the footer line changes. Returns whether the option was written.
        """
        self._require_owned(handle)
        clean = "".join(ch for ch in " ".join(str(text).split()) if ch.isprintable())[:200].replace("#", "##")
        if clean:
            result = self._run(handle.socket_path, "set-option", "-g", "@llm_bridge_review_hint", clean, check=False)
        else:
            result = self._run(handle.socket_path, "set-option", "-gu", "@llm_bridge_review_hint", check=False)
        return result.returncode == 0

    def set_status_chip(self, handle: TerminalHandle, which: int | str) -> None:
        """Colour the mission label as a filled chip in the mission's identity hue (1-8 or `mission_N`)."""
        self._require_owned(handle)
        resolved = resolve_cached()
        self._run(handle.socket_path, "set-option", "-g", "@llm_bridge_chip",
                  tmux_style_markup(mission_chip_style(which, resolved)))

    def set_status_styled(self, handle: TerminalHandle, segments: list[tuple[str, str]]) -> None:
        """Publish the state text with each (text, role) segment in its theme colour.

        Only the sanitized text of each segment is kept; the colour comes from
        the resolved theme, so mission-supplied text can never inject markup.
        """
        self._require_owned(handle)
        self._run(handle.socket_path, "set-option", "-g", "@llm_bridge_context_compact", _styled_status_value(compact_status_segments(segments)))
        self._run(handle.socket_path, "set-option", "-g", "@llm_bridge_context_styled", _styled_status_value(segments))

    def publish_status(
        self,
        handle: TerminalHandle,
        *,
        label: str,
        context: str,
        chip: int | str,
        segments: list[tuple[str, str]],
    ) -> bool:
        """Publish the mission label, state text, chip colour and coloured state text together.

        Only options whose value differs from what this process last wrote to
        the same station are sent, and they go to tmux in one invocation, so an
        unchanged status costs no process and a changed one costs one. Every
        write repaints every attached client, which is why unchanged values are
        skipped. Returns whether anything was written.
        """
        self._require_owned(handle)
        values = {
            "@llm_bridge_label": _clean_status_text(label, _STATUS_LABEL_MAX_CHARS),
            "@llm_bridge_context": _clean_status_text(context, _STATUS_CONTEXT_MAX_CHARS) or "No current task",
            "@llm_bridge_chip": tmux_style_markup(mission_chip_style(chip, resolve_cached())),
            "@llm_bridge_context_styled": _styled_status_value(segments),
            "@llm_bridge_context_compact": _styled_status_value(compact_status_segments(segments)),
        }
        key = str(handle.socket_path)
        identity = _socket_identity(handle.socket_path)
        with _CACHE_LOCK:
            entry = _PUBLISHED_STATUS.get(key)
            last = entry[1] if entry is not None and identity is not None and entry[0] == identity else {}
        changed = {name: value for name, value in values.items() if last.get(name) != value}
        if not changed:
            return False
        self._run(handle.socket_path, *self._chain([["set-option", "-g", name, value] for name, value in changed.items()]))
        if identity is not None:
            with _CACHE_LOCK:
                _PUBLISHED_STATUS[key] = (identity, {**last, **changed})
        return True

    def set_status_label(self, handle: TerminalHandle, label: str) -> None:
        """Set the short mission label that leads the status row and terminal title."""
        self._require_owned(handle)
        safe = _STATUS_CONTEXT_SAFE_RE.sub("", " ".join(str(label).split()))[:_STATUS_LABEL_MAX_CHARS]
        self._run(handle.socket_path, "set-option", "-g", "@llm_bridge_label", safe)

    def writable_clients(self, handle: TerminalHandle) -> tuple[str, ...]:
        """List attached, writable, non-control clients on an owned station."""
        self._require_owned(handle)
        result = self._run(
            handle.socket_path,
            "list-clients",
            "-F",
            "#{client_name}\t#{client_readonly}\t#{client_control_mode}",
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            raise RuntimeError("could not list clients on the owned Bridge station")
        clients: list[str] = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 3:
                continue
            client_name, readonly, control_mode = fields
            # tmux 3.6 does not expose client_id as a format in list-clients;
            # client_name is the exact client selector (normally /dev/pts/N).
            if re.fullmatch(r"/dev/[A-Za-z0-9._/-]{1,120}", client_name) and readonly == "0" and control_mode == "0":
                clients.append(client_name)
        return tuple(clients)

    def station_clients(self, handle: TerminalHandle, *, now: float | None = None) -> tuple[StationClient, ...]:
        """Every attached terminal (not control-mode) with size, idle time and read-only flag."""
        self._require_owned(handle)
        result = self._run(
            handle.socket_path, "list-clients", "-F",
            "#{client_name}\t#{client_readonly}\t#{client_control_mode}\t#{client_width}\t#{client_height}\t#{client_activity}",
            check=False, capture=True,
        )
        if result.returncode != 0:
            raise RuntimeError("could not list clients on the owned Bridge station")
        moment = time.time() if now is None else now
        clients: list[StationClient] = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 6 or fields[2] != "0" or not re.fullmatch(r"/dev/[A-Za-z0-9._/-]{1,120}", fields[0]):
                continue
            activity = _int_or_zero(fields[5])
            clients.append(StationClient(fields[0], fields[1] != "0", _int_or_zero(fields[3]), _int_or_zero(fields[4]), max(0, int(moment - activity)) if activity else 0))
        return tuple(clients)

    def active_client(self, handle: TerminalHandle) -> str | None:
        """Return the writable client with the most recent input, or ``None``."""
        self._require_owned(handle)
        result = self._run(
            handle.socket_path, "list-clients", "-F",
            "#{client_activity}\t#{client_name}\t#{client_readonly}\t#{client_control_mode}",
            check=False, capture=True,
        )
        best: tuple[int, str] | None = None
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 4 or fields[2] != "0" or fields[3] != "0":
                continue
            if not re.fullmatch(r"/dev/[A-Za-z0-9._/-]{1,120}", fields[1]) or not fields[0].isdigit():
                continue
            if best is None or int(fields[0]) >= best[0]:
                best = (int(fields[0]), fields[1])
        return best[1] if best else None

    def attached_clients(self, handle: TerminalHandle, *, exclude: tuple[str, ...] = ()) -> tuple[str, ...]:
        """List every attached non-control client (writable or read-only) on an owned station."""
        self._require_owned(handle)
        result = self._run(
            handle.socket_path,
            "list-clients",
            "-F",
            "#{client_name}\t#{client_control_mode}",
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            raise RuntimeError("could not list clients on the owned Bridge station")
        clients: list[str] = []
        for line in result.stdout.splitlines():
            name, _, control_mode = line.partition("\t")
            if name and control_mode == "0" and name not in exclude:
                clients.append(name)
        return tuple(clients)

    def set_detach_command(self, handle: TerminalHandle, argv: list[str]) -> None:
        """Run ``argv`` in the background whenever a client detaches from this station.

        The detaching client's name replaces a literal ``{client}`` argument so
        the command can ignore it even if tmux still lists it.
        """
        self._require_owned(handle)
        words = [("#{hook_client}" if word == "{client}" else tmux_literal(word)) for word in argv]
        command = " ".join(word if word == "#{hook_client}" else shlex.quote(word) for word in words)
        # tmux shows any run-shell output in view mode in the active pane, which
        # traps the user's keystrokes; the hook must stay silent.
        command += " >/dev/null 2>&1"
        self._run(handle.socket_path, "set-hook", "-g", "client-detached", f"run-shell -b {shlex.quote(command)}")

    def show_provider_message(self, handle: TerminalHandle, text: str) -> bool:
        """Show a short message in an idle provider pane; never replace real provider output.

        The pane runs a harmless placeholder that prints ``text`` and sleeps. It
        carries ``WAIT_MARKER`` in its start command, which the scheduler
        recognises and replaces when it launches the next provider. A pane
        with a live provider, or one that kept a previous provider's output,
        is left alone. Returns whether the message was written.
        """
        self._require_owned(handle)
        result = self._run(
            handle.socket_path,
            "display-message", "-p", "-t", handle.provider_pane,
            "#{pane_dead}\t#{pane_start_command}",
            check=False, capture=True,
        )
        dead, _, start_command = result.stdout.rstrip("\n").partition("\t")
        blank = dead == "1" and "exit 0" in start_command
        if result.returncode != 0 or not (blank or (dead == "0" and WAIT_MARKER in start_command)):
            return False
        clean = " ".join("".join(ch for ch in text if ch.isprintable()).split())
        self._run(
            handle.socket_path, "respawn-pane", "-k", "-t", handle.provider_pane,
            f"printf '%s\\n' {shlex.quote(clean)}; exec sleep 2147483647 # {WAIT_MARKER}",
        )
        return True

    def pane_runs_provider(self, handle: TerminalHandle) -> bool:
        """Whether the provider pane runs a real process, not Bridge's waiting-message placeholder."""
        self._require_owned(handle)
        result = self._run(
            handle.socket_path, "display-message", "-p", "-t", handle.provider_pane,
            "#{pane_dead}\t#{pane_start_command}", check=False, capture=True,
        )
        dead, _, start_command = result.stdout.rstrip("\n").partition("\t")
        return result.returncode == 0 and dead == "0" and WAIT_MARKER not in start_command

    def select_writable_client(self, handle: TerminalHandle, client_name: str | None = None) -> str:
        """Select an explicit client or the only connected writable client."""
        clients = self.writable_clients(handle)
        if client_name is not None:
            if client_name not in clients:
                raise PermissionError("selected tmux client is not a writable client of the owned Bridge station")
            return client_name
        if len(clients) == 1:
            return clients[0]
        if not clients:
            raise RuntimeError("no writable client is attached to the owned Bridge station")
        try:
            listing = "\n".join(f"  {client.describe()}" for client in self.station_clients(handle))
        except RuntimeError:
            listing = "\n".join(f"  {name}" for name in clients)
        raise RuntimeError(f"multiple writable clients are attached; choose one with --client TTY:\n{listing}")

    def detach_client(self, handle: TerminalHandle, *, client_name: str | None = None) -> str:
        """Detach one exact writable client without disturbing observers; return its name."""
        self._require_owned(handle)
        selected = self.select_writable_client(handle, client_name)
        self._run(handle.socket_path, "detach-client", "-t", selected)
        return selected

    def switch_client_to(
        self,
        source: TerminalHandle,
        destination: TerminalHandle,
        *,
        client_name: str | None = None,
    ) -> None:
        """Move one verified source client into another station.

        Only an explicitly named writable client is detached. Stations run on
        separate tmux servers, so ``switch-client`` cannot cross between them;
        the `-E` command attaches the same terminal to the destination
        instead. Detaching from the destination ends that command and returns
        to where the user started (their shell or the home screen). The
        source station keeps running. Only when the destination cannot be
        attached does the terminal go back to the source station.
        """
        self._require_owned(source)
        self._require_owned(destination)
        selected_client = self.select_writable_client(source, client_name)
        destination_attach = self._attach_if_present(destination)
        source_attach = self._attach_if_present(source)
        return_message = "llm-bridge: destination session unavailable; returning to the source station"
        shell_script = f"{destination_attach} || {{ printf '%s\\n' {shlex.quote(return_message)} >&2; {source_attach}; }}"
        shell_command = "exec env -u TMUX -u TMUX_PANE sh -c " + shlex.quote(shell_script)
        self._run(
            source.socket_path,
            "detach-client",
            "-t",
            selected_client,
            "-E",
            shell_command,
        )

    @staticmethod
    def _attach_if_present(handle: TerminalHandle) -> str:
        """Shell command that attaches only to a session that currently exists.

        `tmux attach-session` is one of the two tmux commands that start a
        server when the socket has no listener. Attaching to a station that was
        just stopped would therefore create a fresh, immediately exiting server
        and leave a dead socket behind. `has-session` never starts a server.
        """
        base = ["env", "-u", "TMUX", "-u", "TMUX_PANE", "tmux", "-S", str(handle.socket_path), "-f", str(handle.config_path)]
        target = f"-t={handle.session_name}"
        probe = shlex.join([*base, "has-session", target])
        attach = shlex.join([*base, "attach-session", target])
        return f"{probe} >/dev/null 2>&1 && {attach}"

    def resize(self, handle: TerminalHandle, *, cols: int, rows: int) -> None:
        self._require_owned(handle)
        if cols < 80 or rows < 24:
            raise ValueError("terminal size must be at least 80 columns by 24 rows")
        active_pane = self._run(handle.socket_path, "display-message", "-p", "-t", handle.session_name,
                                "#{pane_id}", capture=True).stdout.strip()
        was_zoomed = self._run(handle.socket_path, "display-message", "-p", "-t", handle.session_name,
                               "#{window_zoomed_flag}", capture=True).stdout.strip() == "1"
        self._run(handle.socket_path, "resize-window", "-t", handle.session_name, "-x", str(cols), "-y", str(rows))
        self._run(handle.socket_path, "resize-pane", "-t", handle.bridge_pane, "-y", str(handle.bridge_rows))
        if was_zoomed and active_pane:
            self._run(handle.socket_path, "resize-pane", "-Z", "-t", active_pane)

    def server_exists(self, handle: TerminalHandle) -> bool:
        """Return whether the private tmux server responds on its socket."""
        state = self._probe_server(handle.socket_path)
        if state == self._SERVER_UNKNOWN:
            raise RuntimeError(f"cannot determine whether tmux server is alive: {handle.socket_path}")
        return state in {self._SERVER_OWNED, self._SERVER_FOREIGN}

    def server_owned(self, socket_path: str | Path) -> bool:
        """Return whether a responsive tmux server identifies as an LLM Bridge station."""
        socket = Path(socket_path).expanduser().absolute()
        if self._verified_owned(socket):
            return True
        state = self._probe_server(socket)
        if state == self._SERVER_UNKNOWN:
            raise RuntimeError(f"cannot determine tmux server ownership: {socket}")
        if state == self._SERVER_OWNED:
            identity = _socket_identity(socket)
            if identity is not None:
                with _CACHE_LOCK:
                    _OWNED_SERVERS[str(socket)] = identity
        return state == self._SERVER_OWNED

    @staticmethod
    def _verified_owned(socket: Path) -> bool:
        """Whether this socket was verified as a Bridge station and is still that server.

        The check is a ``lstat`` and a connection attempt; no tmux process is
        started. A different socket file (replaced server) or a refused
        connection (server gone) discards the remembered verification.
        """
        key = str(socket)
        with _CACHE_LOCK:
            remembered = _OWNED_SERVERS.get(key)
        if remembered is None:
            return False
        if _socket_identity(socket) == remembered and unix_socket_accepts(socket):
            return True
        with _CACHE_LOCK:
            if _OWNED_SERVERS.get(key) == remembered:
                del _OWNED_SERVERS[key]
        return False

    @staticmethod
    def _socket_listener_state(socket_path: Path) -> str:
        """Distinguish an absent listener from a live or unqueryable socket."""
        try:
            info = socket_path.lstat()
        except FileNotFoundError:
            return TmuxTerminalBackend._SERVER_ABSENT
        if not stat.S_ISSOCK(info.st_mode):
            return TmuxTerminalBackend._SERVER_UNKNOWN
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(0.2)
            connection.connect(str(socket_path))
        except (FileNotFoundError, ConnectionRefusedError):
            return TmuxTerminalBackend._SERVER_ABSENT
        except (OSError, TimeoutError):
            return TmuxTerminalBackend._SERVER_UNKNOWN
        finally:
            connection.close()
        return "reachable"

    def _probe_server(self, socket_path: Path) -> str:
        """Return owned/foreign/absent, or unknown when the server cannot be queried."""
        socket = socket_path.expanduser().absolute()
        try:
            result = self._run(
                socket,
                "show-option",
                "-gqv",
                "@llm_bridge_station",
                check=False,
                capture=True,
            )
        except FileNotFoundError:
            return self._socket_listener_state(socket)
        except (PermissionError, subprocess.TimeoutExpired):
            listener = self._socket_listener_state(socket)
            return self._SERVER_ABSENT if listener == self._SERVER_ABSENT else self._SERVER_UNKNOWN
        if result.returncode == 0:
            return self._SERVER_OWNED if result.stdout.strip() == "1" else self._SERVER_FOREIGN
        listener = self._socket_listener_state(socket)
        return self._SERVER_ABSENT if listener == self._SERVER_ABSENT else self._SERVER_UNKNOWN

    @classmethod
    def _has_bridge_config_marker(cls, config_path: Path) -> bool:
        if not config_path.name.endswith(".conf"):
            return False
        try:
            cls._validate_config_path(config_path.with_name(config_path.name[:-5]), config_path)
            content = config_path.read_text(encoding="utf-8")
        except (OSError, PermissionError, UnicodeError):
            return False
        return cls._BRIDGE_CONFIG_MARKER in content

    def _remove_dead_server_artifacts(self, handle: TerminalHandle) -> None:
        """Remove a dead server's socket and Bridge config, tolerating concurrent cleanup.

        Several callers can reach a dead socket at once: `stop()`, the
        `attach()` client that lost its server, and `reclaim_dead_station()`.
        A file that disappears between the checks and the unlink was removed
        by one of them, which is the outcome wanted here, so it is not an
        error. Ownership checks still apply to every file that is present.
        """
        socket_path = handle.socket_path
        config_path = handle.config_path
        socket_exists = socket_path.exists()
        config_exists = config_path.exists()
        if not socket_exists and not config_exists:
            return
        if config_exists and not self._has_bridge_config_marker(config_path):
            if config_path.exists():
                raise PermissionError(f"cannot prove ownership of dead tmux server metadata: {socket_path}")
            config_exists = False  # removed concurrently while it was being read
        if socket_exists:
            self._validate_socket_path(socket_path, allow_missing=True)
        if config_exists:
            try:
                self._validate_config_path(socket_path, config_path)
            except PermissionError:
                if config_path.exists():
                    raise
                config_exists = False
        if socket_exists:
            socket_path.unlink(missing_ok=True)
        if config_exists:
            config_path.unlink(missing_ok=True)

    def reclaim_dead_station(self, socket_path: str | Path) -> bool:
        """Remove the artifacts of a station whose server is gone.

        tmux never unlinks its socket when a server exits, and a late
        `attach-session` recreates one. The socket is removed only when it
        lives in a private directory owned by this user, is a private socket
        owned by this user, and refuses connections. Its `.conf` sibling is
        removed with it, but only when it carries the Bridge marker. A socket
        that accepts connections is never touched. Returns whether anything
        was removed.
        """
        socket_path = Path(socket_path).expanduser().absolute()
        config_path = socket_path.with_name(socket_path.name + ".conf")
        self._validate_socket_path(socket_path, allow_missing=True)
        if self._probe_server(socket_path) != self._SERVER_ABSENT:
            return False
        # A server may have bound the path since the first probe.
        if self._socket_listener_state(socket_path) != self._SERVER_ABSENT:
            return False
        present = socket_path.exists() or config_path.exists()
        self._remove_dead_server_artifacts(TerminalHandle(socket_path, config_path, "", "", "", 6))
        return present

    def _require_owned(self, handle: TerminalHandle) -> None:
        self._validate_socket_path(handle.socket_path)
        self._validate_config_path(handle.socket_path, handle.config_path)
        if not self.server_owned(handle.socket_path):
            raise PermissionError(f"tmux server is not an LLM Bridge station: {handle.socket_path}")

    def open_existing(
        self,
        socket_path: str | Path,
        session_name: str,
        *,
        bridge_rows: int = 6,
    ) -> TerminalHandle:
        """Rebuild a handle for an owned private server without restarting panes."""
        socket = Path(socket_path).expanduser().absolute()
        config = socket.with_name(socket.name + ".conf")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", session_name):
            raise ValueError("session_name may contain only letters, digits, dot, underscore, and hyphen")
        self._validate_socket_path(socket)
        cache_key = (str(socket), session_name, bridge_rows)
        with _CACHE_LOCK:
            cached = _STATION_HANDLES.get(cache_key)
        if cached is not None and cached[1] > time.monotonic() and cached[0] == _socket_identity(socket) and self._verified_owned(socket):
            self._validate_config_path(socket, config)
            return cached[2]
        state = self._probe_server(socket)
        if state == self._SERVER_UNKNOWN:
            raise RuntimeError(f"cannot determine tmux server ownership: {socket}")
        if state == self._SERVER_ABSENT:
            self.reclaim_dead_station(socket)
            raise DeadStationError(f"no live terminal station: removed dead tmux socket {socket}")
        if state != self._SERVER_OWNED:
            raise PermissionError(f"tmux server is not an LLM Bridge station: {socket}")
        self._validate_config_path(socket, config)
        if bridge_rows < 5 or bridge_rows > 8:
            raise ValueError("bridge_rows must be between 5 and 8")
        result = self._run(
            socket,
            "list-panes",
            "-t",
            f"{session_name}:station",
            "-F",
            "#{pane_index} #{pane_id}",
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"no live terminal station named {session_name}")
        panes: dict[int, str] = {}
        for line in result.stdout.splitlines():
            index_text, separator, pane_id = line.partition(" ")
            if separator and index_text.isdigit():
                panes[int(index_text)] = pane_id.strip()
        if set(panes) != {0, 1}:
            raise RuntimeError("Bridge terminal station must contain exactly one provider pane and one command pane")
        handle = TerminalHandle(socket, config, session_name, panes[0], panes[1], bridge_rows)
        identity = _socket_identity(socket)
        if identity is not None:
            with _CACHE_LOCK:
                _OWNED_SERVERS[str(socket)] = identity
                _STATION_HANDLES[cache_key] = (identity, time.monotonic() + _HANDLE_CACHE_SECONDS, handle)
        return handle

    def session_exists(self, handle: TerminalHandle) -> bool:
        """Return whether this station's session still exists."""
        if not self.server_owned(handle.socket_path):
            return False
        try:
            result = self._run(handle.socket_path, "has-session", "-t", handle.session_name, check=False)
        except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def is_alive(self, handle: TerminalHandle) -> bool:
        """Return whether the provider process is alive in its native pane."""
        return self.session_exists(handle) and self.pane_is_alive(handle, pane="provider")

    def pane_is_alive(self, handle: TerminalHandle, *, pane: str) -> bool:
        """Return whether the native process in a pane is still running."""
        if not self.server_owned(handle.socket_path):
            return False
        try:
            result = self._run(
                handle.socket_path,
                "display-message",
                "-p",
                "-t",
                self._target(handle, pane),
                "#{pane_dead}",
                check=False,
                capture=True,
            )
        except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and result.stdout.strip() == "0"

    def pane_activity_epoch(self, handle: TerminalHandle, *, pane: str = "provider") -> int | None:
        """Return tmux's last-activity epoch for the pane's window, or None when unreadable.

        tmux tracks activity per window, so output from any pane in the window
        updates it. A missing tmux, dead pane, foreign server or malformed value
        yields None.
        """
        try:
            if not self.server_owned(handle.socket_path):
                return None
            result = self._run(
                handle.socket_path,
                "display-message",
                "-p",
                "-t",
                self._target(handle, pane),
                "#{pane_dead} #{window_activity}",
                check=False,
                capture=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        fields = result.stdout.split()
        if result.returncode != 0 or len(fields) != 2 or fields[0] != "0":
            return None
        try:
            epoch = int(fields[1])
        except ValueError:
            return None
        return epoch if epoch > 0 else None

    def pane_activity_screen(
        self,
        handle: TerminalHandle,
        *,
        pane: str = "provider",
        seen_epoch: int | None = None,
    ) -> tuple[int, str | None] | None:
        """Read the pane's activity epoch and, only if it moved, its screen, in one tmux invocation.

        Returns ``(epoch, screen)`` where ``screen`` is ``None`` when the epoch
        equals ``seen_epoch`` (tmux evaluates that condition itself, so an
        unchanged pane costs no capture). Returns ``None`` when the pane is
        dead, the server is not an owned station, or the reply is malformed.
        """
        try:
            if not self.server_owned(handle.socket_path):
                return None
            target = self._target(handle, pane)
            result = self._run(
                handle.socket_path,
                *self._chain([
                    ["display-message", "-p", "-t", target, "#{pane_dead} #{window_activity}"],
                    ["if-shell", "-F", "-t", target, f"#{{!=:#{{window_activity}},{int(seen_epoch or 0)}}}", f"capture-pane -p -J -t {target}"],
                ]),
                check=False,
                capture=True,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        header, _, screen = result.stdout.partition("\n")
        fields = header.split()
        if result.returncode != 0 or len(fields) != 2 or fields[0] != "0":
            return None
        try:
            epoch = int(fields[1])
        except ValueError:
            return None
        if epoch <= 0:
            return None
        return epoch, (None if epoch == (seen_epoch or 0) else screen)

    def _fit_window_to_terminal(self, handle: TerminalHandle) -> None:
        """Give a station that has no client the size of the terminal that is about to attach.

        The attach would otherwise resize the window, and the ``client-resized`` hook would then
        restore the command pane's height by unzooming and zooming the window, 50 ms after the
        first frame. The provider is resized and repaints twice while the user watches. Done here,
        before the client exists, the layout is settled when the first frame is drawn and the hook
        finds nothing to correct. ``resize-window`` switches the window to a fixed size, so the
        window option is removed again to keep following the latest client.
        """
        size = os.get_terminal_size(sys.stdin.fileno())
        cols, rows = size.columns, size.lines - 1  # the status row belongs to tmux
        if cols < 80 or rows < 23:
            return
        window = f"{handle.session_name}:station"
        listing = self._run(
            handle.socket_path, "list-panes", "-t", window, "-F",
            "#{pane_id} #{pane_active} #{window_zoomed_flag} #{window_width} #{window_height} #{session_attached}",
            capture=True,
        ).stdout.splitlines()
        panes = [line.split() for line in listing if len(line.split()) == 6]
        active = next((fields for fields in panes if fields[1] == "1"), None)
        if active is None or active[5] != "0" or (active[3], active[4]) == (str(cols), str(rows)):
            return  # another client is attached (the window follows it), or nothing to change
        commands = [
            ["resize-window", "-t", window, "-x", str(cols), "-y", str(rows)],
            ["resize-pane", "-t", handle.bridge_pane, "-y", str(handle.bridge_rows)],
        ]
        if active[2] == "1":
            commands.append(["resize-pane", "-Z", "-t", active[0]])
        commands.append(["set-option", "-w", "-u", "-t", window, "window-size"])
        self._run(handle.socket_path, *self._chain(commands))

    def attach(self, handle: TerminalHandle) -> int:
        """Attach the current real terminal to the station's native PTYs.

        tmux draws through the terminal device it finds from stdin, and prints
        its exit line (``[detached (from session mission-<32 hex>)]``) to
        stdout. stdout is therefore captured: a detach from a mission station is
        reported with ``detach_notice`` instead, and any other exit line is
        passed through unchanged.
        """
        self._require_owned(handle)
        try:
            self._fit_window_to_terminal(handle)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            pass  # only avoids a repaint; attaching works at any size
        result = subprocess.run(
            [resolve_executable("tmux"), "-S", str(handle.socket_path), "-f", str(handle.config_path), "attach-session", "-t", handle.session_name],
            check=False,
            timeout=None,
            stdout=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        line = (result.stdout or "").strip()
        if line:
            notice = detach_notice(handle.session_name) if result.returncode == 0 and line.startswith("[detached") else ""
            print(notice or line, flush=True)
        if result.returncode != 0:
            # attach-session starts a server when the station died after the
            # ownership check above; do not leave its dead socket behind.
            self._reclaim_after_failed_attach(handle)
        return result.returncode

    @staticmethod
    def _mission_lock(session_name: str) -> Any | None:
        """The controller lock of the mission a station belongs to, if its name says so."""
        match = re.fullmatch(r"mission-([0-9a-f]{32})", session_name)
        if match is None:
            return None
        from .bridge_store import MissionLock, default_state_dir

        return MissionLock(default_state_dir() / "locks", match.group(1))

    def _reclaim_after_failed_attach(self, handle: TerminalHandle) -> None:
        """Remove the dead socket of a failed attach, unless a controller may be binding it.

        A starting controller holds the mission lock for its whole life and binds
        the socket under it. Without the lock, reclaiming could unlink a socket
        that a new server has just created. A busy lock means someone else owns
        the socket, so it is left alone.
        """
        lock = self._mission_lock(handle.session_name)
        if lock is not None:
            try:
                lock.acquire()
            except (BlockingIOError, OSError):
                return
        try:
            self.reclaim_dead_station(handle.socket_path)
        except (OSError, RuntimeError):
            pass
        finally:
            if lock is not None:
                lock.release()

    def stop(self, handle: TerminalHandle) -> None:
        """Stop an owned server; retain its metadata unless teardown is proven."""
        state = self._probe_server(handle.socket_path)
        if state == self._SERVER_FOREIGN:
            raise PermissionError(f"refusing to stop a non-Bridge tmux server: {handle.socket_path}")
        if state == self._SERVER_UNKNOWN:
            raise RuntimeError(f"cannot determine tmux server ownership; preserving metadata: {handle.socket_path}")
        if state == self._SERVER_ABSENT:
            self._remove_dead_server_artifacts(handle)
            return

        error: BaseException | None = None
        try:
            result = self._run(handle.socket_path, "kill-server", check=False)
            if result.returncode != 0:
                error = RuntimeError(f"tmux kill-server failed with exit status {result.returncode}: {handle.socket_path}")
        except (FileNotFoundError, PermissionError, subprocess.SubprocessError) as exc:
            error = RuntimeError(f"tmux kill-server could not be confirmed: {handle.socket_path}")
            error.__cause__ = exc

        deadline = time.monotonic() + self._TEARDOWN_CONFIRM_TIMEOUT_SECONDS
        while True:
            state = self._probe_server(handle.socket_path)
            if state == self._SERVER_ABSENT:
                self._remove_dead_server_artifacts(handle)
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)

        if state == self._SERVER_FOREIGN:
            raise RuntimeError(f"tmux socket was replaced during teardown; preserving metadata: {handle.socket_path}") from error
        if state == self._SERVER_UNKNOWN:
            raise RuntimeError(f"tmux server teardown is unverified; preserving metadata: {handle.socket_path}") from error
        if error is not None:
            raise error
        raise RuntimeError(f"tmux server remains alive after kill-server; preserving metadata: {handle.socket_path}")



# --- station policy ------------------------------------------------------------------------------
# Which tmux server a console may act on, and what a terminal action does there. The console
# (``bridge_tui``) and the mission switcher call these; they hold no curses or projection code.

ACTIVITY_SEEN: dict[str, tuple[int, str]] = {}  # mission id -> (epoch, digest) of the last screen capture


def read_provider_activity(mission_id: str, load_handle: Callable[[str], TerminalHandle]) -> tuple[int, str] | None:
    """Read the provider pane's tmux activity epoch and a digest of its screen.

    tmux activity is per window and the Bridge pane shares the window, so the
    digest lets the caller ignore activity that did not change provider output.
    One `display-message` reads the epoch; the screen is captured (the costly
    call) only when that epoch differs from the one captured last time.
    Raises whatever the handle lookup or tmux raises; callers treat observation as best effort.
    """
    backend = TmuxTerminalBackend()
    handle = load_handle(mission_id)
    seen = ACTIVITY_SEEN.get(mission_id)
    # One tmux invocation: the epoch, plus the screen only when the epoch differs from `seen`.
    reading = backend.pane_activity_screen(handle, pane="provider", seen_epoch=seen[0] if seen is not None else None)
    if reading is None:
        ACTIVITY_SEEN.pop(mission_id, None)
        return None
    epoch, screen = reading
    if screen is None and seen is not None:
        return seen
    result = (epoch, hashlib.sha1((screen or "").encode("utf-8", "replace")).hexdigest())
    ACTIVITY_SEEN[mission_id] = result
    return result


def inside_station() -> bool:
    """True when this console runs in a Bridge-owned tmux station, not any tmux."""
    socket_path = Path(os.environ.get("TMUX", "").split(",")[0])
    return socket_path.name.startswith("m-") and (socket_path.parent.name == "llm-bridge" or socket_path.parent.name.startswith("lb-"))


@contextlib.contextmanager
def outside_tmux_client() -> Iterator[None]:
    """Run a tmux client call as if started from a plain terminal.

    tmux refuses to attach from inside another tmux pane while ``$TMUX`` is
    set. A nested attach to a Bridge station is deliberate, so the variables
    are removed for the duration of the call and restored afterwards.
    """
    saved = {name: os.environ.pop(name) for name in ("TMUX", "TMUX_PANE") if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


_STATION_SOCKET_NAME = re.compile(r"m-[0-9a-f]{20}\.sock")


def is_bridge_station_socket(backend: object, socket_path: Path) -> bool:
    """Whether a tmux socket belongs to a Bridge station.

    The server's own marker decides. When the server cannot be queried (for
    example the socket lives in a directory Bridge would not trust), only a
    Bridge-style socket name counts as a station; other servers are the
    user's own tmux.
    """
    try:
        return bool(backend.server_owned(socket_path))  # type: ignore[attr-defined]
    except (OSError, RuntimeError, subprocess.SubprocessError, AttributeError):
        return _STATION_SOCKET_NAME.fullmatch(socket_path.name) is not None


def execute_terminal_action(source_mission: str, action: TerminalAction, load_handle: Callable[[str], TerminalHandle]) -> None:
    """Perform a typed terminal action after curses restores the user's terminal.

    A console inside a Bridge station may act only from its own Bridge pane;
    a console inside the user's own tmux (or no tmux) attaches like a plain terminal.
    """
    backend = TmuxTerminalBackend()
    destination = load_handle(action.mission_id)
    tmux_env = os.environ.get("TMUX", "")
    pane_env = os.environ.get("TMUX_PANE", "")
    source = None
    if tmux_env:
        source = load_handle(source_mission)
        try:
            tmux_socket = Path(tmux_env.split(",", 1)[0]).expanduser().absolute()
            same_socket = tmux_socket == source.socket_path.expanduser().absolute()
        except OSError:
            tmux_socket, same_socket = None, False
        if not same_socket and (tmux_socket is None or is_bridge_station_socket(backend, tmux_socket)):
            raise PermissionError("terminal actions are available only from this mission's owned Bridge pane")
        if not same_socket:
            # The console runs inside the user's own tmux: behave like a plain
            # terminal and attach (or detach) the current client of that tmux.
            source = None
    if source is None:
        if action.action == "detach":
            backend.detach_client(destination, client_name=action.client)
            return
        if action.mission_id == source_mission:
            backend.focus(destination, pane="provider", expand=True)
        with outside_tmux_client():
            backend.attach(destination)
        return

    if pane_env != source.bridge_pane:
        raise PermissionError("terminal actions are available only from this mission's owned Bridge pane")
    if action.action == "detach":
        if action.mission_id != source_mission:
            raise PermissionError("detach from an embedded console is limited to its active mission")
        backend.detach_client(source, client_name=action.client)
        return
    if action.mission_id == source_mission:
        backend.focus(source, pane="provider", expand=True)
        return

    backend.switch_client_to(source, destination, client_name=action.client)


def publish_station_status(
    mission_id: str,
    load_handle: Callable[[str], TerminalHandle],
    *,
    where: str,
    text: str,
    chip: str,
    segments: list[tuple[str, str]],
) -> None:
    """Write the console's status label, state text and chip to its own station's tmux status line.

    Does nothing unless the calling pane is this mission's Bridge pane. ``where`` and ``text`` are
    plain strings and ``segments`` are (text, role) pairs; all are localised here.
    """
    handle = load_handle(mission_id)
    if os.environ.get("TMUX_PANE") != handle.bridge_pane:
        return
    backend = TmuxTerminalBackend()
    localized = [(localize(part), part_role) for part, part_role in segments]
    publish = getattr(backend, "publish_status", None)
    if publish is not None:
        # One tmux invocation, and only for values that changed.
        publish(handle, label=localize(where), context=localize(text), chip=chip, segments=localized)
        return
    set_label = getattr(backend, "set_status_label", None)
    if set_label is not None:
        set_label(handle, localize(where))
    backend.set_status_context(handle, localize(text))
    for method, argument in (("set_status_chip", chip), ("set_status_styled", localized)):
        setter = getattr(backend, method, None)
        if setter is not None:
            setter(handle, argument)
