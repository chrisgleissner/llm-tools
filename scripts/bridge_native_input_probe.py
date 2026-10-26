#!/usr/bin/env python3
"""Compare zero-prompt native CLI input through Bridge tmux and direct PTY.

This opt-in probe resumes only the explicitly supplied, locally verified
Codex session. It submits no model prompt. Codex `/help` and `/model` and
Claude `/help` are local UI commands; no command after those is accepted.
Raw terminal transcripts are examined in memory and never written to disk.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import sys
import termios
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm_tools.bridge_sessions import provider_session_metadata  # noqa: E402
from llm_tools.terminal import TmuxTerminalBackend  # noqa: E402

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_WINDOW = (120, 40)


def _plain(data: bytes | str) -> str:
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return _ANSI.sub("", text).replace("\r", "")


def help_visible(text: str) -> bool:
    """Require help content, not merely the echoed `/help` input."""
    clean = _plain(text).lower()
    return ("help" in clean and "model" in clean and
            any(token in clean for token in ("commands", "keyboard shortcuts", "built-in")))


def model_picker_visible(text: str, *, before: str = "") -> bool:
    clean = _plain(text).lower()
    before_clean = _plain(before).lower()
    return (
        "select model" in clean and "(current)" in clean
    ) or (
        "model" in clean and "gpt-6-luna" in clean and "gpt-6-luna" not in before_clean
    )


def mouse_effect(before: str, after: str) -> str:
    before_text, after_text = _plain(before), _plain(after)
    if "Select Model" in before_text and "reasoning effort" in after_text.lower():
        return "effort-picker"
    if "(current)" in before_text and "(current)" not in after_text:
        return "picker-dismissed-or-selection-made"
    if before_text == after_text:
        return "no-visible-effect"
    return "screen-changed"


def wheel_effect(before: str, after: str, *, provider_mode: int | None = None) -> str:
    if provider_mode not in (None, 0):
        return "not-tested-tmux-copy-mode-active"
    return "visible-scroll" if _plain(before) != _plain(after) else "no-visible-effect"


def _version(executable: str) -> str:
    result = subprocess.run([executable, "--version"], check=False, capture_output=True,
                            text=True, timeout=8)
    return (result.stdout or result.stderr).strip().splitlines()[0][:120]


class _DirectPTY:
    """Launch the CLI directly in an ordinary one-pane tmux PTY for comparison."""

    def __init__(self, argv: list[str], cwd: Path, *, timeout: float, socket_path: Path) -> None:
        self.socket_path = socket_path
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        socket_path.parent.chmod(0o700)
        env = {key: value for key, value in os.environ.items() if key != "TMUX"}
        env["TERM"] = "xterm-256color"
        subprocess.run(
            ["tmux", "-S", str(socket_path), "-f", "/dev/null", "new-session", "-d", "-s", "direct",
             "-x", str(_WINDOW[0]), "-y", str(_WINDOW[1]), shlex.join(argv)],
            cwd=cwd, env=env, check=True, capture_output=True, timeout=8,
        )
        for command in (
            ("set-option", "-g", "mouse", "on"),
            ("set-option", "-g", "status", "off"),
            ("bind-key", "-T", "root", "MouseDown1Pane", "select-pane", "-t", "=", "\\;", "send-keys", "-M", "\\;", "select-pane", "-t", "="),
            ("bind-key", "-T", "root", "WheelUpPane", "select-pane", "-t", "=", "\\;", "send-keys", "-M"),
        ):
            subprocess.run(["tmux", "-S", str(socket_path), *command], check=True,
                           capture_output=True, text=True, timeout=3)
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", _WINDOW[1], _WINDOW[0], 0, 0))

        def controlling_terminal() -> None:
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.process = subprocess.Popen(
            ["tmux", "-S", str(socket_path), "-f", "/dev/null", "attach-session", "-t", "direct"],
            cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, preexec_fn=controlling_terminal,
        )
        os.close(slave)
        os.set_blocking(self.master, False)
        self.deadline = time.monotonic() + timeout

    def send(self, payload: bytes) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("provider interaction deadline expired")
        os.write(self.master, payload)

    def screen(self, duration: float = 0.2) -> str:
        end = min(self.deadline, time.monotonic() + duration)
        while time.monotonic() < end and self.process.poll() is None:
            ready, _, _ = select.select([self.master], [], [], min(0.05, end - time.monotonic()))
            if ready:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
        try:
            return subprocess.run(
                ["tmux", "-S", str(self.socket_path), "capture-pane", "-p", "-t", "direct:0"],
                check=True, capture_output=True, text=True, timeout=3,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.send(b"\x03")
                self.process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired, TimeoutError):
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=2)
        os.close(self.master)
        subprocess.run(["tmux", "-S", str(self.socket_path), "kill-server"],
                       check=False, capture_output=True, timeout=5)


class _StationPTY:
    def __init__(self, backend: TmuxTerminalBackend, handle, *, timeout: float) -> None:
        self.backend, self.handle = backend, handle
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", _WINDOW[1], _WINDOW[0], 0, 0))

        def controlling_terminal() -> None:
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        code = (
            "from pathlib import Path; from llm_tools.terminal import TerminalHandle,TmuxTerminalBackend; "
            f"h=TerminalHandle(Path({str(handle.socket_path)!r}),Path({str(handle.config_path)!r}),"
            f"{handle.session_name!r},{handle.provider_pane!r},{handle.bridge_pane!r},{handle.bridge_rows}); "
            "raise SystemExit(TmuxTerminalBackend().attach(h))"
        )
        env = {key: value for key, value in os.environ.items() if key != "TMUX"}
        env["TERM"] = "xterm-256color"
        self.process = subprocess.Popen(
            [sys.executable, "-c", code], cwd=ROOT, env=env, stdin=slave, stdout=slave,
            stderr=slave, close_fds=True, preexec_fn=controlling_terminal,
        )
        os.close(slave)
        os.set_blocking(self.master, False)
        self.deadline = time.monotonic() + timeout
        self.output = bytearray()

    def send(self, payload: bytes) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("provider interaction deadline expired")
        os.write(self.master, payload)

    def screen(self, duration: float = 0.2) -> str:
        end = min(self.deadline, time.monotonic() + duration)
        while time.monotonic() < end and self.process.poll() is None:
            ready, _, _ = select.select([self.master], [], [], min(0.05, end - time.monotonic()))
            if ready:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.output.extend(data)
        return self.backend.capture(self.handle, pane="provider")

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.send(b"\x03")
                self.process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired, TimeoutError):
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=2)
        os.close(self.master)


def _wait_ready(console, *, provider: str, timeout: float) -> str:
    deadline = min(console.deadline, time.monotonic() + timeout)
    latest = ""
    previous = ""
    stable_reads = 0
    while time.monotonic() < deadline:
        if console.process.poll() is not None:
            break
        latest = console.screen(0.2)
        lower = _plain(latest).lower()
        if "trust this folder" in lower or "trust this project" in lower:
            return latest
        prompt = "›" in latest if provider == "codex" else "❯" in latest
        if prompt:
            stable_reads = stable_reads + 1 if latest == previous else 0
            if stable_reads >= 1:
                return latest
        previous = latest
    return latest


def _run_ui_sequence(console, *, provider: str) -> dict[str, str | bool]:
    if provider == "codex":
        initial = _wait_ready(console, provider=provider, timeout=12)
        if "trust this folder" in initial.lower() or "trust this project" in initial.lower():
            return {"keyboard_local_model_picker": "NOT VERIFIED: Codex project trust gate requires operator input",
                    "model_picker_signal_observed": False, "mouse": "NOT TESTED: trust gate",
                    "wheel": "NOT TESTED: trust gate"}
        console.send(b"/model\r")
        console.screen(0.6)  # commit the local slash-command suggestion
        console.send(b"\r")  # open the provider-local model menu; no prompt is submitted
        picker = console.screen(0.8)
        for _ in range(6):
            if model_picker_visible(picker, before=initial):
                break
            picker = console.screen(0.4)
        result: dict[str, str | bool] = {
            "keyboard_local_model_picker": "PASS" if model_picker_visible(picker, before=initial) else "NOT VERIFIED",
            "model_picker_signal_observed": model_picker_visible(picker, before=initial),
        }
        line_index = next((index for index, line in enumerate(picker.splitlines())
                           if "(current)" in line or "gpt-6-luna" in line), None)
        if line_index is None:
            result["mouse"] = "NOT VERIFIED: model picker row unavailable"
            result["wheel"] = "NOT TESTED: model picker unavailable"
            return result
        line = picker.splitlines()[line_index]
        column = line.find("(current)")
        x = 12 if column > 12 else min(max(2, column), _WINDOW[0] - 2)
        y = min(line_index + 1, _WINDOW[1] - 2)
        mouse_before = picker
        result["mouse_click_x"] = x
        result["mouse_click_y"] = y
        if isinstance(console, _StationPTY):
            mode_target = console.handle.provider_pane
            mode_socket = console.handle.socket_path
        else:
            mode_target = "direct:0"
            mode_socket = console.socket_path
        mode_text = subprocess.run(
            ["tmux", "-S", str(mode_socket), "display-message", "-p", "-t", mode_target, "#{pane_in_mode}"],
            check=True, capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        mouse_mode = int(mode_text) if mode_text.isdigit() else None
        result["mouse_pane_in_mode"] = mouse_mode if mouse_mode is not None else "unknown"
        result["model_picker_before_click"] = model_picker_visible(mouse_before, before=initial)
        if mouse_mode not in (None, 0):
            result["mouse"] = "NOT TESTED: tmux pane copy mode active"
        else:
            console.send(f"\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m".encode("ascii"))
            mouse_after = console.screen(0.8)
            result["mouse"] = mouse_effect(mouse_before, mouse_after)
            result["model_picker_after_click"] = model_picker_visible(mouse_after, before=initial)
        console.send(b"\x1b")
        console.screen(0.3)
        before_scroll = console.screen(0.1)
        mode = None
        if isinstance(console, _StationPTY):
            mode_text = subprocess.run(
                ["tmux", "-S", str(console.handle.socket_path), "display-message", "-p", "-t",
                 console.handle.provider_pane, "#{pane_in_mode}"], check=True, capture_output=True,
                text=True, timeout=3,
            ).stdout.strip()
            mode = int(mode_text) if mode_text.isdigit() else None
        if mode in (None, 0):
            console.send(b"\x1b[<64;5;5M")
            after_scroll = console.screen(0.5)
            result["wheel"] = wheel_effect(before_scroll, after_scroll, provider_mode=mode)
        else:
            result["wheel"] = wheel_effect(before_scroll, before_scroll, provider_mode=mode)
    else:
        ready = _wait_ready(console, provider=provider, timeout=12)
        lower = _plain(ready).lower()
        result = {"keyboard_help": "NOT VERIFIED", "help_signal_observed": False}
        # Never type into a project-trust, login, or startup confirmation view.
        if "trust this folder" in lower or "trust this project" in lower:
            result["keyboard_help"] = "NOT VERIFIED: Claude project trust gate requires operator input"
            return result
        if any(token in lower for token in ("login required", "please log in", "authentication required")):
            result["keyboard_help"] = "NOT VERIFIED: Claude authentication gate"
            return result
        console.send(b"/help\r")
        help_screen = console.screen(1.0)
        for _ in range(5):
            if help_visible(help_screen):
                break
            help_screen = console.screen(0.4)
        result["keyboard_help"] = "PASS" if help_visible(help_screen) else "NOT VERIFIED"
        result["help_signal_observed"] = help_visible(help_screen)
    return result


def _provider_result(provider: str, argv: list[str], cwd: Path, *, timeout: float,
                     backend: TmuxTerminalBackend, socket_path: Path) -> dict[str, object]:
    provider_deadline = time.monotonic() + timeout
    result: dict[str, object] = {
        "version": _version(provider), "model_calls": 0,
        "direct": {}, "bridge_tmux": {}, "input_parity": "NOT VERIFIED",
        "mouse_parity": "NOT TESTED", "wheel_parity": "NOT TESTED",
    }
    direct = station = None
    handle = None
    try:
        direct = _DirectPTY(argv, cwd, timeout=max(1.0, provider_deadline - time.monotonic()),
                            socket_path=socket_path.with_name("direct.sock"))
        direct_result = _run_ui_sequence(direct, provider=provider)
        direct.close()
        direct = None

        handle = backend.start(
            provider_argv=argv,
            bridge_argv=[sys.executable, "-c", "import time; time.sleep(180)"],
            cwd=cwd, socket_path=socket_path, session_name=f"input-{provider}", size=_WINDOW,
        )
        station = _StationPTY(backend, handle, timeout=max(1.0, provider_deadline - time.monotonic()))
        station_result = _run_ui_sequence(station, provider=provider)
        station.close()
        station = None
        result["direct"] = direct_result
        result["bridge_tmux"] = station_result
        result["input_parity"] = (
            "PASS: local command input rendered in both paths"
            if (direct_result.get("help_signal_observed") or direct_result.get("model_picker_signal_observed"))
            and (station_result.get("help_signal_observed") or station_result.get("model_picker_signal_observed"))
            else "NOT VERIFIED: local command did not render in both paths"
        )
        if provider == "codex":
            direct_mouse, station_mouse = direct_result.get("mouse"), station_result.get("mouse")
            if direct_mouse == station_mouse == "effort-picker":
                result["mouse_parity"] = "PASS: current-model click opened effort picker in direct and Bridge paths"
            elif direct_mouse == station_mouse:
                result["mouse_parity"] = f"NOT VERIFIED: both paths observed {direct_mouse}"
            else:
                result["mouse_parity"] = f"DIFFERENT: direct={direct_mouse}; bridge={station_mouse}"
            direct_wheel, station_wheel = direct_result.get("wheel"), station_result.get("wheel")
            if direct_wheel == station_wheel == "visible-scroll":
                result["wheel_parity"] = "PASS: visible history scroll in direct and Bridge paths"
            elif direct_wheel == station_wheel:
                result["wheel_parity"] = f"NOT VERIFIED: both paths observed {direct_wheel}"
            else:
                result["wheel_parity"] = f"DIFFERENT: direct={direct_wheel}; bridge={station_wheel}"
    except Exception as exc:
        result["error"] = type(exc).__name__
    finally:
        if direct is not None:
            direct.close()
        if station is not None:
            station.close()
        if handle is not None:
            try:
                backend.stop(handle)
                result["cleanup"] = "PASS"
            except Exception as exc:
                result["cleanup"] = f"FAILED: {type(exc).__name__}"
        else:
            result["cleanup"] = "PASS: no Bridge station was created"
    return result


def probe(codex_session_id: str, codex_cwd: Path, *, provider_seconds: int = 45) -> dict[str, object]:
    if not 10 <= provider_seconds <= 60:
        raise ValueError("provider timeout must be between 10 and 60 seconds")
    cwd = codex_cwd.expanduser().resolve(strict=True)
    identity = provider_session_metadata("codex", codex_session_id, cwd=cwd)
    if identity is None or identity.identity.cwd != str(cwd):
        raise ValueError("the explicit Codex session ID has no matching structured metadata in the supplied cwd")
    backend = TmuxTerminalBackend()
    with __import__("tempfile").TemporaryDirectory(prefix="bridge-native-input-") as temp:
        root = Path(temp)
        result: dict[str, object] = {
            "model_calls": 0,
            "codex_session": "exact supplied ID and cwd metadata verified",
            "codex": {}, "claude": {},
        }
        codex_argv = [
            "codex", "--no-daemon", "--sandbox", "read-only", "--ask-for-approval", "never",
            "-C", str(cwd), "resume", codex_session_id,
        ]
        result["codex"] = _provider_result(
            "codex", codex_argv, cwd, timeout=provider_seconds, backend=backend,
            socket_path=root / "codex.sock",
        )
        claude_cwd = ROOT
        result["claude"] = _provider_result(
            "claude", ["claude"], claude_cwd, timeout=provider_seconds, backend=backend,
            socket_path=root / "claude.sock",
        )
        result["cleanup"] = "PASS" if all(
            _dict(provider_result).get("cleanup") == "PASS" for provider_result in (result["codex"], result["claude"])
        ) else "FAILED"
        return result


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-session-id", required=True)
    parser.add_argument("--codex-cwd", type=Path, required=True)
    parser.add_argument("--provider-timeout", type=int, choices=range(10, 61), default=45)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = probe(args.codex_session_id, args.codex_cwd, provider_seconds=args.provider_timeout)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("cleanup") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
