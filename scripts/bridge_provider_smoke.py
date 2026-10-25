#!/usr/bin/env python3
"""Opt-in native PTY startup probes, with zero model prompts by default.

Run: python scripts/bridge_provider_smoke.py --provider codex
Provider screen contents are inspected in memory and excluded from the report.
This cannot verify model execution, mouse interaction, or native restoration.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_tools.terminal import TmuxTerminalBackend  # noqa: E402

COMMANDS = {
    "codex": ["codex", "--no-daemon"],
    "claude": ["claude"],
    "copilot": ["copilot", "--no-auto-update"],
    "kilo": ["kilo"],
    "opencode": ["opencode"],
}


def probe(provider: str, seconds: float, *, submit: bool = False) -> dict[str, object]:
    command = list(COMMANDS[provider])
    if submit:
        prompt = ("Reply with the uppercased word bridge followed by an underscore, "
                  "then the uppercased word smoke, an underscore, then the uppercased "
                  "word ok. Do not use tools or inspect or change any files.")
        if provider == "codex":
            command += ["--sandbox", "read-only", "--ask-for-approval", "never", prompt]
        elif provider == "claude":
            command += ["--tools", "", "--", prompt]
        elif provider == "copilot":
            command += ["--interactive", prompt]
        else:
            raise ValueError("Model probes are limited to Codex, Claude and Copilot")
    result: dict[str, object] = {
        "provider": provider, "model_call_budget": int(submit),
        "model_execution": "NOT VERIFIED", "mouse": "NOT VERIFIED",
        "native_resume": "NOT VERIFIED", "keyboard": "NOT VERIFIED",
        "ansi_rendering": "NOT VERIFIED", "resize": "NOT VERIFIED",
    }
    if not shutil.which(command[0]) or not shutil.which("tmux"):
        result["startup"] = "NOT VERIFIED: executable unavailable"
        return result
    try:
        version = subprocess.run([command[0], "--version"], text=True,
                                 capture_output=True, timeout=15, check=False)
        lines = version.stdout.strip().splitlines()
        result["version"] = lines[0][:160] if lines else "unknown"
    except (subprocess.SubprocessError, OSError) as exc:
        result["startup"] = "NOT VERIFIED: version probe failed"
        result["error_type"] = type(exc).__name__
        return result
    backend = TmuxTerminalBackend()
    with tempfile.TemporaryDirectory(prefix="bridge-smoke-") as directory:
        root = Path(directory)
        cwd = root / "workspace"
        cwd.mkdir()
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        subprocess.run(["git", "init", "-q", str(cwd)], check=True)
        handle = None
        started = time.monotonic()
        try:
            handle = backend.start(provider_argv=command,
                bridge_argv=[sys.executable, "-c", "import time; time.sleep(120)"],
                cwd=cwd, socket_path=runtime / "tmux.sock",
                session_name="probe", size=(120, 40))
            text = ""
            while time.monotonic() - started < seconds:
                try:
                    text = backend.capture(handle, pane="provider", ansi=True)
                except subprocess.CalledProcessError:
                    break
                time.sleep(0.2)
            result["output_seen"] = bool(text.strip())
            if submit and "BRIDGE_SMOKE_OK" in text:
                result["model_execution"] = "PASS: expected response observed"
            result["ansi_rendering"] = "PASS" if "\x1b[" in text else "NOT VERIFIED"
            result["startup"] = "OUTPUT OBSERVED" if text.strip() else "NOT VERIFIED: no output"
            pane = subprocess.run(["tmux", "-S", str(handle.socket_path),
                "display-message", "-p", "-t", handle.provider_pane,
                "#{pane_dead}:#{pane_dead_status}"], text=True,
                capture_output=True, check=False).stdout.strip()
            result["pane_state"] = pane
            result["process_alive_after_startup"] = pane.startswith("0:")
            dimensions = []
            for cols, rows in [(80, 24), (120, 40), (180, 55)]:
                backend.resize(handle, cols=cols, rows=rows)
                dimensions.append(subprocess.run(["tmux", "-S", str(handle.socket_path),
                    "display-message", "-p", "-t", handle.provider_pane,
                    "#{pane_width}x#{pane_height}"], text=True,
                    capture_output=True, check=True).stdout.strip())
            result["pane_dimensions"] = dimensions
            result["resize"] = "GEOMETRY PASS; provider response NOT VERIFIED"
            backend.send_keys(handle, pane="provider", keys="C-c")
            time.sleep(0.3)
            backend.send_keys(handle, pane="provider", keys="C-c")
            result["keyboard"] = "interrupt sent; response NOT VERIFIED"
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            result["startup"] = "FAILED"
            result["error_type"] = type(exc).__name__
        finally:
            if handle:
                backend.stop(handle)
                result["private_server_cleanup"] = "PASS" if not backend.server_exists(handle) else "FAILED"
            result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=sorted(COMMANDS), action="append", required=True)
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--model-call-budget", type=int, default=0, choices=range(4),
                        help="Explicit opt-in: submit at most N harmless initial prompts (default zero)")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 30:
        parser.error("--seconds must be between 1 and 30")
    if args.model_call_budget and (len(args.provider) > args.model_call_budget or
                                   any(p not in {"codex", "claude", "copilot"} for p in args.provider)):
        parser.error("model probes require one budget unit per provider; only codex/claude/copilot supported")
    os.environ.setdefault("TERM", "xterm-256color")
    print(json.dumps([probe(provider, args.seconds, submit=bool(args.model_call_budget))
                      for provider in args.provider], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
