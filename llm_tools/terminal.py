"""Native terminal stations backed by an isolated tmux PTY server.

The provider owns its own real tmux pane and terminal. LLM Bridge never
emulates a VT screen or parses provider output to render it; attaching to the
private tmux server lets tmux forward terminal input and resize events to the
native process.
"""

from __future__ import annotations

import os
import re
import shlex
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


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
    action: Literal["attach", "detach"] = "attach"


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
    ) -> subprocess.CompletedProcess[str]:
        self._validate_socket_path(socket_path, allow_missing=args and args[0] == "new-session")
        if config_path is not None and config_path != Path("/dev/null"):
            self._validate_config_path(socket_path, config_path)
        command = ["tmux", "-S", str(socket_path), "-f", str(config_path or "/dev/null"), *args]
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            check=check,
            timeout=self._COMMAND_TIMEOUT_SECONDS,
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
    ) -> TerminalHandle:
        """Start a provider and command pane on a private tmux server.

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
        if socket.exists():
            raise FileExistsError(f"private tmux socket already exists: {socket}")
        config_path = socket.with_name(socket.name + ".conf")
        if config_path.exists():
            raise FileExistsError(f"private tmux configuration already exists: {config_path}")
        provider_cmd = self._command(provider_argv)
        bridge_cmd = self._command(bridge_argv)
        config = (
            "set-option -g remain-on-exit on\n"
            "set-option -g mouse on\n"
            f"set-option -g prefix {prefix}\n"
            "set-option -g focus-events on\n"
            "set-option -g @llm_bridge_station 1\n"
            'set-option -g default-terminal "screen-256color"\n'
            "set-option -g status on\n"
            "set-option -g status-style 'bg=colour234,fg=colour250'\n"
            "set-option -g status-left-length 42\n"
            "set-option -g status-right-length 30\n"
            "set-option -g status-left '#[fg=colour39,bold] Bridge controls [C-] B] #[default]'\n"
            "set-option -g status-right '#[fg=colour76,bold] Provider [C-] P] #[default]'\n"
        )
        config += (
            f"bind-key {prefix} send-prefix\n"
            "bind-key -T root MouseDown1Pane select-pane -t = \\; send-keys -M \\; select-pane -t =\n"
            "bind-key -T root MouseUp1Pane select-pane -t = \\; send-keys -M\n"
            'bind-key -T root WheelUpPane select-pane -t = \\; if-shell -F "#{||:#{alternate_on},#{pane_in_mode},#{mouse_any_flag}}" { send-keys -M } { copy-mode -e }\n'
            'bind-key -T root WheelDownPane select-pane -t = \\; if-shell -F "#{||:#{alternate_on},#{pane_in_mode},#{mouse_any_flag}}" { send-keys -M } { send-keys -M }\n'
        )
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
            self._run(
                socket,
                "new-session",
                "-d",
                "-s",
                session_name,
                "-n",
                "station",
                "-x",
                str(cols),
                "-y",
                str(rows),
                "-c",
                str(workdir),
                provider_cmd,
                config_path=config_path,
            )
            self._validate_socket_path(socket)
            pane = self._run(
                socket,
                "display-message",
                "-p",
                "-t",
                f"{session_name}:station.0",
                "#{pane_id}",
                capture=True,
            ).stdout.strip()
            bridge_pane = self._run(
                socket,
                "split-window",
                "-v",
                "-l",
                str(bridge_rows),
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                pane,
                "-c",
                str(workdir),
                bridge_cmd,
                capture=True,
            ).stdout.strip()
            # tmux's proportional layout resize can collapse the command pane
            # when an attached client shrinks. Keep the reserved control area
            # at its configured height and let the native pane absorb the
            # remaining rows.
            resize_command = shlex.join(
                [
                    "tmux",
                    "-S",
                    str(socket),
                    "-f",
                    str(config_path),
                    "resize-pane",
                    "-t",
                    bridge_pane,
                    "-y",
                    str(bridge_rows),
                ]
            )
            resize_after_client = f"sleep 0.05; exec {resize_command}"
            self._run(
                socket,
                "set-hook",
                "-g",
                "client-resized",
                f"run-shell -b {shlex.quote(resize_after_client)}",
            )
            # Native tmux left/right status hitboxes remain outside every
            # provider viewport, including while a pane is zoomed.
            self._run(
                socket,
                "set-option", "-g", "status-left",
                "#[fg=colour39,bold] Bridge controls [C-] B] #[default]",
            )
            self._run(
                socket,
                "set-option", "-g", "status-right",
                "#[fg=colour76,bold] Provider [C-] P] #[default]",
            )
            # Use pane IDs captured from this exact launch. Prefix shortcuts
            # are in tmux's prefix table, so native providers receive all keys
            # until the operator deliberately sends Ctrl-]. Status-line clicks
            # remain available even while the provider pane is zoomed.
            for key, target in (("B", bridge_pane), ("P", pane)):
                self._run(
                    socket,
                    "bind-key", "-T", "prefix", key,
                    "if-shell", "-F", "#{window_zoomed_flag}",
                    f"resize-pane -Z; select-pane -t {target}",
                    f"select-pane -t {target}",
                )
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
                self._run(
                    socket,
                    "bind-key", "-T", "root", mouse_key,
                    "if-shell", "-F", "#{window_zoomed_flag}",
                    f"resize-pane -Z; select-pane -t {target}",
                    f"select-pane -t {target}",
                )
            self._run(socket, "select-pane", "-t", pane)
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
        if keys in {"Enter", "C-c", "C-d", "Escape", "Tab", "BSpace", "Space"}:
            self._run(handle.socket_path, "send-keys", "-t", self._target(handle, pane), keys)
        else:
            self._run(handle.socket_path, "send-keys", "-l", "-t", self._target(handle, pane), keys)

    def focus(self, handle: TerminalHandle, *, pane: str, expand: bool = False) -> None:
        """Select an owned pane; optionally give its existing UI the full window."""
        self._require_owned(handle)
        target = self._target(handle, pane)
        self._run(handle.socket_path, "select-pane", "-t", target)
        if expand:
            zoomed = self._run(handle.socket_path, "display-message", "-p", "-t", target,
                               "#{window_zoomed_flag}", capture=True).stdout.strip()
            if zoomed != "1":
                self._run(handle.socket_path, "resize-pane", "-Z", "-t", target)

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
        raise RuntimeError("multiple writable clients are attached; specify --client from the listed identities")

    def detach_client(self, handle: TerminalHandle, *, client_name: str | None = None) -> None:
        """Detach one exact writable client without disturbing observers."""
        self._require_owned(handle)
        selected = self.select_writable_client(handle, client_name)
        self._run(handle.socket_path, "detach-client", "-t", selected)

    def switch_client_to(
        self,
        source: TerminalHandle,
        destination: TerminalHandle,
        *,
        client_name: str | None = None,
    ) -> None:
        """Detach one verified source client into another station, then return.

        Only an explicitly named writable client is detached. The `-E`
        command attaches to the destination and, after that client detaches,
        reattaches to the source station so its Bridge console redraws.
        """
        self._require_owned(source)
        self._require_owned(destination)
        selected_client = self.select_writable_client(source, client_name)
        destination_attach = shlex.join([
            "env", "-u", "TMUX", "-u", "TMUX_PANE", "tmux",
            "-S", str(destination.socket_path), "-f", str(destination.config_path),
            "attach-session", f"-t={destination.session_name}",
        ])
        source_attach = shlex.join([
            "env", "-u", "TMUX", "-u", "TMUX_PANE", "tmux",
            "-S", str(source.socket_path), "-f", str(source.config_path),
            "attach-session", f"-t={source.session_name}",
        ])
        return_message = "llm-bridge: destination session unavailable; returning to the source station"
        shell_script = f"{destination_attach} || printf '%s\\n' {shlex.quote(return_message)} >&2; {source_attach}"
        shell_command = "exec env -u TMUX -u TMUX_PANE sh -c " + shlex.quote(shell_script)
        self._run(
            source.socket_path,
            "detach-client",
            "-t",
            selected_client,
            "-E",
            shell_command,
        )

    def resize(self, handle: TerminalHandle, *, cols: int, rows: int) -> None:
        self._require_owned(handle)
        if cols < 80 or rows < 24:
            raise ValueError("terminal size must be at least 80 columns by 24 rows")
        self._run(handle.socket_path, "resize-window", "-t", handle.session_name, "-x", str(cols), "-y", str(rows))
        self._run(handle.socket_path, "resize-pane", "-t", handle.bridge_pane, "-y", str(handle.bridge_rows))

    def server_exists(self, handle: TerminalHandle) -> bool:
        """Return whether the private tmux server responds on its socket."""
        state = self._probe_server(handle.socket_path)
        if state == self._SERVER_UNKNOWN:
            raise RuntimeError(f"cannot determine whether tmux server is alive: {handle.socket_path}")
        return state in {self._SERVER_OWNED, self._SERVER_FOREIGN}

    def server_owned(self, socket_path: str | Path) -> bool:
        """Return whether a responsive tmux server identifies as an LLM Bridge station."""
        socket = Path(socket_path).expanduser().absolute()
        state = self._probe_server(socket)
        if state == self._SERVER_UNKNOWN:
            raise RuntimeError(f"cannot determine tmux server ownership: {socket}")
        return state == self._SERVER_OWNED

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
        socket_path = handle.socket_path
        config_path = handle.config_path
        socket_exists = socket_path.exists()
        config_exists = config_path.exists()
        if not socket_exists and not config_exists:
            return
        if config_exists and not self._has_bridge_config_marker(config_path):
            raise PermissionError(f"cannot prove ownership of dead tmux server metadata: {socket_path}")
        if socket_exists:
            self._validate_socket_path(socket_path)
        if config_exists:
            self._validate_config_path(socket_path, config_path)
        if socket_exists:
            socket_path.unlink()
        if config_exists:
            config_path.unlink()

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
        if not self.server_owned(socket):
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
        return TerminalHandle(socket, config, session_name, panes[0], panes[1], bridge_rows)

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

    def attach(self, handle: TerminalHandle) -> int:
        """Attach the current real terminal to the station's native PTYs."""
        self._require_owned(handle)
        result = subprocess.run(
            ["tmux", "-S", str(handle.socket_path), "-f", str(handle.config_path), "attach-session", "-t", handle.session_name],
            check=False,
            timeout=None,
        )
        return result.returncode

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

