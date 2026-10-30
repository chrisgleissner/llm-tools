#!/usr/bin/env python3
"""Scripted stand-in for an AI coding agent, used only by scripts/bridge_demo.py.

It prints a clearly labelled, generic "agent session" (reading files, editing,
running tests), waits for a gate file so the recording can show the Bridge
controls while a tool call is pending, then asks for ``/exit``.

The script never touches the network, never spawns a process and never writes
to disk. All "files" and "test results" it prints are invented.

Environment:
  DEMO_PROVIDER_SPEED  Multiplier for the delays (default 1.0, 0 disables them).
  DEMO_PROVIDER_GATE   Path; the pending tool call finishes once this file exists.
                       Without it the script does not wait.
  DEMO_PROVIDER_GATE_DIR
                       Directory; like DEMO_PROVIDER_GATE, but the gate is the file
                       named after the working folder, so several missions in
                       different folders can be held or released independently.
  DEMO_PROVIDER_NAME   Name of the CLI this stand-in plays ("claude", "codex");
                       shown in the banner. The launchers in the demo's bin/ set it.
  DEMO_PROVIDER_SCENARIO
                       "limits" plays the capacity hand-off story (see
                       docs/llm-bridge/demo.md): the "claude" stand-in works until
                       the file named by DEMO_PROVIDER_LIMIT_FILE exists, then reports
                       that its 5-hour limit is used up and exits with status 1; the
                       "codex" stand-in resumes the same task and keeps working.
  DEMO_PROVIDER_LIMIT_FILE
                       Path whose existence makes the "claude" limits scenario hit its limit.
  DEMO_PROVIDER_GO_DIR
                       Directory; the limits scenario prints its banner at once but starts
                       streaming only when the file "<name>.go" exists there, so the
                       recording decides when each provider's work begins.
  DEMO_PROVIDER_CADENCE
                       Seconds between the lines the limits scenario streams (default 0.5).
                       One line at a steady pace reads as work in progress; bursts do not.
"""

from __future__ import annotations

import os
import re
import signal
import sys
import time
from pathlib import Path

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[38;5;245m"
CYAN = "\x1b[38;5;80m"
BLUE = "\x1b[38;5;75m"
GREEN = "\x1b[38;5;78m"
RED = "\x1b[38;5;203m"
YELLOW = "\x1b[38;5;221m"
BADGE = "\x1b[1;38;5;16;48;5;80m"

SPEED = float(os.environ.get("DEMO_PROVIDER_SPEED", "1") or "0")
GATE = os.environ.get("DEMO_PROVIDER_GATE", "")
GATE_DIR = os.environ.get("DEMO_PROVIDER_GATE_DIR", "")
NAME = os.environ.get("DEMO_PROVIDER_NAME", "")
SCENARIO = os.environ.get("DEMO_PROVIDER_SCENARIO", "")
LIMIT_FILE = os.environ.get("DEMO_PROVIDER_LIMIT_FILE", "")
GO_DIR = os.environ.get("DEMO_PROVIDER_GO_DIR", "")
CADENCE = float(os.environ.get("DEMO_PROVIDER_CADENCE", "0.5") or "0.5")


def pause(seconds: float) -> None:
    if SPEED > 0:
        time.sleep(seconds / SPEED)


# What the "limits" scenario has printed, so it can repaint after a resize the way
# a real full-screen agent does.  A tmux pane that is resized while a program that
# never redraws is running can end up with blank rows above the output.
TRANSCRIPT: list[str] = []
BUSY: list[bool] = [False]
REPAINT_PENDING: list[bool] = [False]


def repaint() -> None:
    sys.stdout.write("\x1b[H\x1b[J" + "".join(line + "\n" for line in TRANSCRIPT))
    sys.stdout.flush()


def on_resize(_signum: int, _frame: object) -> None:
    if BUSY[0]:
        REPAINT_PENDING[0] = True
    else:
        repaint()


def out(text: str = "") -> None:
    BUSY[0] = True
    try:
        if SCENARIO == "limits":
            TRANSCRIPT.append(text)
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    finally:
        BUSY[0] = False
    if REPAINT_PENDING[0]:
        REPAINT_PENDING[0] = False
        repaint()


def typed(text: str, delay: float = 0.012) -> None:
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        pause(delay)
    sys.stdout.write("\n")
    sys.stdout.flush()


def prompt_text(argv: list[str]) -> str:
    """Short form of the prompt: the last non-option argument of the launch command.

    Bridge wraps the task in a long context block; only the mission objective
    is shown so the demo screen stays readable.
    """
    if len(argv) < 2 or argv[-1].startswith("-"):
        return "(no prompt given)"
    prompt = argv[-1]
    task = re.search(r"Task \d+ \((.+?)\), revision", prompt)
    marker = "Mission objective:\n"
    if task:
        prompt = task.group(1)
    elif marker in prompt:
        prompt = prompt.split(marker, 1)[1].split("\n\n", 1)[0]
    return " ".join(prompt.split())[:100]


def banner(cwd: str) -> None:
    width = 58
    title = f" {BADGE} demo provider {RESET}{BOLD} scripted session, no AI model{RESET}"
    title_visible = 1 + 15 + 30
    if NAME:
        suffix = f"{NAME} · scripted, no AI model"
        title = f" {BADGE} demo provider {RESET}{BOLD} {suffix}{RESET}"
        title_visible = 1 + 15 + 1 + len(suffix)
    detail = f"model: demo-{NAME or '1'}   cwd: {cwd}"
    rows = [(title, title_visible), (f" {DIM}{detail}{RESET}", 1 + len(detail))]
    out(f"{DIM}╭{'─' * width}╮{RESET}")
    for text, visible in rows:
        out(f"{DIM}│{RESET}{text}{' ' * (width - visible)}{DIM}│{RESET}")
    out(f"{DIM}╰{'─' * width}╯{RESET}")
    out()


def step(title: str, detail: str = "", wait: float = 0.7) -> None:
    out(f"{GREEN}●{RESET} {BOLD}{title}{RESET}" + (f" {DIM}{detail}{RESET}" if detail else ""))
    pause(wait)


def note(text: str, wait: float = 0.35) -> None:
    out(f"  {DIM}⎿{RESET} {text}")
    pause(wait)


def diff(lines: list[tuple[str, str]]) -> None:
    for kind, text in lines:
        colour = {"+": GREEN, "-": RED}.get(kind, DIM)
        out(f"    {colour}{kind} {text}{RESET}")
        pause(0.07)
    pause(0.4)


def spinner(label: str, until_gate: bool) -> None:
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    index = 0
    started = time.monotonic()
    while True:
        done = (not until_gate) or (GATE and Path(GATE).exists())
        if done and time.monotonic() - started > 1.2 / max(SPEED, 0.01):
            break
        sys.stdout.write(f"\r  {CYAN}{frames[index % len(frames)]}{RESET} {label} {DIM}{time.monotonic() - started:4.1f}s{RESET}\x1b[K")
        sys.stdout.flush()
        index += 1
        time.sleep(0.12 if SPEED > 0 else 0.001)
        if SPEED == 0 and index > 3:
            break
    sys.stdout.write(f"\r  {GREEN}✓{RESET} {label}\x1b[K\n")
    sys.stdout.flush()


def stream(text: str) -> None:
    """One line of the transcript, then a fixed pause: a steady cadence, never a burst."""
    out(text)
    time.sleep(CADENCE)


def step_line(title: str, detail: str = "") -> None:
    stream(f"{GREEN}●{RESET} {BOLD}{title}{RESET}" + (f" {DIM}{detail}{RESET}" if detail else ""))


def note_line(text: str) -> None:
    stream(f"  {DIM}⎿{RESET} {text}")


def diff_line(kind: str, text: str) -> None:
    colour = {"+": GREEN, "-": RED}.get(kind, DIM)
    stream(f"    {colour}{kind} {text}{RESET}")


def wait_for_go() -> None:
    """Hold the stream until the recording releases it (no gate configured: start at once)."""
    if not (GO_DIR and NAME):
        return
    gate = Path(GO_DIR) / f"{NAME}.go"
    while not gate.exists():
        time.sleep(0.05)


def limits_claude() -> int:
    """First half of the hand-off story: work line by line, then the 5-hour limit is used up."""
    wait_for_go()
    step_line("Reading the project layout", "src/ tests/ pyproject.toml")
    note_line("3 directories, 9 files")
    step_line("Read", "src/api/server.py (184 lines)")
    step_line("Write", "src/api/ratelimit.py (+48 lines)")
    diff_line("+", "class TokenBucket:")
    diff_line("+", "    def __init__(self, rate, burst):")
    diff_line("+", "        self.rate, self.burst = rate, burst")
    step_line("Edit", "src/api/server.py (+6 -1)")
    diff_line("+", "app.use(rate_limit(rate=5, burst=10))")
    step_line("Run", "pytest -q tests/test_ratelimit.py")
    names = ("test_burst", "test_refill", "test_per_client", "test_retry_after", "test_reset", "test_limit")
    for index in range(60):  # the recording ends this by creating the limit file
        if LIMIT_FILE and Path(LIMIT_FILE).exists():
            break
        stream(f"    {DIM}tests/test_ratelimit.py::{names[index % len(names)]}{RESET}")
    out(f"  {RED}✗{RESET} {BOLD}5-hour limit reached{RESET} {DIM}· wait for the reset{RESET}")
    time.sleep(CADENCE)
    out(f"  {DIM}⎿ stopping; task 1 is not finished{RESET}")
    time.sleep(0.2)
    return 1


def limits_codex() -> int:
    """Second half: another provider resumes the same task from the repository state, passes the tests and stops."""
    wait_for_go()
    step_line("Run", "git diff --stat")
    note_line("src/api/ratelimit.py | 48 +++")
    note_line("src/api/server.py    |  7 +-")
    step_line("Read", "src/api/ratelimit.py (48 lines)")
    step_line("Write", "tests/test_ratelimit.py (+37 lines)")
    step_line("Run", "pytest -q tests/test_ratelimit.py")
    for name in ("test_burst", "test_refill", "test_per_client", "test_retry_after"):
        stream(f"    {GREEN}✓{RESET} {DIM}tests/test_ratelimit.py::{name}{RESET}")
    stream(f"  {GREEN}✓{RESET} {BOLD}6 passed{RESET} {DIM}in 0.41s{RESET}")
    stream(f"{YELLOW}Task complete.{RESET} Waiting for your review.")
    return 0


def main() -> int:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    prompt = prompt_text(sys.argv)
    cwd = os.path.basename(os.getcwd()) or "."
    if "-C" in sys.argv[1:-1]:
        cwd = os.path.basename(sys.argv[sys.argv.index("-C") + 1].rstrip("/")) or cwd
    global GATE
    if GATE_DIR:
        GATE = os.path.join(GATE_DIR, cwd)
    if SCENARIO == "limits":  # start on the top row, with no empty line above the banner
        signal.signal(signal.SIGWINCH, on_resize)
        sys.stdout.write("\x1b[H\x1b[J")  # home and erase: ED 2 would push blank rows into tmux history
    else:
        out("\x1b[2J\x1b[H")
    banner(f"~/{cwd}")
    out(f"{BOLD}{BLUE}›{RESET} {prompt}")
    out()
    pause(0.2 if SCENARIO == "limits" else 0.9)
    if SCENARIO == "limits":
        return limits_claude() if NAME == "claude" else limits_codex()

    step("Reading the project layout", "src/ tests/ pyproject.toml")
    note("3 directories, 9 files")
    step("Read", "src/api/server.py (184 lines)")
    step("Read", "src/api/middleware.py (61 lines)")
    note("requests are handled by one shared router; no per-client state yet", 0.9)
    step("Write", "src/api/ratelimit.py (+48 lines)", 0.5)
    diff(
        [
            ("+", "class TokenBucket:"),
            ("+", "    def __init__(self, rate: float, burst: int) -> None:"),
            ("+", "        self.rate, self.burst = rate, burst"),
            ("+", "        self.tokens, self.updated = float(burst), monotonic()"),
        ]
    )
    step("Edit", "src/api/server.py (+6 -1)", 0.5)
    diff(
        [
            (" ", "app.use(log_requests)"),
            ("-", "app.use(cors_headers)"),
            ("+", "app.use(rate_limit(rate=5, burst=10))"),
            ("+", "app.use(cors_headers)"),
        ]
    )
    step("Write", "tests/test_ratelimit.py (+37 lines)", 1.0)
    step("Run", "pytest -q tests/test_ratelimit.py", 0.2)
    spinner("Running tests", until_gate=True)
    out(f"    {GREEN}......{RESET} {DIM}6 passed in 0.41s{RESET}")
    pause(0.8)
    step("Run", "pytest -q", 0.2)
    spinner("Running full suite", until_gate=False)
    out(f"    {GREEN}..........................{RESET} {DIM}26 passed in 1.83s{RESET}")
    pause(0.8)
    out()
    out(f"{BOLD}Summary{RESET}")
    typed("  Added a token-bucket limiter (5 requests/s, burst 10 per client) and", 0.006)
    typed("  wired it into the router. Rejected requests return 429 with a", 0.006)
    typed("  Retry-After header. Tests cover bursts, refill and per-client isolation.", 0.006)
    out()
    out(f"{YELLOW}Task complete.{RESET} Review the changes, then type {BOLD}/exit{RESET} to finish.")
    out()
    while True:
        try:
            line = input(f"{BOLD}{BLUE}›{RESET} ")
        except EOFError:
            return 0
        if line.strip() == "/exit":
            out(f"{DIM}Session ended. Goodbye.{RESET}")
            pause(0.4)
            return 0
        out(f"{DIM}(demo provider) type /exit to finish.{RESET}")


if __name__ == "__main__":
    raise SystemExit(main())
